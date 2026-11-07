"""Unresettable, read-only four-tab archive sourced from independent history.

Never reads user history reset markers. Never writes or deletes source data or
DEMO ledgers. Source-game completeness is not assumed when timestamps are absent.
"""
import hashlib
import json
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from archive_integrity import archive_day_evidence
from coefficient_stats import THRESHOLDS
from demo_persistence import ARCHIVE_CYCLES, ARCHIVE_ENTRIES
from round_archive import STREAM_KEY

TZ = ZoneInfo('Asia/Vladivostok')
MAX_DAY_EVENTS = 20000
MAX_STATS_CYCLES = 20000
ARCHIVE_PAGE_SIZE = 10

VIEWS = {
    'archive_days', 'archive_day',
    'archive_high_days', 'archive_high_day', 'archive_high_item',
    'archive_cycles', 'archive_cycle', 'archive_demo_stats',
    'archive_max_steps', 'archive_max_step_day',
}


def _decode(value):
    return value.decode('utf-8') if isinstance(value, bytes) else str(value)


def _day_bounds(day):
    start = datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=TZ)
    return int(start.timestamp() * 1000), int((start + timedelta(days=1)).timestamp() * 1000)


async def _source_days(redis, page):
    first = await redis.xrange(STREAM_KEY, count=1)
    if not first:
        return [], 0
    first_ms = int(_decode(first[0][0]).split('-')[0])
    earliest = datetime.fromtimestamp(first_ms / 1000, TZ).date()
    today = datetime.now(TZ).date()
    total = max(0, (today - earliest).days + 1)
    dates = [today - timedelta(days=i) for i in range(page * 10, min(total, page * 10 + 10))]
    days = []
    for date in dates:
        day = date.isoformat()
        lower, upper = _day_bounds(day)
        present = await redis.xrange(STREAM_KEY, min=f'{lower}-0', max=f'({upper}-0', count=1)
        days.append({'day': day, 'partial': True, 'has_records': bool(present)})
    return days, total


async def _source_events(redis, day):
    lower, upper = _day_bounds(day)
    events = await redis.xrange(
        STREAM_KEY, min=f'{lower}-0', max=f'({upper}-0', count=MAX_DAY_EVENTS + 1,
    )
    if len(events) > MAX_DAY_EVENTS:
        return None
    return [(_decode(event_id), {_decode(k): _decode(v) for k, v in fields.items()})
            for event_id, fields in events]


def _source_summary(events):
    if events is None:
        return None, None
    summary = {'total': 0, 'lt2': 0, 'lt2_tracking': 1,
               'new_threshold_tracking': 1, 'partial': 1}
    summary.update({str(t): 0 for t in THRESHOLDS})
    longest = streak = 0
    for _id, record in events:
        if record.get('type') != 'round':
            streak = 0
            continue
        try:
            coef = float(record['coefficient'])
        except (ValueError, TypeError, KeyError):
            streak = 0
            continue
        if not math.isfinite(coef) or coef < 1:
            streak = 0
            continue
        summary['total'] += 1
        if not record.get('source_time'):
            summary['time_uncertain'] = 1
        if coef < 2:
            streak += 1
            longest = max(longest, streak)
            summary['lt2'] += 1
        else:
            streak = 0
        for t in THRESHOLDS:
            if coef > t:
                summary[str(t)] += 1
    return summary, longest


def _high_events(events, day):
    """Count between observed highs, never invent cross-day or gap counts."""
    if events is None:
        return None
    highs = []
    since = 0
    previous = None
    certain = False
    for stream_id, record in events:
        if record.get('type') != 'round':
            certain = False
            previous = None
            since = 0
            continue
        try:
            coefficient = float(record.get('coefficient', ''))
            if not math.isfinite(coefficient) or coefficient < 1:
                raise ValueError('invalid')
        except (ValueError, TypeError):
            certain = False
            previous = None
            since = 0
            continue
        since += 1
        if coefficient > 100:
            event = {
                'id': hashlib.sha256(stream_id.encode()).hexdigest()[:16],
                'round_id': record.get('round_id', ''),
                'day': day, 'stamp': int(stream_id.split('-')[0]) / 1000,
                'value': coefficient,
                'rounds': since if certain and previous is not None else None,
                'previous': previous, 'uncertain': not record.get('source_time'),
            }
            highs.append(event)
            previous = {'value': coefficient, 'stamp': event['stamp']}
            certain = True
            since = 0
    return list(reversed(highs))


def _cycle_data(raw):
    if raw is None:
        return None
    try:
        record = json.loads(_decode(raw))
    except (TypeError, ValueError):
        return None
    return record if isinstance(record, dict) else None


