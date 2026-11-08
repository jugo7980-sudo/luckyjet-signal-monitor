"""Authenticated, read-only history for Telegram Mini App.

No account, bot token or Redis key is ever exposed to the browser.
"""
import json
import math
import re
from datetime import datetime, timedelta
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

from archive_integrity import archive_day_evidence
from mini_app_factual_archive import VIEWS as FACTUAL_VIEWS, archive_history
from round_archive import STREAM_KEY
from coefficient_stats import THRESHOLDS
from mini_app_api import json_response, telegram_user
from luckyjet_signal.demo_ledger import DemoLedger, DEFAULT_BALANCE_CENTS, MAX_HISTORY

TZ = ZoneInfo('Asia/Vladivostok')
VIEWS = {'days', 'day', 'high_days', 'high_day', 'high_item',
         'cycles', 'cycle', 'high_cycles', 'high_cycle', 'demo_stats',
         'max_steps', 'max_step_day', 'archive'} | FACTUAL_VIEWS
DAY_PATTERN = re.compile(r'^\d{4}-\d{2}-\d{2}$')
ITEM_PATTERN = re.compile(r'^[0-9a-f]{16}$')
CURSOR_PATTERN = re.compile(r'^\d{1,16}-\d{1,16}$')
ARCHIVE_PAGE_SIZE = 50
MAX_RESET_EVENTS = 20000


def _string(value):
    return value.decode('utf-8') if isinstance(value, bytes) else value


def _date(value):
    if not isinstance(value, str) or not DAY_PATTERN.fullmatch(value):
        raise ValueError('Invalid day')
    parsed = datetime.strptime(value, '%Y-%m-%d').date()
    if parsed > datetime.now(TZ).date():
        raise ValueError('Future day')
    return value


def _number(value, maximum):
    if not isinstance(value, str) or not value.isdecimal() or len(value) > 5:
        raise ValueError('Invalid number')
    result = int(value)
    if result > maximum:
        raise ValueError('Number out of bounds')
    return result


def _entry(raw):
    if raw is None:
        return None
    value = json.loads(_string(raw))
    previous = value.get('previous')
    if isinstance(previous, str):
        try:
            previous = json.loads(previous)
        except (ValueError, TypeError):
            previous = None
    return {
        'id': value.get('id'),
        'day': value.get('day'),
        'stamp': value.get('stamp'),
        'value': value.get('value'),
        'rounds': value.get('rounds'),
        'uncertain': bool(value.get('uncertain')),
        'previous': previous,
    }


def _cycle(entry, index, detailed=False):
    result = {
        'index': index,
        'currency': entry.get('currency', 'USD'),
        'started_at': entry.get('started_at'),
        'ended_at': entry.get('ended_at'),
        'outcome': entry.get('outcome'),
        'net_cents': entry.get('net_cents'),
        'steps': len(entry.get('rounds') or []),
        'strategy': entry.get('strategy', 'classic'),
        'cashout_multiplier': entry.get('cashout_multiplier', '2'),
        'reference_threshold': entry.get('reference_threshold'),
        'reference_coefficient': entry.get('reference_coefficient'),
        'wait_rounds': entry.get('wait_rounds'),
    }
    if detailed:
        result.update({
            'rounds': entry.get('rounds') or [],
            'total_stake_cents': entry.get('total_stake_cents'),
            'payout_cents': entry.get('payout_cents'),
            'balance_before_cents': entry.get('balance_before_cents'),
            'balance_after_cents': entry.get('balance_after_cents'),
        })
    return result


def _demo_stats(ledger, records=None):
    records = list(ledger.history if records is None else records)
    valid = [r for r in records if r.get('outcome') in
             {'recovered', 'insufficient_balance'} and r.get('net_cents') is not None]
    return {
        'currency': ledger.currency,
        'total': len(records),
        'maximum': MAX_HISTORY,
        'recovered': sum(r.get('outcome') == 'recovered' for r in records),
        'lost': sum(r.get('outcome') == 'insufficient_balance' for r in records),
        'incomplete': sum(r.get('outcome') == 'incomplete_data' for r in records),
        'stopped': sum(r.get('outcome') == 'stopped_by_user' for r in records),
        'since_reset_cents': ledger.balance_cents - ledger.initial_balance_cents,
        'history_net_cents': sum(int(r['net_cents']) for r in valid),
        'average_steps': round(sum(len(r.get('rounds') or []) for r in valid) / len(valid), 1) if valid else 0,
    }




