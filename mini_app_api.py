"""Read-only Mini App data, authenticated using Telegram initData."""
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


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


async def overview(authorization, gate, token):
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
    from luckyjet_signal.demo_ledger import DemoLedger
    ledger = gate.demo_ledgers.get(uid) or DemoLedger()
    history = list(gate.history_provider() or [])[:20] if gate.history_provider else []
    return json_response('200 OK', {
        'coefficients': history,
        'last_round_at': gate.last_round_at,
        'updated_at': time.time(),
        'demo': {'enabled': ledger.enabled, 'balance_cents': ledger.balance_cents,
                 'base_stake_cents': ledger.base_stake_cents, 'active': ledger.active,
                 'step': len(ledger.cycle_rounds) + 1 if ledger.active else None,
                 'trigger': ledger.trigger_streak},
    })
