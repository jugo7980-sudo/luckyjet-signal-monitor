"""Strategy 3 regression: recovery math, idempotency, balance, API and archive."""
import asyncio
import json
from datetime import datetime, timezone
from fakeredis.aioredis import FakeRedis

from luckyjet_signal.demo_ledger import DemoLedger
from luckyjet_signal.target_streak_demo import TargetStreakStrategy
from demo_persistence import DemoPersistence, ARCHIVE_CYCLES, TARGET_COMPLETED
from luckyjet_reliable_gate import ReliableTelegramPasswordGate
from mini_app_controls import controls
from mini_app_history import history
from target_streak_notifications import normalize, changed_events
from test_archive_four_tabs import signed

NOW = datetime(2026, 11, 8, tzinfo=timezone.utc)


def advance(ledger, n, coefficient):
    result, changed = ledger.process_archived_round(
        f'round-{n}', str(coefficient), now=NOW, stream_id=f'{n * 1000}-0')
    assert changed
    return result


def new_ledger(x='1.57', balance=1_000_000_00, stake=15000):
    return DemoLedger(
        currency='KRW', balance_cents=balance,
        initial_balance_cents=balance, base_stake_cents=10000,
        target_streak={'enabled': True, 'target': x, 'trigger': 3,
                       'stake_cents': stake, 'phase': 'waiting'},
    )


def test_default_off_does_not_change_existing_strategy():
    ledger = DemoLedger(enabled=True, trigger_streak=2)
    assert ledger.target_streak.enabled is False
    for n, x in enumerate((1.2, 1.3, 2.0), 1):
        advance(ledger, n, x)
    assert ledger.balance_cents == ledger.initial_balance_cents + ledger.base_stake_cents
    assert not ledger.target_history


def test_x157_starts_on_next_round_and_recovers_loss_plus_original_bet():
    ledger = new_ledger()
    for i in (1, 2, 3):
        advance(ledger, i, '1.20')
        assert ledger.balance_cents == ledger.initial_balance_cents
    assert ledger.target_streak.phase == 'betting'
    assert ledger.target_streak.rounds == []
    advance(ledger, 4, '1.10')
    assert ledger.target_streak.next_stake_cents == 52700  # 527 won after 150 lost
    assert ledger.balance_cents == ledger.initial_balance_cents - 15000
    advance(ledger, 5, '1.57')
    cycle = ledger.target_history[0]
    assert cycle['strategy'] == 'target_streak'
    assert cycle['outcome'] == 'recovered'
    assert cycle['total_stake_cents'] == 15000 + 52700
    assert cycle['payout_cents'] == 82700  # 527 * 1.57 rounded to whole won
    assert cycle['net_cents'] == 15000
    assert ledger.balance_cents == ledger.initial_balance_cents + 15000
    assert ledger.target_streak.phase == 'waiting'
    assert not ledger.target_streak.rounds


def test_x132_and_first_win_math_are_honest():
    ledger = new_ledger('1.32')
    for i in (1, 2, 3):
        advance(ledger, i, '1.10')
    advance(ledger, 4, '1.10')
    assert ledger.target_streak.next_stake_cents == 93800
    advance(ledger, 5, '1.32')
    assert ledger.target_history[0]['net_cents'] >= 15000
    assert ledger.target_history[0]['payout_cents'] == 123800
    first = new_ledger('1.57')
    for i in (1, 2, 3):
        advance(first, i, '1.10')
    advance(first, 4, '1.57')
    assert first.target_history[0]['net_cents'] == 8600  # 86 won, not 150
    assert first.target_history[0]['net_cents'] < first.target_streak.stake_cents


def test_three_losses_automatically_increase_stakes_without_inventing_wins():
    ledger = new_ledger()
    for i in (1, 2, 3):
        advance(ledger, i, '1.10')
    advance(ledger, 4, '1.10')
    advance(ledger, 5, '1.10')
    assert ledger.target_streak.next_stake_cents == 145100
    replica = DemoLedger(**ledger.dump())
    assert replica.target_streak.rounds == ledger.target_streak.rounds
    assert replica.target_streak.next_stake_cents == 145100
    advance(replica, 6, '1.57')
    assert replica.target_history[0]['net_cents'] >= 15000
    entry = replica.target_history[0]
    assert len(entry['rounds']) == 3
    assert entry['total_stake_cents'] == sum(r['stake_cents'] for r in entry['rounds'])
    previous_balance = replica.balance_cents
    prior = replica.target_history[:]
    event, changed = replica.process_archived_round(
        'round-6', '1.57', now=NOW, stream_id='6000-0')
    assert not changed and event is None
    assert replica.balance_cents == previous_balance and replica.target_history == prior


def test_insufficient_funds_pauses_without_negative_balance_and_no_guess():
    ledger = new_ledger(balance=30000)
    for i in (1, 2, 3):
        advance(ledger, i, '1.10')
    advance(ledger, 4, '1.10')
    assert ledger.balance_cents == 15000
    assert not ledger.target_streak.enabled
    assert ledger.target_streak.phase == 'paused_funds'
    assert ledger.target_history[0]['outcome'] == 'insufficient_balance'
    assert ledger.target_history[0]['net_cents'] == -15000


