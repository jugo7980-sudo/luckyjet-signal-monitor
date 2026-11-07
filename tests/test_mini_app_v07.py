"""V0.7: public/private isolation, verified Telegram registration and Premium."""
import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import fakeredis.aioredis
from mini_app_api import overview
from mini_app_access import (
    _widget_profile, _redis_session_key, _sign_tma, access, cookie_user,
    guest_overview, effective_auth,
)
from luckyjet_reliable_gate import ReliableTelegramPasswordGate


def signed(uid=42, username='visitor_123'):
    fields = {'auth_date': str(int(time.time())), 'user': json.dumps({
        'id': uid, 'username': username, 'first_name': 'User'})}
    check = '\n'.join(f'{key}={value}' for key, value in sorted(fields.items()))
    secret = hmac.new(b'WebAppData', b'fixture', hashlib.sha256).digest()
    fields['hash'] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return 'tma ' + urlencode(fields)


def widget(uid=42):
    attrs = {'id': str(uid), 'auth_date': str(int(time.time())),
        'username': 'visitor_123', 'first_name': 'User'}
    check = '\n'.join(f'{key}={value}' for key, value in sorted(attrs.items()))
    attrs['hash'] = hmac.new(hashlib.sha256(b'fixture').digest(),
        check.encode(), hashlib.sha256).hexdigest()
    return attrs


def decode(response):
    return response[0], json.loads(response[2])


def test_guest_only_receives_public_coefficient_tiles_and_no_summary():
    async def run():
        gate = ReliableTelegramPasswordGate(None,
            history_provider=lambda: ['1.20x', '11.00x', 'NaNx', '-2x', '3.60x'])
        code, value = decode(await guest_overview(gate))
        assert code == '200 OK'
        assert value == {'coefficients': ['1.20x', '11.00x', '3.60x'], 'last_round_at': None, 'limit': 20}
        assert not any(field in value for field in ('summary', 'demo', 'status', 'balance'))
        assert decode(await guest_overview(gate, 'limit=123'))[0] == '400 Bad Request'
        assert decode(await guest_overview(gate, 'limit=20&limit=30'))[0] == '400 Bad Request'
        assert decode(await overview('', gate, 'fixture'))[0] == '401 Unauthorized'
        assert decode(await overview(signed(), gate, 'fixture'))[0] == '403 Forbidden'
        await gate.close()
    asyncio.run(run())


def test_widget_signature_age_and_forgery():
    good = widget()
    assert _widget_profile(good, 'fixture')['id'] == 42
    wrong = dict(good, username='attacker')
    try: _widget_profile(wrong, 'fixture')
    except ValueError: pass
    else: assert False, 'Forged Telegram profile accepted'
    old = dict(good, auth_date=str(int(time.time()) - 10000))
    try: _widget_profile(old, 'fixture')
    except ValueError: pass
    else: assert False, 'Expired login accepted'
    try: _widget_profile(good, 'other-bot-token')
    except ValueError: pass
    else: assert False, 'Wrong bot accepted'


def test_premium_register_login_cookie_revocation_and_rate_limits(monkeypatch):
    monkeypatch.setenv('MINI_APP_PREMIUM_KEY', 'test-access-token')
    async def run():
        gate = ReliableTelegramPasswordGate(None)
        gate.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        async def call(auth='', cookie='', action=None, **kwargs):
            payload = {'action': action, **kwargs} if action is not None else None
            return await access(auth, cookie, gate, 'fixture',
                'POST' if payload else 'GET',
                json.dumps(payload).encode() if payload else None)
        code, initial = decode(await call())
        assert code == '200 OK' and not initial['verified']
        code, identity = decode(await call(signed()))
        assert code == '200 OK' and identity['verified'] and not identity['registered']
        code, registered = decode(await call(signed(), action='register'))
        assert code == '200 OK' and registered['registered'] and not registered['premium']
        assert registered['username'] == 'visitor_123'
        for _ in range(5):
            assert decode(await call(signed(), action='premium', key='bad'))[0] == '403 Forbidden'
        assert decode(await call(signed(), action='premium', key='test-access-token'))[0] == '429 Too Many Requests'
        await gate.redis.delete('luckyjet:premium_attempts:42')
        code, upgraded = decode(await call(signed(), action='premium', key='test-access-token'))
        assert code == '200 OK' and upgraded['premium']
        assert 42 in gate.authorized
        assert await gate.redis.sismember('luckyjet:authorized_users', '42')
        assert decode(await overview(signed(), gate, 'fixture'))[0] == '200 OK'
        raw_login = await call(action='telegram_login', user=widget())
        assert raw_login[0] == '200 OK' and len(raw_login) == 4
    asyncio.run(run())


def test_browser_telegram_login_session_cookie(monkeypatch):
    monkeypatch.setenv('MINI_APP_PREMIUM_KEY', 'test-access-token')
    async def run():
        gate = ReliableTelegramPasswordGate(None)
        gate.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        payload = json.dumps({'action':'telegram_login', 'user': widget()}).encode()
        raw = await access('', '', gate, 'fixture', 'POST', payload)
        assert raw[0] == '200 OK' and len(raw) == 4
        header = raw[3]['Set-Cookie']
        assert all(x in header for x in ('HttpOnly','Secure','SameSite=Lax','Path=/'))
        cookie = header.split(';', 1)[0]
        assert await cookie_user(cookie, gate) == 42
        resolved = await effective_auth('', cookie, gate, 'fixture')
        assert resolved.startswith('tma ')
        code, status = decode(await access('', cookie, gate, 'fixture'))
        assert code == '200 OK' and status['registered'] and not status['premium']
        assert decode(await access('', cookie, gate, 'fixture', 'POST',
            b'{"action":"premium","key":"test-access-token"}'))[1]['premium']
        assert decode(await overview(resolved, gate, 'fixture'))[0] == '200 OK'
        logged_out = await access('', cookie, gate, 'fixture', 'POST', b'{"action":"logout"}')
        assert logged_out[0] == '200 OK' and len(logged_out) == 4
        assert 'Max-Age=0' in logged_out[3]['Set-Cookie']
        assert await cookie_user(cookie, gate) is None
        gate.blocked.add(42)
        assert (await access('', '', gate, 'fixture', 'POST', payload))[0] == '403 Forbidden'
        await gate.redis.aclose()
        gate.redis = None
        await gate.close()
    asyncio.run(run())