def _reset_info(raw):
    """A corrupt marker must not delete data or invalidate history APIs."""
    if not raw:
        return None
    try:
        marker = json.loads(_string(raw))
        if not isinstance(marker, dict):
            return None
        if not isinstance(marker.get('day'), str) or not DAY_PATTERN.fullmatch(marker['day']):
            return None
        if not isinstance(marker.get('stream_id'), str) or not CURSOR_PATTERN.fullmatch(marker['stream_id']):
            return None
        if type(marker.get('at_ms')) is not int or marker['at_ms'] <= 0:
            return None
        return marker
    except (ValueError, TypeError):
        return None


def _after_cycle_reset(entry, marker):
    if not marker:
        return True
    value = entry.get('ended_at') or entry.get('started_at')
    if isinstance(value, (int, float)):
        return value * (1000 if value < 1e11 else 1) > marker['at_ms']
    try:
        stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            return False
        return stamp.timestamp() * 1000 > marker['at_ms']
    except (ValueError, TypeError, OverflowError):
        return False


def _distribution_bucket(value):
    """Non-overlapping coefficient ranges: 1.5–1.99, 2–2.99, ..., 150–150.99."""
    if not math.isfinite(value) or value < 1:
        return None
    if value < 1.5:
        return 'below_1_5'
    if value < 2:
        return '1.5'
    if value < 151:
        return str(int(value))
    return 'above_150'


def _new_distribution():
    result = {'below_1_5': 0, '1.5': 0, 'above_150': 0}
    result.update({str(n): 0 for n in range(2, 151)})
    return result


async def _day_distribution(redis, day, marker=None):
    """Read-only stream scan, bounded to avoid heavy loads or fabricated totals."""
    start = datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=TZ)
    end = start + timedelta(days=1)
    first = f'{int(start.timestamp() * 1000)}-0'
    if marker:
        reset_id = marker['stream_id']
        if tuple(map(int, reset_id.split('-'))) >= tuple(map(int, first.split('-'))):
            first = '(' + reset_id
    events = await redis.xrange(STREAM_KEY, min=first,
                                max=f'({int(end.timestamp() * 1000)}-0',
                                count=MAX_RESET_EVENTS + 1)
    if len(events) > MAX_RESET_EVENTS:
        return {'complete_scan': False, 'counts': None, 'total': None}
    counts = _new_distribution()
    total = 0
    for _event_id, raw in events:
        fields = {_string(k): _string(v) for k, v in raw.items()}
        if fields.get('type') != 'round':
            continue
        try:
            value = float(fields.get('coefficient', ''))
        except (TypeError, ValueError, OverflowError):
            continue
        bucket = _distribution_bucket(value)
        if bucket is not None:
            counts[bucket] += 1
            total += 1
    return {'complete_scan': True, 'counts': counts, 'total': total}


async def _after_reset_day(redis, day, marker):
    """Rebuild post-reset visible counters from immutable source receipts."""
    start = datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=TZ)
    end = start + timedelta(days=1)
    start_id = f'{int(start.timestamp() * 1000)}-0'
    marker_id = marker['stream_id']
    if tuple(map(int, marker_id.split('-'))) >= tuple(map(int, start_id.split('-'))):
        start_id = '(' + marker_id
    events = await redis.xrange(
        STREAM_KEY, min=start_id, max=f'({int(end.timestamp() * 1000)}-0',
        count=MAX_RESET_EVENTS + 1,
    )
    if len(events) > MAX_RESET_EVENTS:
        return None, None
    summary = {'total': 0, 'lt2': 0, 'lt2_tracking': 1,
               'new_threshold_tracking': 1, 'partial': 1, 'reset_view': 1}
    for threshold in THRESHOLDS:
        summary[str(threshold)] = 0
    longest, streak = 0, 0
    for _event_id, raw in events:
        fields = {_string(k): _string(v) for k, v in raw.items()}
        if fields.get('type') != 'round':
            streak = 0
            continue
        try:
            value = float(str(fields.get('coefficient', '')))
        except (TypeError, ValueError, OverflowError):
            streak = 0
            continue
        if not math.isfinite(value) or value < 1:
            streak = 0
            continue
        summary['total'] += 1
        if not fields.get('source_time'):
            summary['time_uncertain'] = 1
        if value < 2:
            summary['lt2'] += 1
            streak += 1
            longest = max(longest, streak)
        else:
            streak = 0
        for threshold in THRESHOLDS:
            if value > threshold:
                summary[str(threshold)] += 1
    return summary, longest