def test_gap_does_not_stitch_two_streaks_or_claim_unverified_balance():
    async def run():
        gate = ReliableTelegramPasswordGate(None)
        gate.redis = FakeRedis(decode_responses=True)
        gate.authorized.add(42)
        ledger = new_ledger()
        gate.demo_ledgers[42] = ledger
        for i in (1, 2, 3, 4):
            await gate._handle_demo_round(42, '1.10', round_id=str(i), stream_id=f'{i}-0')
        assert ledger.target_streak.rounds
        await gate.on_history_gap(stream_id='5-0', silent=True)
        assert not ledger.target_streak.enabled
        assert ledger.target_streak.phase == 'paused_gap'
        assert ledger.target_history[0]['outcome'] == 'incomplete_data'
        assert ledger.target_history[0]['net_cents'] is None
        assert not ledger.balance_verified
        await gate.close()
    asyncio.run(run())


def test_private_third_history_survives_atomic_save_restart_and_archive():
    async def run():
        redis = FakeRedis(decode_responses=True)
        store = DemoPersistence()
        ledger = new_ledger()
        for i in (1, 2, 3):
            advance(ledger, i, '1.10')
        advance(ledger, 4, '1.10')
        advance(ledger, 5, '1.57')
        assert ledger.target_history
        await store.save(redis, 42, ledger)
        assert len(json.loads(await redis.hget(TARGET_COMPLETED, '42'))) == 1
        assert await redis.zcard(ARCHIVE_CYCLES + '42') == 1
        state = json.loads(await redis.hget('luckyjet:demo_sessions', '42'))
        restored = {42: DemoLedger(**state)}
        await DemoPersistence().restore(redis, restored)
        assert restored[42].target_history == ledger.target_history
        assert restored[42].target_streak.dump() == ledger.target_streak.dump()
        await redis.aclose()
    asyncio.run(run())


def test_target_controls_and_separate_history_and_notices_api():
    async def run():
        gate = ReliableTelegramPasswordGate(None)
        gate.redis = FakeRedis(decode_responses=True)
        gate.authorized.add(42)
        gate.demo_ledgers[42] = new_ledger()
        ledger = gate.demo_ledgers[42]
        ledger.target_streak.enabled = False
        ledger.last_archived_stream_id = '5000-0'
        original = ledger.target_streak.dump()
        bad = await controls('tma '+signed(), gate, 'fixture', 'POST', json.dumps({
            'action': 'target_streak_config',
            'value': {'target': '1.00', 'trigger': 3, 'stake': '150'},
        }).encode())
        assert bad[0] == '409 Conflict'
        assert ledger.target_streak.dump() == original
        good = await controls('tma '+signed(), gate, 'fixture', 'POST', json.dumps({
            'action': 'target_streak_config',
            'value': {'target': '1.57', 'trigger': 3, 'stake': '150'},
        }).encode())
        assert good[0] == '200 OK'
        assert json.loads(good[2])['target_streak']['target'] == '1.57'
        enabled = await controls('tma '+signed(), gate, 'fixture', 'POST',
                                json.dumps({'action': 'target_streak_toggle'}).encode())
        assert enabled[0] == '200 OK'
        assert ledger.target_streak.enabled
        change = await controls('tma '+signed(), gate, 'fixture', 'POST',
                                json.dumps({'action': 'target_notice_event', 'value': 'loss'}).encode())
        assert change[0] == '200 OK'
        assert json.loads(change[2])['target_streak_notifications']['events']['loss'] is True
        await gate._persist_user(42)
        assert await gate.redis.hexists('luckyjet:target_streak_notices:v1', '42')
        ledger.target_streak.reset_progress()
        for i in (1, 2, 3):
            advance(ledger, i, '1.10')
        advance(ledger, 4, '1.10')
        advance(ledger, 5, '1.57')
        assert (await history('tma '+signed(), gate, 'fixture', 'view=target_cycles'))[0] == '200 OK'
        data = json.loads((await history('tma '+signed(), gate, 'fixture',
                                         'view=target_cycles'))[2])
        assert data['stats']['total'] == 1
        assert data['items'][0]['strategy'] == 'target_streak'
        assert data['items'][0]['net_cents'] == 15000
        assert json.loads((await history('tma '+signed(), gate, 'fixture',
                                         'view=target_cycle&index=0'))[2])['item']['rounds']
        await gate.close()
    asyncio.run(run())


def test_settings_default_and_notice_events():
    assert normalize()['events'] == {
        'armed': True, 'betting': True, 'loss': False, 'goal': True, 'stopped': True,
    }
    before = {'enabled': True, 'phase': 'waiting', 'steps': 0}
    after = {'enabled': True, 'phase': 'betting', 'steps': 0}
    assert changed_events(before, after) == ['armed']
    assert changed_events(after, {'enabled': True, 'phase': 'betting', 'steps': 1}) == ['betting', 'loss']
