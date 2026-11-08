"""Read-only DEMO income/expense statement from completed and active journals.

Never changes source rounds, cycle history, strategy configuration or balances.
Cycle archive is append-only across DEMO resets. A round's gross cashout is
income; the original wager is a separate debit, never double-counted.

Legacy round timestamps may be absent. Do not infer the exact step time:
display the cycle's completion time only as an explicitly marked approximation.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from demo_persistence import ARCHIVE_CYCLES, ARCHIVE_ENTRIES
from luckyjet_signal.demo_money import CURRENCIES

TZ = ZoneInfo('Asia/Vladivostok')
PAGE_SIZE = 40
MAX_ARCHIVED_CYCLES = 3000
ARCHIVE_BATCH = 150
STRATEGIES = frozenset(('classic', 'high_gap', 'target_streak', 'recovery_streak'))
PERIODS = frozenset(('all', 'today', '7d', '30d', 'day'))
FLOWS = frozenset(('all', 'income', 'expense'))


def _date(raw):
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
    except (TypeError, ValueError, OverflowError):
        return None
    if value.tzinfo is None:
        return None
    return value.astimezone(timezone.utc)


def _identity(entry):
    value = json.dumps(entry, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _active_entries(ledger):
    """Current partial journals: still include each confirmed virtual debit."""
    classic = ledger
    if classic.active and classic.cycle_rounds:
        yield {
            'strategy': 'classic', 'currency': ledger.currency,
            'started_at': classic.cycle_started_at,
            'ended_at': None, 'rounds': classic.cycle_rounds[:],
            'cashout_multiplier': '2',
            'cycle_status': 'active',
        }
    high = ledger.high_gap
    if high.rounds:
        yield {
            'strategy': 'high_gap', 'currency': ledger.currency,
            'started_at': high.started_at,
            'ended_at': None, 'rounds': high.rounds[:],
            'cashout_multiplier': str(high.target),
            'cycle_status': 'active',
        }
    for strategy_name, target in (
            ('target_streak', ledger.target_streak),
            ('recovery_streak', ledger.recovery_streak)):
        if target.rounds:
            yield {
                'strategy': strategy_name, 'currency': ledger.currency,
                'started_at': target.started_at,
                'ended_at': None, 'rounds': target.rounds[:],
                'cashout_multiplier': str(target.target),
                'cycle_status': 'active',
            }


async def demo_finance(redis, uid, ledger, *, page=0, period='all',
                       strategy='all', flow='all', day=None, now=None):
    """One chronological, per-currency, per-operation feed (40 rows per page).

    Guard exceptionally large historical data sets instead of presenting
    incomplete totals as complete. This endpoint never records a reset as
    income or an expense, and only emits actual saved stakes/payouts.
    """
    if period not in PERIODS or strategy not in (STRATEGIES | {'all'}) or flow not in FLOWS:
        raise ValueError('Invalid finance filters')
    if period == 'day' and not day:
        raise ValueError('Date required for daily finance statement')
    today = (now or datetime.now(timezone.utc)).astimezone(TZ).date()
    if period == 'day':
        chosen = datetime.strptime(day, '%Y-%m-%d').date()
        if chosen > today:
            raise ValueError('Future finance date')
        first = last = chosen
    elif period == 'all':
        first, last = None, today
    else:
        days = {'today': 1, '7d': 7, '30d': 30}[period]
        first, last = today - timedelta(days=days - 1), today

    entries = []
    seen = set()
    key = ARCHIVE_CYCLES + str(int(uid))
    count = await redis.zcard(key)
    to_scan = min(count, MAX_ARCHIVED_CYCLES)
    for offset in range(0, to_scan, ARCHIVE_BATCH):
        ids = await redis.zrevrange(key, offset,
                                    min(offset + ARCHIVE_BATCH, to_scan) - 1)
        if not ids:
            break
        raw_items = await redis.hmget(ARCHIVE_ENTRIES + str(int(uid)), ids)
        for identifier, raw in zip(ids, raw_items):
            if raw is None:
                continue
            try:
                entry = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(entry, dict):
                continue
            ident = identifier.decode('ascii') if isinstance(identifier, bytes) else str(identifier)
            if ident in seen:
                continue
            seen.add(ident)
            entries.append((ident, entry))

    # Pending or recent completed entries can exist before the next atomic save,
    # and must be counted exactly once if also present in the archive index.
    for entry in (ledger.history + ledger.high_history + ledger.target_history +
                  ledger.recovery_history + ledger.pending_archive_cycles):
        if not isinstance(entry, dict):
            continue
        ident = _identity(entry)
        if ident not in seen:
            entries.append((ident, entry))
            seen.add(ident)
    for entry in _active_entries(ledger):
        ident = 'active:' + entry['strategy'] + ':' + str(entry.get('started_at') or 'unknown')
        entries.append((ident, entry))

    operations = []
    omitted_unknown_times = 0
    invalid_rows = 0
    for identifier, cycle in entries:
        code = cycle.get('currency', 'USD')
        kind = cycle.get('strategy', 'classic')
        if code not in CURRENCIES or kind not in STRATEGIES or (
                strategy != 'all' and strategy != kind):
            continue
        close_at = _date(cycle.get('ended_at'))
        for i, round_row in enumerate(cycle.get('rounds') or []):
            if not isinstance(round_row, dict):
                invalid_rows += 1
                continue
            exact_at = _date(round_row.get('observed_at'))
            # No exact step timestamp in older journals: only cycle time is
            # known. Show the limitation and never call that the round time.
            occurred = exact_at or close_at or _date(cycle.get('started_at'))
            if occurred is None:
                omitted_unknown_times += 1
                if first is not None:
                    continue
            if occurred is not None:
                d = occurred.astimezone(TZ).date()
                if (first is not None and d < first) or d > last:
                    continue
            try:
                stake = int(round_row.get('stake_cents') or 0)
                payout = int(round_row.get('payout_cents') or 0)
                after = round_row.get('balance_after_cents')
                balance_after = int(after) if after is not None else None
            except (TypeError, ValueError, OverflowError):
                invalid_rows += 1
                continue
            if stake < 0 or payout < 0 or (stake == 0 and payout == 0):
                invalid_rows += 1
                continue
            base = {
                'cycle_id': identifier, 'strategy': kind, 'currency': code,
                'step': i + 1,
                'round_id': str(round_row.get('round_id') or ''),
                'coefficient': str(round_row.get('coefficient') or ''),
                'cashout_multiplier': str(cycle.get('cashout_multiplier') or (
                    '2' if kind == 'classic' else '')),
                'time': occurred.isoformat() if occurred is not None else None,
                'time_exact': exact_at is not None,
                'time_basis': ('round_receipt' if exact_at is not None
                               else 'cycle_completion' if close_at else 'cycle_start'
                               if occurred is not None else 'unknown'),
                'cycle_status': cycle.get('cycle_status') or cycle.get('outcome') or 'unknown',
            }
            if stake > 0:
                debit_after = balance_after - payout if balance_after is not None else None
                operations.append({
                    **base, 'id': identifier + ':' + str(i) + ':stake',
                    'type': 'expense', 'amount_cents': stake,
                    'balance_after_cents': debit_after,
                    'balance_before_cents': debit_after + stake if debit_after is not None else None,
                    '_order': (i, 0),
                })
            if payout > 0:
                operations.append({
                    **base, 'id': identifier + ':' + str(i) + ':payout',
                    'type': 'income', 'amount_cents': payout,
                    'balance_after_cents': balance_after,
                    'balance_before_cents': (balance_after - payout
                                             if balance_after is not None else None),
                    '_order': (i, 1),
                })

    totals = defaultdict(lambda: {'currency': '', 'income_cents': 0, 'expense_cents': 0,
                                  'net_cents': 0, 'operations': 0})
    for record in operations:
        group = totals[record['currency']]
        group['currency'] = record['currency']
        group['operations'] += 1
        if record['type'] == 'income':
            group['income_cents'] += record['amount_cents']
            group['net_cents'] += record['amount_cents']
        else:
            group['expense_cents'] += record['amount_cents']
            group['net_cents'] -= record['amount_cents']

    if flow != 'all':
        operations = [r for r in operations if r['type'] == flow]
    # Stable ordering: true recorded time first; when only a cycle timestamp
    # survived, step index orders that cycle's own actions, not other cycles.
    operations.sort(key=lambda r: (
        r['time'] or '', r['cycle_id'], r['_order'][0], r['_order'][1]),
        reverse=True)
    total = len(operations)
    result_rows = operations[page * PAGE_SIZE: (page + 1) * PAGE_SIZE]
    for record in result_rows:
        record.pop('_order', None)

    return {
        'view': 'demo_finance', 'page': page, 'page_size': PAGE_SIZE,
        'total': total, 'items': result_rows, 'period': period,
        'day': day if period == 'day' else None, 'strategy': strategy,
        'flow': flow,
        'totals': [totals[c] for c in ('KRW', 'RUB', 'USD') if c in totals],
        'truncated': count > MAX_ARCHIVED_CYCLES,
        'scanned_archived_cycles': to_scan,
        'total_archived_cycles': count,
        'unknown_time_rows': omitted_unknown_times,
        'invalid_rows': invalid_rows,
        'time_note': 'Дата старого шага может соответствовать окончанию цикла, а не точному времени ставки.',
        'basis': 'Только записанные виртуальные ставки и выплаты; сбросы DEMO не учитываются.',
    }
