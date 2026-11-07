"""Second DEMO strategy: fixed stake, shared balance, durable and honest history."""
import asyncio
import json
from datetime import datetime, timezone

import pytest
from fakeredis.aioredis import FakeRedis

from luckyjet_signal.demo_ledger import DemoLedger
from luckyjet_signal.high_gap_demo import HighGapStrategy, parse_multiplier
from demo_persistence import DemoPersistence, ARCHIVE_CYCLES, ARCHIVE_ENTRIES
from mini_app_controls import controls
from mini_app_history import history
from luckyjet_reliable_gate import ReliableTelegramPasswordGate
from test_archive_four_tabs import signed, query

NOW = datetime(2026, 11, 8, tzinfo=timezone.utc)


def ledger_with_high(*, wait=100, target='100', reference='100',
                     stake=1000, balance=1000000, classic=False):
    return DemoLedger(
        enabled=classic, trigger_streak=2, balance_cents=balance,
        initial_balance_cents=balance, base_stake_cents=1000,
        high_gap={'enabled': True, 'reference': reference, 'target': target,
                  'wait_rounds': wait, 'stake_cents': stake,
                  'phase': 'waiting_reference'},
    )


def advance(ledger, i, coefficient):
    result, changed = ledger.process_archived_round(
        f'round-{i}', str(coefficient), now=NOW, stream_id=f'{i * 1000}-0')
    assert changed
    return result


def test_default_disabled_does_not_affect_existing_strategy():
    ledger = DemoLedger(enabled=True, trigger_streak=2)
    assert not ledger.high_gap.enabled
    before = ledger.high_gap.dump()
    for i, value in enumerate((1.2, 1.3, 2.0), 1):
        advance(ledger, i, value)
    assert len(ledger.history) == 1
    assert ledger.high_history == []
    assert ledger.high_gap.dump() == before
    assert ledger.balance_cents == ledger.initial_balance_cents + 1000


def test_exact_trigger_and_hundred_completed_round_wait_no_retroactive_bet():
    ledger = ledger_with_high()
    advance(ledger, 1, 100)
    assert ledger.high_gap.phase == 'waiting_reference'
    advance(ledger, 2, 101)
    assert ledger.high_gap.phase == 'waiting'
    initial = ledger.balance_cents
    for i in range(3, 103):
        advance(ledger, i, 1.4)
        assert ledger.balance_cents == initial  # no stake during 100-round wait
    assert ledger.high_gap.phase == 'betting'
    assert ledger.high_gap.elapsed == 100
    assert not ledger.high_gap.rounds
    advance(ledger, 103, 1.35)
    assert ledger.balance_cents == initial - 1000
    assert len(ledger.high_gap.rounds) == 1
    advance(ledger, 104, 100)  # >= cashout, but NOT a fresh >100 reference
    assert len(ledger.high_history) == 1
    entry = ledger.high_history[0]
    assert entry['strategy'] == 'high_gap'
    assert entry['wait_rounds'] == 100
    assert entry['cashout_multiplier'] == '100'
    assert entry['total_stake_cents'] == 2000
    assert entry['payout_cents'] == 100000
    assert entry['net_cents'] == 98000
    assert entry['outcome'] == 'recovered'
    assert ledger.balance_cents == initial + 98000
    assert ledger.high_gap.phase == 'waiting_reference'


def test_shared_balance_and_independent_cycle_profit():
    ledger = ledger_with_high(wait=0, target='1.50', classic=True, balance=100000)
    advance(ledger, 1, 101)
    advance(ledger, 2, 1.2)
    advance(ledger, 3, 1.2)
    advance(ledger, 4, 2.5)
    assert len(ledger.history) == 1
    assert len(ledger.high_history) == 1
    first = ledger.history[0]
    second = ledger.high_history[0]
    assert first['net_cents'] == 1000  # the existing fixed x2 logic
    assert second['net_cents'] == -1500  # 3*1000 spent, 1500 cashed out
    assert second['payout_cents'] == 1500
    assert ledger.balance_cents == 99500
    assert ledger.high_gap.phase == 'waiting_reference'


