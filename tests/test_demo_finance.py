"""DEMO cashflow history: one actual signed net result for each virtual bet."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

from fakeredis.aioredis import FakeRedis

from demo_persistence import DemoPersistence, ARCHIVE_CYCLES, ARCHIVE_ENTRIES
from luckyjet_reliable_gate import ReliableTelegramPasswordGate
from luckyjet_signal.demo_ledger import DemoLedger
from mini_app_history import history
from test_archive_four_tabs import signed


UTC = timezone.utc


def make_cycle(strategy, currency, rounds, ended, *, target='2'):
    total_stake = sum(row['stake_cents'] for row in rounds)
    payouts = sum(row.get('payout_cents', 0) for row in rounds)
    return {
        'strategy': strategy, 'currency': currency,
        'rounds': rounds, 'cashout_multiplier': target,
        'started_at': (ended - timedelta(minutes=5)).isoformat(),
        'ended_at': ended.isoformat(), 'outcome': 'recovered',
        'total_stake_cents': total_stake, 'payout_cents': payouts,
        'net_cents': payouts - total_stake,
        'balance_before_cents': 100_000_00,
        'balance_after_cents': 100_000_00 + payouts - total_stake,
    }


def row(n, at, stake, payout, balance_after, *, missing_time=False):
    result = {
        'round_id': f'round-{n}', 'coefficient': '1.57' if payout else '1.10',
        'stake_cents': stake, 'payout_cents': payout,
        'balance_after_cents': balance_after,
    }
    if not missing_time:
        result['observed_at'] = at.isoformat()
    return result


async def query(gate, uid=42, **kwargs):
    from urllib.parse import urlencode
    code, _, raw = await history(signed(uid), gate, 'fixture',
                                 urlencode({'view': 'demo_finance', **kwargs}))
    return code, json.loads(raw)


def test_three_strategies_exact_incomes_expenses_and_stable_archive_after_reset():
    async def run():
        now = datetime.now(UTC).replace(hour=6, minute=0, second=0, microsecond=0)
        redis = FakeRedis(decode_responses=True)
        gate = ReliableTelegramPasswordGate(None)
        gate.redis = redis
        gate.authorized.add(42)
        gate.authorized.add(43)

        classic = make_cycle('classic', 'USD', [
            row(1, now - timedelta(minutes=3), 1000, 0, 99000),
            row(2, now - timedelta(minutes=2), 2000, 4000, 101000),
        ], now)
        high = make_cycle('high_gap', 'USD', [
            row(3, now - timedelta(minutes=2), 2000, 3000, 101000),
        ], now + timedelta(seconds=1), target='1.50')
        target = make_cycle('target_streak', 'KRW', [
            row(4, now - timedelta(minutes=1), 15000, 0, 9985000),
            row(5, now, 52700, 82700, 10015000),
        ], now + timedelta(seconds=2), target='1.57')

        ledger = DemoLedger(currency='KRW', history=[classic], high_history=[high],
                            target_history=[target])
        gate.demo_ledgers[42] = ledger
        await gate._demo_storage.save(redis, 42, ledger)
        assert await redis.zcard(ARCHIVE_CYCLES + '42') == 3
        assert await redis.hlen(ARCHIVE_ENTRIES + '42') == 3

        code, data = await query(gate)
        assert code == '200 OK'
        assert data['view'] == 'demo_finance'
        assert data['total'] == 5  # five wagers; every cashout belongs to its stake
        assert len({op['id'] for op in data['items']}) == 5
        assert {op['strategy'] for op in data['items']} == {
            'classic', 'high_gap', 'target_streak'}
        assert all(op['type'] in ('income', 'expense') for op in data['items'])
        totals = {t['currency']: t for t in data['totals']}
        assert totals['USD']['income_cents'] == 3000
        assert totals['USD']['expense_cents'] == 1000
        assert totals['USD']['net_cents'] == 2000
        assert totals['KRW']['income_cents'] == 30000
        assert totals['KRW']['expense_cents'] == 15000
        assert totals['KRW']['net_cents'] == 15000
        # The gross x1.57 cashout includes the staked money. One wager,
        # one +30000 net result, while original stake/payout remain details.
        win = next(op for op in data['items']
                   if op['strategy'] == 'target_streak' and op['type'] == 'income')
        assert win['amount_cents'] == 30000
        assert win['net_cents'] == 30000
        assert win['stake_cents'] == 52700
        assert win['payout_cents'] == 82700
        assert win['balance_before_cents'] == 9985000
        assert win['balance_after_cents'] == 10015000
        assert all(op['time_exact'] for op in data['items'])

        _, filtered = await query(gate, strategy='target_streak', flow='income')
        assert filtered['total'] == 1
        assert filtered['items'][0]['amount_cents'] == 30000
        assert filtered['totals'][0]['net_cents'] == 15000  # flow does not change net

        _, expenses = await query(gate, flow='expense')
        assert expenses['total'] == 2
        assert all(op['type'] == 'expense' for op in expenses['items'])
        _, scoped = await query(gate, period='day', day=now.astimezone(
            timezone(timedelta(hours=10))).date().isoformat())
        assert scoped['total'] == 5

        # Reset DEMO is NOT income, expense or a new zero-sum transaction.
        ledger.reset(currency='RUB', balance_cents=5000000,
                     base_stake_cents=10000, reset_revision='new-reset')
        await gate._demo_storage.save(redis, 42, ledger)
        _, after_reset = await query(gate)
        assert after_reset['total'] == 5
        assert after_reset['totals'] == data['totals']
        assert await redis.zcard(ARCHIVE_CYCLES + '42') == 3
        _, isolated = await query(gate, uid=43)
        assert isolated['total'] == 0
        await gate.close()
    asyncio.run(run())


def test_older_cycle_marks_unknown_round_time_and_preserves_balance_explanations():
    async def run():
        now = datetime.now(UTC)
        gate = ReliableTelegramPasswordGate(None)
        gate.redis = FakeRedis(decode_responses=True)
        gate.authorized.add(42)
        cycle = make_cycle('classic', 'USD', [
            row(1, now - timedelta(minutes=1), 1000, 0, 99000,
                missing_time=True),
            row(2, now, 2000, 4000, 101000, missing_time=True),
        ], now)
        gate.demo_ledgers[42] = DemoLedger(history=[cycle])
        await gate._demo_storage.save(gate.redis, 42, gate.demo_ledgers[42])
        status, data = await query(gate)
        assert status == '200 OK'
        assert data['total'] == 2
        assert all(not op['time_exact'] for op in data['items'])
        assert all(op['time_basis'] == 'cycle_completion' for op in data['items'])
        win = next(op for op in data['items'] if op['step'] == 2)
        loss = next(op for op in data['items'] if op['step'] == 1)
        assert win['type'] == 'income' and win['net_cents'] == 2000
        assert win['amount_cents'] == 2000
        assert win['stake_cents'] == 2000 and win['payout_cents'] == 4000
        assert win['balance_before_cents'] == 99000
        assert win['balance_after_cents'] == 101000
        assert loss['type'] == 'expense' and loss['net_cents'] == -1000
        assert loss['balance_before_cents'] == 100000
        assert loss['balance_after_cents'] == 99000
        await gate.close()
    asyncio.run(run())


def test_active_cycles_included_and_completed_cycle_deduped():
    async def run():
        now = datetime.now(UTC)
        gate = ReliableTelegramPasswordGate(None)
        gate.redis = FakeRedis(decode_responses=True)
        gate.authorized.add(42)
        ledger = DemoLedger(currency='KRW', enabled=True, active=True,
                            balance_cents=9990000, cycle_started_at=now.isoformat(),
                            cycle_rounds=[row(6, now, 10000, 0, 9990000)])
        gate.demo_ledgers[42] = ledger
        _, first = await query(gate)
        assert first['total'] == 1
        assert first['items'][0]['cycle_status'] == 'active'
        assert first['totals'][0]['expense_cents'] == 10000
        assert first['totals'][0]['income_cents'] == 0
        assert await gate.redis.zcard(ARCHIVE_CYCLES + '42') == 0
        # Moving from active to a complete archived cycle must not show
        # both the provisional active record and the archived record.
        completed = make_cycle('classic', 'KRW', ledger.cycle_rounds[:], now)
        ledger.active = False
        ledger.cycle_rounds = []
        ledger.history.insert(0, completed)
        await gate._demo_storage.save(gate.redis, 42, ledger)
        _, second = await query(gate)
        assert second['total'] == 1
        assert second['items'][0]['cycle_status'] == 'recovered'
        await gate.close()
    asyncio.run(run())


def test_cashflow_pages_are_disjoint_and_totals_cover_full_selection():
    async def run():
        now = datetime.now(UTC)
        gate = ReliableTelegramPasswordGate(None)
        gate.redis = FakeRedis(decode_responses=True)
        gate.authorized.add(42)
        rounds = [row(i, now + timedelta(seconds=i), 1000, 0, 100000-i*1000)
                  for i in range(45)]
        last = rounds[-1]
        last['payout_cents'] = 46000
        last['balance_after_cents'] += 46000
        entry = make_cycle('high_gap', 'USD', rounds, now + timedelta(minutes=1))
        gate.demo_ledgers[42] = DemoLedger(high_history=[entry])
        await gate._demo_storage.save(gate.redis, 42, gate.demo_ledgers[42])
        _, first = await query(gate, page=0)
        _, second = await query(gate, page=1)
        assert first['page_size'] == 40
        assert first['total'] == second['total'] == 45
        assert len(first['items']) == 40 and len(second['items']) == 5
        assert not ({x['id'] for x in first['items']} & {x['id'] for x in second['items']})
        assert first['totals'] == second['totals']
        assert first['totals'][0]['income_cents'] == 45000
        assert first['totals'][0]['expense_cents'] == 44000
        assert first['totals'][0]['net_cents'] == 1000
        await gate.close()
    asyncio.run(run())


def test_invalid_finance_queries_are_rejected_without_mutation():
    async def run():
        gate = ReliableTelegramPasswordGate(None)
        gate.redis = FakeRedis(decode_responses=True)
        gate.authorized.add(42)
        for text in (
            'view=demo_finance&period=hack',
            'view=demo_finance&strategy=someone',
            'view=demo_finance&flow=reset',
            'view=demo_finance&period=day',
            'view=demo_finance&index=0',
            'view=cycles&period=7d',
            'view=demo_finance&flow=all&flow=expense',
        ):
            status, _, _ = await history(signed(42), gate, 'fixture', text)
            assert status == '400 Bad Request', text
        await gate.close()
    asyncio.run(run())



def test_single_net_entry_matches_visible_5000_x137_example():
    """One result is +1 850 ₩, not -5 000 ₩ plus +6 850 ₩."""
    async def run():
        now = datetime.now(UTC)
        gate = ReliableTelegramPasswordGate(None)
        gate.redis = FakeRedis(decode_responses=True)
        gate.authorized.add(42)
        cycle = make_cycle('target_streak', 'KRW', [
            {'round_id': 'winning-round', 'observed_at': now.isoformat(),
             'coefficient': '1.37', 'stake_cents': 500000,
             'payout_cents': 685000, 'balance_after_cents': 8485500}
        ], now, target='1.37')
        gate.demo_ledgers[42] = DemoLedger(currency='KRW', target_history=[cycle])
        await gate._demo_storage.save(gate.redis, 42, gate.demo_ledgers[42])
        status, data = await query(gate)
        assert status == '200 OK'
        assert data['total'] == 1
        op = data['items'][0]
        assert (op['type'], op['net_cents'], op['amount_cents']) == (
            'income', 185000, 185000)
        assert op['stake_cents'] == 500000 and op['payout_cents'] == 685000
        assert op['balance_before_cents'] == 8300500
        assert op['balance_after_cents'] == 8485500
        assert data['totals'][0]['income_cents'] == 185000
        assert data['totals'][0]['expense_cents'] == 0
        await gate.close()
    asyncio.run(run())


def test_four_strategies_positive_negative_even_and_filter_behavior():
    async def run():
        now = datetime.now(UTC)
        gate = ReliableTelegramPasswordGate(None)
        gate.redis = FakeRedis(decode_responses=True)
        gate.authorized.add(42)
        # KRW: +150 (#1), -100 (#2), +1850 (#3), -150 (#4)
        cycles = {
            'classic': make_cycle('classic', 'KRW', [
                row(1, now, 15000, 30000, 10015000)], now),
            'high_gap': make_cycle('high_gap', 'KRW', [
                row(2, now, 10000, 0, 10005000)], now),
            'target_streak': make_cycle('target_streak', 'KRW', [
                row(3, now, 500000, 685000, 10190000)], now, target='1.37'),
            'recovery_streak': make_cycle('recovery_streak', 'KRW', [
                row(4, now, 15000, 0, 10175000),
                row(5, now, 15000, 15000, 10175000)], now, target='1.32'),
        }
        ledger = DemoLedger(currency='KRW', history=[cycles['classic']],
                            high_history=[cycles['high_gap']],
                            target_history=[cycles['target_streak']],
                            recovery_history=[cycles['recovery_streak']])
        gate.demo_ledgers[42] = ledger
        await gate._demo_storage.save(gate.redis, 42, ledger)
        _, results = await query(gate)
        assert results['total'] == 5
        assert {op['strategy'] for op in results['items']} == set(cycles)
        assert len({op['id'] for op in results['items']}) == 5
        assert {op['type'] for op in results['items']} == {'income','expense','even'}
        assert all(op['net_cents'] == op['payout_cents'] - op['stake_cents']
                   for op in results['items'])
        assert results['totals'][0]['income_cents'] == (150 + 1850)*100
        assert results['totals'][0]['expense_cents'] == (100 + 150)*100
        assert results['totals'][0]['net_cents'] == 1750*100
        _, plus = await query(gate, flow='income')
        _, minus = await query(gate, flow='expense')
        assert plus['total'] == 2 and minus['total'] == 2
        assert results['totals'] == plus['totals'] == minus['totals']
        _, only_four = await query(gate, strategy='recovery_streak')
        assert only_four['total'] == 2
        assert only_four['totals'][0]['net_cents'] == -150*100
        await gate.close()
    asyncio.run(run())