async def archive_page(redis, day, cursor=None):
    """Read recorded source events; never mutate the append-only archive.

    Stream IDs mark Redis receipt times. Source times remain separate and may
    be absent, so these records alone do not prove complete third-party history.
    """
    start = datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=TZ)
    end = start + timedelta(days=1)
    first = f'{int(start.timestamp() * 1000)}-0'
    boundary = f'({int(end.timestamp() * 1000)}-0'
    if cursor is not None:
        if not CURSOR_PATTERN.fullmatch(cursor):
            raise ValueError('Invalid archive cursor')
        boundary = f'({cursor}'
    events = await redis.xrevrange(
        STREAM_KEY, max=boundary, min=first, count=ARCHIVE_PAGE_SIZE + 1,
    )
    has_more = len(events) > ARCHIVE_PAGE_SIZE
    chosen = events[:ARCHIVE_PAGE_SIZE]
    items = []
    for event_id, fields in chosen:
        values = {_string(k): _string(v) for k, v in fields.items()}
        items.append({
            'stream_id': _string(event_id),
            'type': values.get('type', ''),
            'round_id': values.get('round_id', ''),
            'coefficient': values.get('coefficient', ''),
            'source_time': values.get('source_time', ''),
            'observed_at': values.get('observed_at', ''),
        })
    return {
        'view': 'archive', 'day': day, 'items': items,
        'next_cursor': _string(chosen[-1][0]) if has_more else None,
        'page_size': ARCHIVE_PAGE_SIZE,
    }