def test_high_line_stops_if_both_virtual_stakes_cannot_fit_pre_round_balance():
    ledger = ledger_with_high(wait=0, target='100', classic=True, balance=2500)
    advance(ledger, 1, 101)
    advance(ledger, 2, 1.2)  # high stake 1000
    advance(ledger, 3, 1.2)  # classic arms; high stake 1000
    assert ledger.balance_cents == 500
    # Classic active, needs 1000 and cannot afford it; high cannot afford 1000.
    advance(ledger, 4, 2.5)
    assert ledger.high_gap.phase == 'paused_funds'
    assert not ledger.high_gap.enabled
    assert ledger.high_history[0]['outcome'] == 'insufficient_balance'
    assert ledger.high_history[0]['net_cents'] == -2000


def test_shared_funding_preflight_prioritizes_existing_classic_line():
    ledger = ledger_with_high(wait=0, target='100', classic=True, balance=2500)
    advance(ledger, 1, 101)
    advance(ledger, 2, 1.2)
    advance(ledger, 3, 1.2)
    ledger.balance_cents = 1500
    advance(ledger, 4, 2.5)
    assert ledger.history[0]['net_cents'] == 1000
    assert ledger.high_history[0]['net_cents'] == -2000
    assert ledger.high_history[0]['outcome'] == 'insufficient_balance'
    assert ledger.balance_cents == 2500  # high made no bet on this round


def test_restart_preserves_wait_count_and_pending_bets_exactly_once():
    ledger = ledger_with_high(wait=3)
    advance(ledger, 1, 110)
    advance(ledger, 2, 1.2)
    replica = DemoLedger(**ledger.dump())
    assert replica.high_gap.elapsed == 1
    assert replica.high_gap.reference_id == 'round-1'
    advance(replica, 3, 1.2)
    advance(replica, 4, 1.2)
    assert replica.high_gap.phase == 'betting'
    advance(replica, 5, 1.2)
    assert replica.balance_cents == 999000
    saved = DemoLedger(**replica.dump())
    old_balance = saved.balance_cents
    event, changed = saved.process_archived_round(
        'round-5', '1.2', now=NOW, stream_id='5000-0')
    assert event is None and changed is False
    assert saved.balance_cents == old_balance
    advance(saved, 6, 100)
    assert saved.high_history[0]['net_cents'] == 98000


def test_demo_reset_archives_open_session_without_changing_archived_events():
    async def run():
        redis = FakeRedis(decode_responses=True)
        ledger = ledger_with_high(wait=0)
        advance(ledger, 1, 110)
        advance(ledger, 2, 1.1)
        assert ledger.high_gap.rounds
        ledger.reset(currency='KRW', balance_cents=500000, base_stake_cents=10000)
        assert ledger.high_gap.enabled
        assert ledger.high_gap.phase == 'waiting_reference'
        assert ledger.high_history == []
        assert ledger.pending_archive_cycles
        store = DemoPersistence()
        await store.save(redis, 42, ledger)
        assert ledger.pending_archive_cycles == []
        assert await redis.zcard(ARCHIVE_CYCLES+'42') == 1
        entry_id = (await redis.zrange(ARCHIVE_CYCLES+'42', 0, 0))[0]
        record = json.loads(await redis.hget(ARCHIVE_ENTRIES+'42', entry_id))
        assert record['strategy'] == 'high_gap'
        assert record['outcome'] == 'stopped_by_user'
        assert record['net_cents'] == -1000
        assert record['currency'] == 'USD'
        assert ledger.currency == 'KRW'
        await redis.aclose()
    asyncio.run(run())


def test_gap_closes_high_cycle_and_marks_shared_balance_unverified():
    async def run():
        gate = ReliableTelegramPasswordGate(None)
        gate.authorized.add(42)
        gate.redis = FakeRedis(decode_responses=True)
        ledger = ledger_with_high(wait=0)
        gate.demo_ledgers[42] = ledger
        advance(ledger, 1, 120)
        advance(ledger, 2, 1.2)
        assert ledger.high_gap.rounds
        await gate.on_history_gap(stream_id='3000-0', silent=True)
        assert ledger.balance_verified is False
        assert ledger.high_gap.phase == 'paused_gap'
        assert not ledger.high_gap.enabled
        assert ledger.high_history[0]['outcome'] == 'incomplete_data'
        assert ledger.high_history[0]['net_cents'] is None
        await gate.redis.aclose()
        gate.redis = None
        await gate.close()
    asyncio.run(run())


