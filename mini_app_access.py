"""Public live tiles and verified Telegram registration with Premium access.

A guest gets only completed coefficient tiles. Premium reuses the existing
bot's Redis-backed authorized set. Browser sessions use opaque HttpOnly cookies;
Telegram Mini Apps use server-verified Telegram initData.
"""
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import time
from urllib.parse import parse_qs, parse_qsl

from mini_app_api import telegram_user, json_response

SESSION_AGE = 30 * 86400
SESSION_COOKIE = 'lj_session'
SESSION_KEY = 'luckyjet:web_session:after-reset-20261107:'
REGISTRY_KEY = 'luckyjet:web_registered'
AUTH_DAYS = 30
LIMITS = {'20', '30', '50', '100'}


def _identity(uid, username=None, first_name=None):
    handle = username if isinstance(username, str) and re.fullmatch(r'[A-Za-z0-9_]{4,32}', username) else None
    name = (first_name[:64] if isinstance(first_name, str) else '') or ''
    return {'id': uid, 'username': handle, 'first_name': name}


def _telegram_profile(raw, bot_token):
    uid = telegram_user(raw, bot_token)
    values = dict(parse_qsl(raw, keep_blank_values=True))
    user = json.loads(values.get('user', '{}'))
    return _identity(uid, user.get('username'), user.get('first_name'))


