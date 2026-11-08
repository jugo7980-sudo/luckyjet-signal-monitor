"""Strategy 2 session safeguards: close a DEMO cycle, never switch the strategy off."""
import asyncio
import json
from datetime import datetime, timezone

from fakeredis.aioredis import FakeRedis
from luckyjet_signal.demo_ledger import DemoLedger
from mini_app_controls import controls
from mini_app_history import _demo_stats
from test_archive_four_tabs import signed
from luckyjet_reliable_gate import ReliableTelegramPasswordGate
from high_gap_notifications import changed_events, format_event

NOW = datetime(2026, 11, 9, tzinfo=timezone.utc)


def make_ledger():
    return DemoLedger(currency='KRW', initial_balance_cents=10000*100,
        balance_cents=10000*100, base_stake_cents=150*100,
        high_gap={'enabled': True, 'reference': '184', 'target': '180',
                  'wait_rounds': 1, 'stake_cents': 150*100,
                  'phase': 'waiting_reference'})


def tick(ledger, idx, value):
    _, changed = ledger.process_archived_round(
        f'round-{idx}', str(value), now=NOW, stream_id=f'{idx*1000}-0')
    assert changed


def setup(ledger, **safety):
    strategy = ledger.high_gap
    config = dict(bets_limit_enabled=False, bets_limit=120,
                  active_filter_enabled=False, active_filter_threshold='10',
                  active_filter_window=50, active_filter_limit=7)
    config.update(safety)
    assert strategy.configure('184', 1, '180', 150*100, min_stake=100,
                              **config)
    return strategy


def arm(ledger):
    tick(ledger, 1, '200')  # confirmed reference strictly greater than x184
    tick(ledger, 2, '1.20')  # completes wait, no retroactive wager
    assert ledger.high_gap.phase == 'betting'
    assert not ledger.high_gap.rounds


def test_bet_limit_ends_only_cycle_and_rearms_without_another_wager():
    ledger = make_ledger()
    high = setup(ledger, bets_limit_enabled=True, bets_limit=3)
    arm(ledger)
    for n in (3, 4, 5):
        tick(ledger, n, '1.20')
    assert high.enabled is True
    assert high.phase == 'waiting_reference'
    assert len(ledger.high_history) == 1
    cycle = ledger.high_history[0]
    assert cycle['outcome'] == 'bet_limit'
    assert cycle['bets_limit'] == 3
    assert cycle['net_cents'] == -450*100
    assert [r['stake_cents'] for r in cycle['rounds']] == [150*100]*3
    assert ledger.balance_cents == (10000-450)*100
    tick(ledger, 6, '1.10')  # no extra debit without a new reference
    assert ledger.balance_cents == (10000-450)*100
    tick(ledger, 7, '200')
    tick(ledger, 8, '1.10')
    assert high.phase == 'betting'
    tick(ledger, 9, '1.15')
    assert len(high.rounds) == 1
    assert len(ledger.high_history) == 1


def test_x_filter_is_strict_sliding_window_and_survives_reload():
    ledger = make_ledger()
    high = setup(ledger, active_filter_enabled=True, active_filter_window=2,
                 active_filter_threshold='10', active_filter_limit=2)
    arm(ledger)
    tick(ledger, 3, '11')
    assert high.active_filter_count == 1
    tick(ledger, 4, '10')  # x10 exactly is not strictly >10
    assert high.active_filter_count == 1
    tick(ledger, 5, '1.00')  # earlier x11 exits the 2-round window
    assert high.active_filter_count == 0
    tick(ledger, 6, '12')
    assert high.active_filter_count == 1
    restored = DemoLedger(**ledger.dump())
    assert restored.high_gap.active_filter_count == 1
    tick(restored, 7, '13')
    assert restored.high_gap.phase == 'waiting_reference'
    assert restored.high_gap.enabled
    assert restored.high_history[0]['outcome'] == 'active_filter_limit'
    assert restored.high_history[0]['net_cents'] == -5*150*100
    assert restored.high_history[0]['active_filter_window'] == 2
    tick(restored, 8, '1.00')
    assert restored.high_gap.phase == 'waiting_reference'


