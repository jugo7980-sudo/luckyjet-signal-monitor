"""Authenticated, read-only history for Telegram Mini App.

No account, bot token or Redis key is ever exposed to the browser.
"""
import json
import re
from datetime import datetime
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

from mini_app_api import json_response, telegram_user
from luckyjet_signal.demo_ledger import DemoLedger, DEFAULT_BALANCE_CENTS, MAX_HISTORY

TZ = ZoneInfo('Asia/Bangkok')
VIEWS = {'days', 'day', 'high_days', 'high_day', 'high_item',
         'cycles', 'cycle', 'demo_stats', 'max_steps', 'max_step_day'}
DAY_PATTERN = re.compile(r'^\d{4}-\d{2}-\d{2}$')
ITEM_PATTERN = re.compile(r'^[0-9a-f]{16}$')


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
        'started_at': entry.get('started_at'),
        'ended_at': entry.get('ended_at'),
        'outcome': entry.get('outcome'),
        'net_cents': entry.get('net_cents'),
        'steps': len(entry.get('rounds') or []),
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


def _demo_stats(ledger):
    records = list(ledger.history)
    valid = [r for r in records if r.get('outcome') in
             {'recovered', 'insufficient_balance'} and r.get('net_cents') is not None]
    return {
        'total': len(records),
        'maximum': MAX_HISTORY,
        'recovered': sum(r.get('outcome') == 'recovered' for r in records),
        'lost': sum(r.get('outcome') == 'insufficient_balance' for r in records),
        'incomplete': sum(r.get('outcome') == 'incomplete_data' for r in records),
        'stopped': sum(r.get('outcome') == 'stopped_by_user' for r in records),
        'since_reset_cents': ledger.balance_cents - DEFAULT_BALANCE_CENTS,
        'history_net_cents': sum(int(r['net_cents']) for r in valid),
        'average_steps': round(sum(len(r.get('rounds') or []) for r in valid) / len(valid), 1) if valid else 0,
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
        if any(len(v) != 1 for v in args.values()) or not set(args) <= {'view', 'page', 'day', 'index', 'id'}:
            raise ValueError('Invalid parameters')
        view = args.get('view', ['days'])[0]
        if view not in VIEWS:
            raise ValueError('Unknown view')
        page = _number(args.get('page', ['0'])[0], 9999)
        day = _date(args['day'][0]) if 'day' in args else None
        index = _number(args['index'][0], MAX_HISTORY - 1) if 'index' in args else None
        item_id = args.get('id', [None])[0]
        if item_id is not None and not ITEM_PATTERN.fullmatch(item_id):
            raise ValueError('Invalid item')
        if (view in {'day', 'high_day', 'max_step_day'} and day is None or
            view == 'high_item' and item_id is None or
            view == 'cycle' and index is None):
            raise ValueError('Required parameter missing')
    except (ValueError, KeyError, TypeError):
        return json_response('400 Bad Request', {'error': 'Некорректные параметры истории.'})

    if view in {'cycles', 'cycle', 'demo_stats', 'max_steps', 'max_step_day'}:
        ledger = gate.demo_ledgers.get(uid) or DemoLedger()
        if view == 'demo_stats':
            return json_response('200 OK', {'view': view, 'stats': _demo_stats(ledger)})
        if view == 'max_step_day':
            return json_response('200 OK', {
                'view': view, 'day': day, 'steps': ledger.daily_max_low_streak.get(day),
            })
        if view == 'max_steps':
            days = sorted(ledger.daily_max_low_streak, reverse=True)
            return json_response('200 OK', {
                'view': view, 'days': [
                    {'day': key, 'steps': ledger.daily_max_low_streak[key]}
                    for key in days[page*10:page*10+10]],
                'total': len(days), 'page': page,
            })
        records = ledger.history
        if view == 'cycles':
            return json_response('200 OK', {
                'view': view, 'total': len(records), 'page': page,
                'items': [_cycle(row, i) for i, row in
                          enumerate(records[page*10:page*10+10], start=page*10)],
            })
        if index >= len(records):
            return json_response('404 Not Found', {'error': 'Цикл не найден.'})
        return json_response('200 OK', {'view': view, 'item': _cycle(records[index], index, detailed=True)})

    stats = getattr(gate, 'coefficient_stats', None)
    if stats is None or stats.redis is None:
        return json_response('503 Service Unavailable', {'error': 'Хранилище истории недоступно.'})
    redis = stats.redis

    if view == 'days':
        days, total = await stats.days(page)
        return json_response('200 OK', {'view': view, 'days': days, 'page': page, 'total': total})
    if view == 'day':
        return json_response('200 OK', {'view': view, 'day': day, 'summary': await stats.summary(day)})

    if view == 'high_days':
        total = await redis.zcard('luckyjet:coef:high:days')
        days = await redis.zrevrange('luckyjet:coef:high:days', page*10, page*10+9)
        state = {_string(k): _string(v) for k, v in
                 (await redis.hgetall('luckyjet:coef:high:state')).items()}
        previous = json.loads(state['last_high']) if state.get('last_high') else None
        return json_response('200 OK', {
            'view': view, 'total': total, 'page': page,
            'days': [_string(d) for d in days],
            'since_high': int(state.get('since', 0)), 'gap': state.get('gap') == '1',
            'last_high': previous,
        })
    if view == 'high_day':
        key = f'luckyjet:coef:high:{day}'
        total = await redis.llen(key)
        ids = await redis.lrange(key, page*10, page*10+9)
        raws = await redis.hmget('luckyjet:coef:high:entries', ids) if ids else []
        return json_response('200 OK', {
            'view': view, 'day': day, 'total': total, 'page': page,
            'items': [entry for entry in (_entry(raw) for raw in raws) if entry],
        })
    item = _entry(await redis.hget('luckyjet:coef:high:entries', item_id))
    if not item:
        return json_response('404 Not Found', {'error': 'Интервал не найден.'})
    return json_response('200 OK', {'view': view, 'item': item})
