"""Four isolated DEMO strategy tiles never infer net from the shared balance."""
from decimal import Decimal

from luckyjet_signal.demo_ledger import DemoLedger
from strategy_tiles import strategy_tile_snapshot


def record(stake, payout=0):
    return {'stake_cents': stake, 'payout_cents': payout, 'coefficient': '1.38'}


def test_four_tiles_report_each_strategy_net_without_shared_balance_delta():
    ledger = DemoLedger(currency='KRW', balance_cents=10_000_000)
    ledger.enabled = True
    ledger.active = True
    ledger.low_streak = 3
    ledger.trigger_streak = 5
    ledger.cycle_rounds = [record(100_000)]
    ledger.next_stake_cents = 200_000
    ledger.high_gap.enabled = True
    ledger.high_gap.phase = 'waiting'
    ledger.high_gap.elapsed = 78
    ledger.high_gap.wait_rounds = 100
    ledger.high_history = [{'net_cents': 95_000, 'rounds': [record(100_000, 195_000)]}]
    ledger.target_streak.enabled = True
    ledger.target_streak.phase = 'betting'
    ledger.target_streak.next_stake_cents = 250_000
    ledger.target_streak.rounds = [record(100_000), record(250_000)]
    ledger.recovery_streak.enabled = True
    ledger.recovery_streak.phase = 'waiting'
    ledger.recovery_streak.streak = 2
    ledger.recovery_streak.trigger = 4
    ledger.recovery_streak.rounds = [record(100_000)]
    ledger.balance_cents = 22_000_000  # Other strategies' money cannot affect isolated net.

    tiles = strategy_tile_snapshot(ledger)
    assert [t['id'] for t in tiles] == [1, 2, 3, 4]
    assert [t['net_cents'] for t in tiles] == [-100_000, 95_000, -350_000, -100_000]
    assert [t['result_scope'] for t in tiles] == ['current', 'last', 'current', 'current']
    assert tiles[0]['stake_cents'] == 200_000
    assert tiles[1]['progress'] == '78/100'
    assert tiles[2]['stake_cents'] == 250_000
    assert tiles[3]['progress'] == '2/4'


def test_stop_shows_last_exact_net_and_next_cycle_resets_display():
    ledger = DemoLedger(currency='KRW')
    ledger.enabled = True
    ledger.active = True
    ledger.cycle_rounds = [record(100_000)]
    ledger.cycle_stake_cents = 100_000
    ledger.finish_active('stopped_by_user')
    ledger.enabled = False

    after_stop = strategy_tile_snapshot(ledger)[0]
    assert after_stop['enabled'] is False
    assert after_stop['net_cents'] == -100_000
    assert after_stop['last_action'] == 'Проигрыш'

    ledger.enabled = True
    ledger.active = True
    ledger.cycle_rounds = []
    assert strategy_tile_snapshot(ledger)[0]['result_scope'] == 'last'
    # Once the first NEW wager settles, the open-cycle net replaces the old result.
    ledger.cycle_rounds = [record(100_000, 200_000)]
    assert strategy_tile_snapshot(ledger)[0]['net_cents'] == 100_000


def test_no_invented_money_after_gap_and_reset():
    ledger = DemoLedger(currency='KRW')
    ledger.history = [{'net_cents': None, 'outcome': 'incomplete_data',
                       'rounds': [record(100_000)]}]
    ledger.high_history = [{'outcome': 'incomplete_data', 'net_cents': None}]
    ledger.target_streak.rounds = [{'stake_cents': 100_000}]  # missing payout
    rows = strategy_tile_snapshot(ledger)
    assert all(t['net_cents'] is None for t in rows)
    assert rows[0]['last_action'] == 'Проигрыш'
    ledger.reset(currency='KRW', balance_cents=10_000_000,
                 base_stake_cents=100_000, reset_revision='new')
    assert all(t['net_cents'] is None for t in strategy_tile_snapshot(ledger))


def test_recovery_four_real_rounds_and_last_actual_minus_620_krw():
    ledger = DemoLedger(currency='KRW', balance_cents=10_000_000)
    strategy = ledger.recovery_streak
    strategy.enabled = True
    strategy.target = Decimal('1.38')
    strategy.trigger = 2
    strategy.stake_cents = 100_000
    strategy.next_stake_cents = 100_000
    for i, value in enumerate(['1.10', '1.20', '1.10'], 1):
        ledger.process_archived_round(str(i), value, stream_id=f'{i}-0')
    pending = strategy_tile_snapshot(ledger)[3]
    assert pending['progress'] == '0/2'
    assert pending['net_cents'] == -100_000
    assert pending['last_action'] == 'Проигрыш'
    assert strategy.next_stake_cents == 100_000
    for i, value in enumerate(['1.10', '1.20', '1.38'], 4):
        ledger.process_archived_round(str(i), value, stream_id=f'{i}-0')
    finished = strategy_tile_snapshot(ledger)[3]
    assert finished['net_cents'] == -62_000
    assert finished['result_scope'] == 'last'
    assert finished['last_action'] == 'Выигрыш'
    assert finished['progress'] == '0/2'