async def history(authorization, gate, token, query=''):
    """Return one bounded history page after verifying Telegram initData."""
    if not authorization.startswith('tma '):
        return json_response('401 Unauthorized', {'error': 'Открой приложение через Telegram.'})
    try:
        uid = telegram_user(authorization[4:], token)
    except (ValueError, KeyError, TypeError, AttributeError):
        return json_response('401 Unauthorized', {'error': 'Сеанс истёк. Переоткрой приложение.'})
    if gate is None:
        return json_response('503 Service Unavailable', {'error': 'Бот запускается.'})
    if not await gate.is_authorized(uid) or (uid in gate.blocked and not gate.is_admin(uid)):
        return json_response('403 Forbidden', {'error': 'Сначала получи доступ в чате бота.'})
    try:
        args = parse_qs(query, keep_blank_values=True, max_num_fields=6)
        if any(len(v) != 1 for v in args.values()) or not set(args) <= {'view', 'page', 'day', 'index', 'id', 'cursor'}:
            raise ValueError('Invalid parameters')
        view = args.get('view', ['days'])[0]
        if view not in VIEWS:
            raise ValueError('Unknown view')
        page = _number(args.get('page', ['0'])[0], 9999)
        day = _date(args['day'][0]) if 'day' in args else None
        index = _number(args['index'][0], MAX_HISTORY - 1) if 'index' in args else None
        item_id = args.get('id', [None])[0]
        cursor = args.get('cursor', [None])[0]
        if cursor is not None and (view != 'archive' or not CURSOR_PATTERN.fullmatch(cursor)):
            raise ValueError('Invalid archive cursor')
        if item_id is not None and not (
            (view == 'archive_cycle' and re.fullmatch(r'[0-9a-f]{64}', item_id)) or
            (view != 'archive_cycle' and ITEM_PATTERN.fullmatch(item_id))
        ):
            raise ValueError('Invalid item')
        if (view in {'day', 'high_day', 'max_step_day',
                     'archive_day', 'archive_high_day', 'archive_high_item', 'archive_max_step_day'} and day is None or
            view in {'high_item', 'archive_high_item', 'archive_cycle'} and item_id is None or
            view in {'cycle', 'high_cycle'} and index is None):
            raise ValueError('Required parameter missing')
    except (ValueError, KeyError, TypeError):
        return json_response('400 Bad Request', {'error': 'Некорректные параметры истории.'})

    if view in FACTUAL_VIEWS:
        redis = getattr(gate, 'redis', None)
        if redis is None:
            return json_response('503 Service Unavailable', {'error': 'Хранилище архива недоступно.'})
        result = await archive_history(redis, uid, view, day=day, page=page, item_id=item_id)
        if result.get('not_found'):
            return json_response('404 Not Found', {'error': 'Запись архива не найдена.'})
        return json_response('200 OK', result)

    redis_for_reset = getattr(gate, 'redis', None)
    markers = {}
    if redis_for_reset is not None and view != 'archive':
        stored = await redis_for_reset.hgetall(f'luckyjet:history:user-reset:{uid}')
        markers = {_string(k): _reset_info(v) for k, v in stored.items()}

    if view in {'cycles', 'cycle', 'high_cycles', 'high_cycle', 'demo_stats', 'max_steps', 'max_step_day'}:
        ledger = gate.demo_ledgers.get(uid) or DemoLedger()
        cycle_marker = markers.get('cycles')
        is_high = view in {'high_cycles', 'high_cycle'}
        current_history = ledger.high_history if is_high else ledger.history
        eligible = [(i, record) for i, record in enumerate(current_history)
                    if _after_cycle_reset(record, cycle_marker)]
        if view == 'demo_stats':
            result = _demo_stats(ledger, [record for _, record in eligible])
            if cycle_marker:
                result['view_reset'] = True
            return json_response('200 OK', {'view': view, 'stats': result})
        if view == 'max_step_day':
            marker = markers.get('max_steps')
            if marker and day < marker['day']:
                return json_response('200 OK', {'view': view, 'day': day, 'steps': None, 'view_reset': True})
            if marker and day == marker['day'] and redis_for_reset is not None:
                _summary, steps = await _after_reset_day(redis_for_reset, day, marker)
                return json_response('200 OK', {
                    'view': view, 'day': day, 'steps': steps, 'view_reset': True,
                    'truncated': _summary is None,
                })
            return json_response('200 OK', {
                'view': view, 'day': day, 'steps': ledger.daily_max_low_streak.get(day),
            })
        if view == 'max_steps':
            marker = markers.get('max_steps')
            maxima = dict(ledger.daily_max_low_streak)
            if marker and redis_for_reset is not None:
                summary, steps = await _after_reset_day(redis_for_reset, marker['day'], marker)
                if summary is not None:
                    if summary['total']:
                        maxima[marker['day']] = steps
                    else:
                        maxima.pop(marker['day'], None)
                else:
                    maxima[marker['day']] = None
            days = sorted((d for d in maxima
                           if not marker or d >= marker['day']), reverse=True)
            return json_response('200 OK', {
                'view': view, 'days': [
                    {'day': key, 'steps': maxima[key]}
                    for key in days[page*10:page*10+10]],
                'total': len(days), 'page': page, 'view_reset': bool(marker),
            })
        if view in {'cycles', 'high_cycles'}:
            return json_response('200 OK', {
                'view': view, 'total': len(eligible), 'page': page,
                'items': [_cycle(row, i) for i, row in eligible[page*10:page*10+10]],
            })
        if index >= len(current_history) or not _after_cycle_reset(current_history[index], cycle_marker):
            return json_response('404 Not Found', {'error': 'Цикл не найден.'})
        return json_response('200 OK', {
            'view': view, 'item': _cycle(current_history[index], index, detailed=True),
        })

    stats = getattr(gate, 'coefficient_stats', None)
    if stats is None or stats.redis is None:
        return json_response('503 Service Unavailable', {'error': 'Хранилище истории недоступно.'})
    redis = stats.redis

    if view == 'archive':
        selected = day or datetime.now(TZ).date().isoformat()
        result = await archive_page(redis, selected, cursor)
        if cursor is None:
            legacy = await stats.summary(selected)
            result['integrity'] = await archive_day_evidence(redis, selected, legacy_total=legacy.get('total'))
            aggregate, max_low_streak = await _after_reset_day(redis, selected, {'stream_id': '0-0'})
            result['aggregate'] = aggregate
            result['max_low_streak'] = max_low_streak
            result['truncated'] = aggregate is None
        return json_response('200 OK', result)

    if view == 'days':
        marker = markers.get('days')
        if marker:
            raw = await redis.zrevrange('luckyjet:coef:days', 0, -1)
            days = [_string(d) for d in raw if _string(d) >= marker['day']]
            if marker['day'] in days:
                summary, _ = await _after_reset_day(redis, marker['day'], marker)
                if summary is not None and summary['total'] == 0:
                    days.remove(marker['day'])
            total = len(days)
            days = days[page*10:page*10+10]
        else:
            days, total = await stats.days(page)
        return json_response('200 OK', {'view': view, 'days': days, 'page': page, 'total': total})
    if view == 'day':
        marker = markers.get('days')
        if marker and day < marker['day']:
            return json_response('200 OK', {'view': view, 'day': day, 'summary': {}, 'view_reset': True})
        if marker and day == marker['day']:
            summary, _ = await _after_reset_day(redis, day, marker)
            if summary is None:
                return json_response('200 OK', {'view': view, 'day': day, 'summary': {}, 'truncated': True, 'view_reset': True})
            distribution = await _day_distribution(redis, day, marker)
            return json_response('200 OK', {'view': view, 'day': day, 'summary': summary,
                                            'distribution': distribution, 'view_reset': True})
        summary = await stats.summary(day)
        distribution = await _day_distribution(redis, day)
        evidence = await archive_day_evidence(
            redis, day, legacy_total=summary.get('total'),
        )
        return json_response('200 OK', {
            'view': view, 'day': day, 'summary': summary, 'distribution': distribution, 'integrity': evidence,
        })

    if view == 'high_days':
        marker = markers.get('high_days')
        if marker:
            raw = await redis.zrevrange('luckyjet:coef:high:days', 0, -1)
            eligible_days = [_string(d) for d in raw if _string(d) >= marker['day']]
            if marker['day'] in eligible_days:
                raw_ids = await redis.lrange(f'luckyjet:coef:high:{marker["day"]}', 0, -1)
                raw_entries = await redis.hmget('luckyjet:coef:high:entries', raw_ids) if raw_ids else []
                found = any(entry and float(entry.get('stamp') or 0)*1000 > marker['at_ms']
                            for entry in (_entry(value) for value in raw_entries))
                if not found:
                    eligible_days.remove(marker['day'])
            total = len(eligible_days)
            days = eligible_days[page*10:page*10+10]
        else:
            total = await redis.zcard('luckyjet:coef:high:days')
            days = await redis.zrevrange('luckyjet:coef:high:days', page*10, page*10+9)
        state = {_string(k): _string(v) for k, v in
                 (await redis.hgetall('luckyjet:coef:high:state')).items()}
        previous = json.loads(state['last_high']) if state.get('last_high') else None
        if marker and previous and float(previous.get('stamp') or 0) * 1000 <= marker['at_ms']:
            previous = None
        return json_response('200 OK', {
            'view': view, 'total': total, 'page': page,
            'days': [_string(d) for d in days],
            'since_high': int(state.get('since', 0)), 'gap': state.get('gap') == '1',
            'last_high': previous, 'view_reset': bool(marker),
        })
    if view == 'high_day':
        key = f'luckyjet:coef:high:{day}'
        marker = markers.get('high_days')
        if marker and day < marker['day']:
            return json_response('200 OK', {'view': view, 'day': day, 'total': 0, 'page': page, 'items': []})
        if marker and day == marker['day']:
            all_ids = await redis.lrange(key, 0, -1)
            raw_entries = await redis.hmget('luckyjet:coef:high:entries', all_ids) if all_ids else []
            entries = [e for e in (_entry(raw) for raw in raw_entries)
                       if e and float(e.get('stamp') or 0) * 1000 > marker['at_ms']]
            total = len(entries)
            items = entries[page*10:page*10+10]
        else:
            total = await redis.llen(key)
            ids = await redis.lrange(key, page*10, page*10+9)
            raw_entries = await redis.hmget('luckyjet:coef:high:entries', ids) if ids else []
            items = [e for e in (_entry(raw) for raw in raw_entries) if e]
        return json_response('200 OK', {
            'view': view, 'day': day, 'total': total, 'page': page, 'items': items,
        })
    item = _entry(await redis.hget('luckyjet:coef:high:entries', item_id))
    marker = markers.get('high_days')
    if item and marker and (item.get('day') < marker['day'] or
            (item.get('day') == marker['day'] and
             float(item.get('stamp') or 0) * 1000 <= marker['at_ms'])):
        item = None
    if not item:
        return json_response('404 Not Found', {'error': 'Интервал не найден.'})
    return json_response('200 OK', {'view': view, 'item': item})