def _widget_profile(value, bot_token, *, now=None):
    """Legacy Telegram Login Widget signature, short freshness window."""
    if not isinstance(value, dict) or not bot_token or 'hash' not in value:
        raise ValueError('Invalid login')
    allowed = {'id', 'first_name', 'last_name', 'username', 'photo_url', 'auth_date', 'hash'}
    if not set(value) <= allowed or not all(isinstance(x, (str, int)) for x in value.values()):
        raise ValueError('Invalid fields')
    try:
        uid = int(value['id'])
        auth_date = int(value['auth_date'])
        received = str(value['hash'])
    except (KeyError, TypeError, ValueError):
        raise ValueError('Missing fields') from None
    instant = time.time() if now is None else now
    if uid < 1 or not (0 <= instant - auth_date <= 300) or not re.fullmatch(r'[a-f0-9]{64}', received):
        raise ValueError('Expired login')
    check = '\n'.join(f'{key}={val}' for key, val in sorted(value.items()) if key != 'hash')
    expected = hmac.new(hashlib.sha256(bot_token.encode()).digest(),
                        check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(received, expected):
        raise ValueError('Signature mismatch')
    return _identity(uid, value.get('username'), value.get('first_name'))


def _cookie_token(raw):
    for item in (raw or '').split(';'):
        key, _, val = item.strip().partition('=')
        if key == SESSION_COOKIE and re.fullmatch(r'[A-Za-z0-9_-]{32,100}', val):
            return val
    return None


def _redis_session_key(secret):
    return SESSION_KEY + hashlib.sha256(secret.encode()).hexdigest()


async def cookie_user(raw_cookie, gate):
    secret = _cookie_token(raw_cookie)
    if not secret or gate is None or gate.redis is None:
        return None
    raw_id = await gate.redis.get(_redis_session_key(secret))
    if raw_id and str(raw_id).isdigit():
        return int(raw_id)
    return None


def _sign_tma(uid, bot_token):
    """Only for trusted, Redis-validated server-side browser session translation."""
    fields = {'auth_date': str(int(time.time())), 'user': json.dumps({'id': uid}, separators=(',', ':'))}
    check = '\n'.join(f'{key}={val}' for key, val in sorted(fields.items()))
    secret = hmac.new(b'WebAppData', bot_token.encode(), hashlib.sha256).digest()
    fields['hash'] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    from urllib.parse import urlencode
    return 'tma ' + urlencode(fields)


async def effective_auth(authorization, cookie, gate, token):
    if authorization.startswith('tma '):
        return authorization
    uid = await cookie_user(cookie, gate)
    return _sign_tma(uid, token) if uid and token else ''


async def guest_overview(gate, query=''):
    if gate is None:
        return json_response('503 Service Unavailable', {'error': 'Мониторинг запускается.'})
    try:
        args = parse_qs(query, keep_blank_values=True, max_num_fields=1)
        if set(args) - {'limit'} or any(len(v) != 1 for v in args.values()):
            raise ValueError()
        limit = args.get('limit', ['20'])[0]
        if limit not in LIMITS:
            raise ValueError()
    except ValueError:
        return json_response('400 Bad Request', {'error': 'Доступно 20, 30, 50 или 100 коэффициентов.'})
    source = gate.history_provider() if gate.history_provider else []
    coefficients = []
    for value in list(source or [])[:int(limit)]:
        try:
            number = float(str(value).strip().rstrip('xX').replace(',', '.'))
        except (ValueError, TypeError):
            continue
        if math.isfinite(number) and 1 <= number <= 100000:
            coefficients.append(f'{number:.2f}x')
    return json_response('200 OK', {
        'coefficients': coefficients,
        'last_round_at': gate.last_round_at,
        'limit': int(limit),
    })


async def _registered(redis, uid):
    raw = await redis.hget(REGISTRY_KEY, str(uid)) if redis is not None else None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


async def _status(gate, profile, *, registered=False):
    uid = profile['id'] if profile else None
    premium = bool(uid and uid in gate.authorized
                   and (uid not in gate.blocked or gate.is_admin(uid)))
    member = registered or premium
    ledger = gate.demo_ledgers.get(uid) if premium else None
    return {'verified': bool(profile), 'registered': bool(member),
            'premium': premium, 'username': profile.get('username') if member else None,
            'first_name': profile.get('first_name') if member else None,
            'balance_cents': ledger.balance_cents if ledger else (1000000 if premium else None),
            'currency': ledger.currency if ledger else 'USD',
            'balance_verified': ledger.balance_verified if ledger else True,
            'bot_username': 'jugo_luckyjet_signal_bot'}


def _no_store_response(status, data, extra=None):
    result = json_response(status, data)
    return (*result, extra) if extra is not None else result


async def access(authorization, cookie, gate, bot_token, method='GET', body=None):
    if gate is None or gate.redis is None:
        return json_response('503 Service Unavailable', {'error': 'Авторизация временно недоступна.'})

    profile = None
    if authorization.startswith('tma '):
        try:
            profile = _telegram_profile(authorization[4:], bot_token)
        except (ValueError, TypeError, KeyError, AttributeError):
            return json_response('401 Unauthorized', {'error': 'Сессия Telegram недействительна.'})
    else:
        uid = await cookie_user(cookie, gate)
        if uid:
            saved = await _registered(gate.redis, uid)
            profile = saved or _identity(uid)
    if method == 'GET':
        if not profile:
            return json_response('200 OK', await _status(gate, None))
        saved = await _registered(gate.redis, profile['id'])
        if saved:
            profile = {**saved, **{k: v for k,v in profile.items() if v}}
        return json_response('200 OK', await _status(gate, profile, registered=bool(saved)))

    if method != 'POST':
        return json_response('405 Method Not Allowed', {'error': 'Недопустимый метод.'})
    try:
        data = json.loads(body or b'')
        if not isinstance(data, dict):
            raise ValueError()
    except (TypeError, ValueError, UnicodeDecodeError):
        return json_response('400 Bad Request', {'error': 'Некорректные данные.'})
    action = data.get('action')
    if action == 'telegram_login':
        if set(data) != {'action', 'user'}:
            return json_response('400 Bad Request', {'error': 'Неверный запрос входа.'})
        try:
            signed_profile = _widget_profile(data['user'], bot_token)
        except ValueError:
            return json_response('401 Unauthorized', {'error': 'Вход Telegram не подтверждён.'})
        uid = signed_profile['id']
        if uid in gate.blocked and not gate.is_admin(uid):
            return json_response('403 Forbidden', {'error': 'Доступ заблокирован.'})
        await gate.redis.hset(REGISTRY_KEY, str(uid), json.dumps(signed_profile))
        session = secrets.token_urlsafe(32)
        await gate.redis.setex(_redis_session_key(session), SESSION_AGE, str(uid))
        return _no_store_response('200 OK', await _status(gate, signed_profile, registered=True),
                {'Set-Cookie': f'{SESSION_COOKIE}={session}; Max-Age={SESSION_AGE}; Path=/; HttpOnly; Secure; SameSite=Lax'})

    if not profile:
        return json_response('401 Unauthorized', {'error': 'Сначала войди через Telegram.'})
    uid = profile['id']
    if uid in gate.blocked and not gate.is_admin(uid):
        return json_response('403 Forbidden', {'error': 'Доступ заблокирован.'})

    if action == 'register' and set(data) == {'action'}:
        await gate.redis.hset(REGISTRY_KEY, str(uid), json.dumps(profile))
        return json_response('200 OK', await _status(gate, profile, registered=True))

    if action == 'premium' and set(data) == {'action', 'key'}:
        registered = await _registered(gate.redis, uid)
        if not registered and uid not in gate.authorized and not gate.is_admin(uid):
            return json_response('403 Forbidden', {'error': 'Сначала зарегистрируйся через Telegram.'})
        # The Telegram bot password already grants exactly the same authorized
        # membership through /start. Accept it here as well as the original
        # Premium-only key; never log, expose, or overwrite either secret.
        keys = tuple(value for value in (
            os.getenv('MINI_APP_PREMIUM_KEY', '').strip(),
            getattr(gate, 'password', ''),
        ) if isinstance(value, str) and value)
        if not keys:
            return json_response('503 Service Unavailable', {'error': 'Активация временно недоступна.'})
        attempts_key = f'luckyjet:premium_attempts:{uid}'
        failures = int(await gate.redis.get(attempts_key) or 0)
        if failures >= 5:
            return json_response('429 Too Many Requests', {'error': 'Слишком много попыток. Повтори через 15 минут.'})
        submitted = data.get('key')
        if not isinstance(submitted, str) or len(submitted) > 128:
            return json_response('400 Bad Request', {'error': 'Недопустимый ключ.'})
        # Evaluate both matches, even if the first matched, and keep the same
        # per-user failure lockout for *either* credential.
        candidate = submitted.encode('utf-8')
        valid = False
        for secret in keys:
            valid |= hmac.compare_digest(candidate, secret.encode('utf-8'))
        if not valid:
            attempts = await gate.redis.incr(attempts_key)
            if attempts == 1:
                await gate.redis.expire(attempts_key, 900)
            return json_response('403 Forbidden', {'error': 'Неверный ключ доступа.'})
        await gate.redis.delete(attempts_key)
        gate.authorized.add(uid)
        await gate._persist_user(uid)
        return json_response('200 OK', await _status(gate, profile, registered=True))

    if action == 'logout' and set(data) == {'action'}:
        # Logout ends website browser session; it does not revoke Premium membership.
        secret = _cookie_token(cookie)
        if secret and not authorization.startswith('tma '):
            await gate.redis.delete(_redis_session_key(secret))
            return _no_store_response('200 OK', await _status(gate, None),
                {'Set-Cookie': f'{SESSION_COOKIE}=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax'})
        return json_response('409 Conflict', {'error': 'В Telegram вход выполняется автоматически.'})
    return json_response('400 Bad Request', {'error': 'Неизвестная команда.'})