def test_config_validation_and_no_changes_when_invalid():
    high = HighGapStrategy()
    before = high.dump()
    assert not high.configure('0.99', 100, '100', 1000, min_stake=1000)
    assert high.dump() == before
    assert not high.configure('100', -1, '100', 1000, min_stake=1000)
    assert high.dump() == before
    assert not high.configure('100', 100, '0.9', 1000, min_stake=1000)
    assert high.dump() == before
    assert high.configure('25.50', 130, '1.50', 1000, min_stake=1000)
    assert high.reference == parse_multiplier('25.50')
    assert high.target == parse_multiplier('1.50')


def test_authenticated_config_and_backfill_from_persisted_source_cursor():
    async def run():
        gate = ReliableTelegramPasswordGate(None)
        gate.authorized.add(42)
        gate.redis = FakeRedis(decode_responses=True)
        ledger = gate._demo_ledger(42)
        from round_archive import STREAM_KEY
        for i, coef in enumerate((110, 1.2, 1.3, 1.4), 1):
            await gate.redis.xadd(STREAM_KEY, {
                'type': 'round', 'coefficient': str(coef),
                'round_id': str(i), 'observed_at': str(i * 1000),
            }, id=f'{i * 1000}-0')
        ledger.last_archived_stream_id = '4000-0'
        config = {'reference': '100', 'wait_rounds': 2,
                  'target': '1.50', 'stake': '10'}
        code, _, body = await controls(
            signed(42), gate, 'fixture', 'POST',
            json.dumps({'action': 'high_gap_config', 'value': config}).encode())
        assert code == '200 OK', body
        code, _, body = await controls(
            signed(42), gate, 'fixture', 'POST',
            json.dumps({'action': 'high_gap_toggle'}).encode())
        assert code == '200 OK', body
        assert ledger.high_gap.enabled
        assert ledger.high_gap.reference_id == '1'
        assert ledger.high_gap.elapsed == 3
        assert ledger.high_gap.phase == 'betting'
        assert ledger.balance_cents == ledger.initial_balance_cents
        code, _, body = await controls(
            signed(42), gate, 'fixture', 'POST',
            json.dumps({'action': 'high_gap_config',
                        'value': dict(config, target='0.25')}).encode())
        assert code == '400 Bad Request', body
        assert ledger.high_gap.target == parse_multiplier('1.50')
        code, _, body = await controls(
            signed(42), gate, 'fixture', 'POST',
            json.dumps({'action': 'high_gap_toggle'}).encode())
        assert code == '200 OK'
        assert not ledger.high_gap.enabled
        await gate.redis.aclose()
        gate.redis = None
        await gate.close()
    asyncio.run(run())


def test_krw_payout_rounds_to_integer_won_not_fractional_display():
    ledger = DemoLedger(
        currency='KRW', enabled=False, balance_cents=100000,
        initial_balance_cents=100000, base_stake_cents=100,
        high_gap={'enabled': True, 'phase': 'waiting_reference',
                  'reference': '100', 'wait_rounds': 0,
                  'target': '1.50', 'stake_cents': 100},
    )
    advance(ledger, 1, 120)
    advance(ledger, 2, 1.5)
    assert ledger.high_history[0]['total_stake_cents'] == 100
    assert ledger.high_history[0]['payout_cents'] == 200  # nearest whole KRW
    assert ledger.high_history[0]['net_cents'] == 100
    assert ledger.balance_cents == 100100
    assert ledger.balance_cents % 100 == 0



def test_reset_preserves_high_gap_enabled_status_and_keeps_pre_reset_cycle():
    ledger = ledger_with_high(wait=0)
    advance(ledger, 1, 120)
    advance(ledger, 2, 1.3)
    assert ledger.high_gap.enabled and ledger.high_gap.rounds
    ledger.reset(currency='KRW', balance_cents=700000, base_stake_cents=10000)
    assert ledger.high_gap.enabled is True
    assert ledger.high_gap.phase == 'waiting_reference'
    assert ledger.high_gap.stake_cents == 10000  # currency changed
    assert ledger.pending_archive_cycles[0]['currency'] == 'USD'
    assert ledger.pending_archive_cycles[0]['net_cents'] == -1000
