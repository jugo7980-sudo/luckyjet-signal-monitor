"""Read-only Mini App data, authenticated using Telegram initData."""
import hashlib
import hmac
import json
import logging
import math
import time
from urllib.parse import parse_qsl, parse_qs

log = logging.getLogger('luckyjet.mini_app_api')


def telegram_user(raw, token, *, now=None):
    if not token or not raw or len(raw) > 12000:
        raise ValueError('Invalid session')
    pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError('Duplicate fields')
    received = fields.pop('hash', '')
    check = '\n'.join(f'{key}={value}' for key, value in sorted(fields.items()))
    secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise ValueError('Invalid signature')
    age = (time.time() if now is None else now) - int(fields['auth_date'])
    if age < -30 or age > 86400:
        raise ValueError('Expired session')
    user = json.loads(fields['user'])
    uid = user.get('id')
    if type(uid) is not int or uid <= 0:
        raise ValueError('Invalid user')
    return uid


def json_response(status, data):
    return status, 'application/json; charset=utf-8', json.dumps(data, ensure_ascii=False).encode()


def _coefficients(values):
    """Use the same accepted coefficient format as the Telegram status panel."""
    parsed = []
    for raw in values:
        try:
            n = float(str(raw).strip().rstrip('xX').replace(',', '.'))
        except (TypeError, ValueError):
            continue
        if math.isfinite(n) and n >= 1:
            parsed.append(n)
    return parsed


def _summary(values):
    numbers = _coefficients(values)
    streak = 0
    for n in numbers:
        if n >= 2:
            break
        streak += 1
    return {
        'total': len(numbers),
        'average': round(sum(numbers) / len(numbers), 2) if numbers else None,
        'maximum': max(numbers) if numbers else None,
        'below_two': sum(n < 2 for n in numbers),
        'streak': streak,
    }


async def _recent_from_archive(redis, limit):
    """Read actual latest completed rounds, newest first, without touching ledgers.

    The Mini App's live in-memory buffer may have only 20 entries right after
    startup. The persistent collector stream has the preceding rounds already.
    Skip baseline/gap records, not legitimate repeated coefficient values.
    """
    from round_archive import STREAM_KEY
    result = []
    upper = '+'
    batch_size = max(128, limit * 2)
    # Bounded scan avoids a slow HTTP endpoint if the stream contains many gaps.
    for _ in range(4):
        batch = await redis.xrevrange(STREAM_KEY, max=upper, min='-', count=batch_size)
        if not batch:
            break
        for _stream_id, fields in batch:
            kind = fields.get('type', fields.get(b'type'))
            if kind not in ('round', b'round'):
                continue
            raw = fields.get('coefficient', fields.get(b'coefficient'))
            try:
                number = float(raw)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number) and number >= 1:
                result.append(f'{number:.2f}x')
                if len(result) >= limit:
                    return result
        if len(batch) < batch_size:
            break
        last_id = batch[-1][0]
        if isinstance(last_id, bytes):
            last_id = last_id.decode('ascii')
        upper = '(' + str(last_id)
    return result


async def overview(authorization, gate, token, query=''):
    if not authorization.startswith('tma '):
        return json_response('401 Unauthorized', {'error': 'Открой приложение через Telegram.'})
    try:
        uid = telegram_user(authorization[4:], token)
    except (ValueError, KeyError, TypeError, AttributeError):
        return json_response('401 Unauthorized', {'error': 'Сеанс истёк. Переоткрой приложение.'})
    if gate is None:
        return json_response('503 Service Unavailable', {'error': 'Бот запускается. Повтори чуть позже.'})
    if not await gate.is_authorized(uid) or (uid in gate.blocked and not gate.is_admin(uid)):
        return json_response('403 Forbidden', {'error': 'Сначала получи доступ в чате бота.'})
    try:
        args = parse_qs(query, keep_blank_values=True, max_num_fields=1)
        if set(args) - {'limit'} or any(len(v) != 1 for v in args.values()):
            raise ValueError('Invalid query')
        raw_limit = args.get('limit', ['20'])[0]
        if raw_limit not in {'20', '30', '50', '100'}:
            raise ValueError('Invalid limit')
        limit = int(raw_limit)
    except ValueError:
        return json_response('400 Bad Request', {'error': 'История: 20, 30, 50 или 100 коэффициентов.'})
    from luckyjet_signal.demo_ledger import DemoLedger
    ledger = gate.demo_ledgers.get(uid) or DemoLedger()
    raw_history = list(gate.history_provider() or []) if gate.history_provider else []
    history = raw_history[:limit]
    # Requesting 30/50/100 must retrieve earlier persisted rounds immediately,
    # not wait for new rounds to fill the live buffer after a restart.
    if len(history) < limit and getattr(gate, 'redis', None) is not None:
        try:
            archived = await _recent_from_archive(gate.redis, limit)
            if len(archived) > len(history):
                history = archived
        except Exception:
            # Continue serving available live data; never mutate or invent history.
            log.exception('Failed to backfill recent coefficients from Redis archive')
    now = time.time()
    last_at = getattr(gate, 'last_round_at', None)
    age = max(0, int(now - last_at)) if isinstance(last_at, (float, int)) else None
    raw_probe = getattr(gate, 'demo_probe', None)
    probe = {
        'state': raw_probe.result,
        'requested': bool(raw_probe.requested),
        'running': bool(raw_probe.task and not raw_probe.task.done()),
        'checked_at': raw_probe.checked_at,
        'source': raw_probe.source,
    } if raw_probe else {
        'state': 'not_checked', 'requested': False, 'running': False, 'checked_at': None, 'source': None,
    }
    return json_response('200 OK', {
        'coefficients': history,
        'last_round_at': last_at,
        'updated_at': now,
        'limit': limit,
        'summary': _summary(history),
        'status': {
            'last': getattr(gate, 'last_round', None) or (history[0] if history else None),
            'age_seconds': age,
            'fresh': age is not None and age <= 90,
            'uptime_seconds': max(0, int(time.monotonic() - gate.started_at)),
            'history_count': len(history),
            'streak_count': len(gate.current_streak),
            'threshold': gate.user_threshold(uid),
            'probe': probe,
        },
        'demo': {'currency': ledger.currency, 'reset_revision': ledger.reset_revision, 'enabled': ledger.enabled, 'balance_cents': ledger.balance_cents, 'balance_verified': ledger.balance_verified,
                 'base_stake_cents': ledger.base_stake_cents, 'active': ledger.active,
                 'step': len(ledger.cycle_rounds) + 1 if ledger.active else None,
                 'trigger': ledger.trigger_streak,
                 'high_gap': ledger.high_gap.public(),
                 'target_streak': ledger.target_streak.public(ledger.currency),
                  'recovery_streak': ledger.recovery_streak.public(ledger.currency)},
    })