async def _archive_cycles(redis, uid, page, day=None):
    key = ARCHIVE_CYCLES + str(uid)
    entries_key = ARCHIVE_ENTRIES + str(uid)
    if day:
        start, end = _day_bounds(day)
        upper = f'({end / 1000}'
        lower = str(start / 1000)
        count = await redis.zcount(key, lower, upper)
        ids = await redis.zrevrangebyscore(key, upper, lower,
                                           start=page * ARCHIVE_PAGE_SIZE,
                                           num=ARCHIVE_PAGE_SIZE)
    else:
        count = await redis.zcard(key)
        ids = await redis.zrevrange(key, page * ARCHIVE_PAGE_SIZE,
                                     page * ARCHIVE_PAGE_SIZE + ARCHIVE_PAGE_SIZE - 1)
    raws = await redis.hmget(entries_key, ids) if ids else []
    from mini_app_history import _cycle
    items = []
    for identifier, raw in zip(ids, raws):
        entry = _cycle_data(raw)
        if entry is not None:
            result = _cycle(entry, 0)
            result.pop('index', None)
            result['id'] = _decode(identifier)
            items.append(result)
    return {'view': 'archive_cycles', 'total': count, 'page': page,
            'items': items, 'day': day}


async def _archive_demo_stats(redis, uid, day=None):
    key = ARCHIVE_CYCLES + str(uid)
    count = await redis.zcard(key) if not day else None
    if count is not None and count > MAX_STATS_CYCLES:
        return {'view': 'archive_demo_stats', 'truncated': True, 'total': count, 'day': day}
    from mini_app_history import _demo_stats
    page = 0
    records = []
    while True:
        part = await _archive_cycles(redis, uid, page, day)
        if not part['items']:
            break
        records.extend(part['items'])
        if len(records) > MAX_STATS_CYCLES:
            return {'view': 'archive_demo_stats', 'truncated': True,
                    'total': part['total'], 'day': day}
        if len(records) >= part['total']:
            break
        page += 1
    groups = {}
    for record in records:
        code = record.get('currency') or 'USD'
        group = groups.setdefault(code, [])
        group.append(record)
    result = {}
    for code, group in groups.items():
        good = [r for r in group if r.get('net_cents') is not None
                and r.get('outcome') in {'recovered', 'insufficient_balance'}]
        result[code] = {
            'total': len(group),
            'recovered': sum(r.get('outcome') == 'recovered' for r in group),
            'lost': sum(r.get('outcome') == 'insufficient_balance' for r in group),
            'incomplete': sum(r.get('outcome') == 'incomplete_data' for r in group),
            'stopped': sum(r.get('outcome') == 'stopped_by_user' for r in group),
            'history_net_cents': sum(int(r['net_cents']) for r in good),
            'average_steps': round(sum(int(r.get('steps') or 0) for r in good) / len(good), 1) if good else 0,
        }
    return {'view': 'archive_demo_stats', 'day': day,
            'total': len(records), 'currencies': result, 'truncated': False}


async def archive_history(redis, uid, view, *, day=None, page=0, item_id=None):
    if view in {'archive_days', 'archive_high_days', 'archive_max_steps'}:
        days, total = await _source_days(redis, page)
        if view == 'archive_high_days':
            for record in days:
                events = await _source_events(redis, record['day'])
                record['incomplete'] = events is None or not record['has_records']
                if events is not None:
                    record['count'] = len(_high_events(events, record['day']))
        if view == 'archive_max_steps':
            for record in days:
                events = await _source_events(redis, record['day'])
                _summary, streak = _source_summary(events)
                record['steps'] = streak if events is not None and record['has_records'] else None
        return {'view': view, 'days': days, 'total': total, 'page': page}
    if view in {'archive_day', 'archive_high_day', 'archive_high_item', 'archive_max_step_day'}:
        events = await _source_events(redis, day)
        if view == 'archive_day':
            summary, streak = _source_summary(events)
            evidence = await archive_day_evidence(redis, day)
            return {'view': view, 'day': day, 'summary': summary,
                    'integrity': evidence, 'truncated': events is None,
                    'max_low_streak': streak}
        if view == 'archive_max_step_day':
            _summary, streak = _source_summary(events)
            return {'view': view, 'day': day, 'steps': streak,
                    'truncated': events is None,
                    'incomplete': events is None or not events}
        highs = _high_events(events, day)
        if view == 'archive_high_day':
            return {'view': view, 'day': day, 'total': len(highs) if highs is not None else 0,
                    'page': page, 'truncated': highs is None,
                    'items': highs[page * ARCHIVE_PAGE_SIZE:(page + 1) * ARCHIVE_PAGE_SIZE]
                    if highs is not None else []}
        match = next((entry for entry in (highs or []) if entry['id'] == item_id), None)
        return {'view': view, 'item': match, 'day': day, 'not_found': match is None}
    if view == 'archive_cycles':
        return await _archive_cycles(redis, uid, page, day)
    if view == 'archive_cycle':
        raw = await redis.hget(ARCHIVE_ENTRIES + str(uid), item_id)
        entry = _cycle_data(raw)
        if not entry:
            return {'view': view, 'item': None, 'not_found': True}
        from mini_app_history import _cycle
        item = _cycle(entry, 0, detailed=True)
        item.pop('index', None)
        item['id'] = item_id
        return {'view': view, 'item': item}
    if view == 'archive_demo_stats':
        return await _archive_demo_stats(redis, uid, day)
    raise ValueError('Unknown archive view')