def test_safety_does_not_override_successful_x180_settlement():
    ledger = make_ledger()
    high = setup(ledger, bets_limit_enabled=True, bets_limit=1,
                 active_filter_enabled=True, active_filter_window=1,
                 active_filter_threshold='10', active_filter_limit=1)
    arm(ledger)
    tick(ledger, 3, '180')
    assert high.enabled and high.phase == 'waiting_reference'
    assert ledger.high_history[0]['outcome'] == 'recovered'
    assert ledger.high_history[0]['payout_cents'] == 180*150*100
    assert ledger.high_history[0]['net_cents'] == (180-1)*150*100


def test_legacy_configs_remain_disabled_and_bad_values_do_not_mutate():
    high = make_ledger().high_gap
    old = high.dump()
    assert not high.bets_limit_enabled and not high.active_filter_enabled
    invalid = [
        {'bets_limit_enabled': True, 'bets_limit': 0},
        {'bets_limit_enabled': 'true', 'bets_limit': 120},
        {'active_filter_enabled': True, 'active_filter_window': 2,
         'active_filter_limit': 3},
        {'active_filter_enabled': True, 'active_filter_window': True},
        {'active_filter_enabled': True, 'active_filter_threshold': '0.5'},
    ]
    for values in invalid:
        assert not high.configure('184', 1, '180', 150*100, min_stake=100, **values)
        assert high.dump() == old
    assert high.configure('184', 1, '180', 150*100, min_stake=100)
    assert not high.active_filter_enabled


def test_safety_closure_counted_in_factual_history_and_notified():
    ledger = make_ledger()
    high = setup(ledger, bets_limit_enabled=True, bets_limit=1)
    arm(ledger)
    before = high.dump()
    tick(ledger, 3, '1.10')
    completed = ledger.high_history[0]
    assert changed_events(before, high.dump(), completed) == ['betting', 'safe']
    notice = format_event('safe', high.dump(), 'KRW', completed)
    assert 'Сейв сработал' in notice or 'сейв сработал' in notice
    assert 'Ожидание нового опорного X' in notice
    assert '−' in notice or '-' in notice
    totals = _demo_stats(ledger, ledger.high_history)
    assert totals['total'] == 1 and totals['lost'] == 1
    assert totals['history_net_cents'] == -150*100


def test_api_validates_two_safety_controls_and_preserves_existing_data():
    async def run():
        gate = ReliableTelegramPasswordGate(None)
        gate.redis = FakeRedis(decode_responses=True)
        gate.authorized.add(42)
        ledger = make_ledger()
        ledger.high_gap.enabled = False
        gate.demo_ledgers[42] = ledger
        settings = dict(reference='184', wait_rounds=50, target='180', stake='150',
                        filter_enabled=True, filter_threshold='10', filter_limit='7',
                        bets_limit_enabled=True, bets_limit=120,
                        active_filter_enabled=True, active_filter_threshold='10',
                        active_filter_window=50, active_filter_limit=7)
        async def request(value):
            return await controls(signed(42), gate, 'fixture', 'POST',
                json.dumps({'action': 'high_gap_config', 'value': value}).encode())
        code, _, body = await request(settings)
        assert code == '200 OK', body
        assert ledger.high_gap.bets_limit == 120
        assert ledger.high_gap.active_filter_window == 50
        previous = ledger.high_gap.dump()
        for broken in (
                dict(settings, active_filter_window=5),
                dict(settings, bets_limit=True),
                dict(settings, active_filter_enabled='yes'),
                dict(settings, active_filter_threshold='0.7')):
            code, _, _ = await request(broken)
            assert code != '200 OK'
            assert ledger.high_gap.dump() == previous
        await gate.close()
    asyncio.run(run())
