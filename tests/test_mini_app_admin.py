"""Admin rights, user isolation, destructive confirmation, and test notifications."""
import asyncio
import hashlib
import hmac
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from mini_app import response
from mini_app_admin import admin
from mini_app_controls import controls
from luckyjet_reliable_gate import ReliableTelegramPasswordGate


def signed(uid):
    fields = {'auth_date': str(int(time.time())), 'user': json.dumps({'id': uid})}
    body = '\n'.join(f'{key}={value}' for key, value in sorted(fields.items()))
    secret = hmac.new(b'WebAppData', b'fixture', hashlib.sha256).digest()
    fields['hash'] = hmac.new(secret, body.encode(), hashlib.sha256).hexdigest()
    return 'tma ' + urlencode(fields)


def test_admin_listing_and_confirmed_changes_protect_owner_and_users():
    async def run():
        gate = ReliableTelegramPasswordGate(None, admin_chat_id='99')
        gate.authorized.update({99, 42, 43})
        gate.blocked.add(45)
        gate.redis = object()
        persisted = []
        revoked = []

        async def save(uid):
            persisted.append(uid)
        async def revoke(uid, *, block):
            revoked.append((uid, block))
            gate.authorized.discard(uid)
            if block:
                gate.blocked.add(uid)
        gate._persist_user = save
        gate._revoke_user = revoke

        async def call(uid=99, method='GET', value=None, query=''):
            body = json.dumps(value).encode() if value is not None else None
            code, _, raw = await admin(signed(uid), gate, 'fixture', method, body, query)
            return code, json.loads(raw)

        code, users = await call()
        assert code == '200 OK'
        assert users['admin'] is True and users['authorized_count'] == 3
        assert users['blocked_count'] == 1
        assert [row['id'] for row in users['users']] == [42, 43, 45]
        assert (await call(uid=42))[0] == '403 Forbidden'
        assert (await call(uid=45))[0] == '403 Forbidden'
        assert (await call(query='page=-1'))[0] == '400 Bad Request'
        assert (await call(query='page=0&page=1'))[0] == '400 Bad Request'

        def operation(action, target, *, confirm=True, confirmation=None):
            return {'action': action, 'target': target, 'confirm': confirm,
                    'confirm_id': str(target) if confirmation is None else confirmation}

        assert (await call(method='POST', value=operation('block', 99)))[0] == '403 Forbidden'
        assert (await call(method='POST', value=operation('block', 42, confirm=False)))[0] == '400 Bad Request'
        assert (await call(method='POST', value=operation('block', 42, confirmation='43')))[0] == '400 Bad Request'
        assert (await call(method='POST', value=operation('block', 777)))[0] == '409 Conflict'
        assert (await call(method='POST', value=operation('unblock', 42)))[0] == '409 Conflict'
        assert (await call(uid=42, method='POST', value=operation('block', 43)))[0] == '403 Forbidden'
        assert (await call(method='POST', value={'action': 'block', 'target': 42}))[0] == '400 Bad Request'

        code, outcome = await call(method='POST', value=operation('block', 42))
        assert code == '200 OK' and outcome['user'] == {'id': 42, 'status': 'blocked'}
        assert revoked == [(42, True)]
        code, outcome = await call(method='POST', value=operation('unblock', 42))
        assert code == '200 OK' and outcome['user']['status'] == 'not_authorized'
        assert 42 in persisted
        code, outcome = await call(method='POST', value=operation('revoke', 43))
        assert code == '200 OK' and outcome['user']['status'] == 'not_authorized'
        assert revoked == [(42, True), (43, False)]
        assert (await call(method='POST', value=operation('block', 42)))[0] == '409 Conflict'
        gate.redis = None
        await gate.close()
    asyncio.run(run())


def test_notification_test_requires_private_chat_and_enforces_cooldown():
    async def run():
        sent = []
        scheduled = []

        async def telegram(method, payload):
            sent.append((method, payload))
            return {'ok': True, 'result': {'message_id': 123}}
        gate = ReliableTelegramPasswordGate(telegram)
        gate.authorized.add(42)
        gate.redis = object()

        async def deletion(chat_id, message_id, delay):
            scheduled.append((chat_id, message_id, delay))
        gate._schedule_delete = deletion

        async def send():
            code, _, raw = await controls(signed(42), gate, 'fixture', 'POST',
                                          b'{"action":"test_notification"}')
            return code, json.loads(raw)

        assert (await send())[0] == '409 Conflict'
        gate.chats[42] = 42
        gate.silent_users.add(42)
        code, data = await send()
        assert code == '200 OK' and 'Telegram' in data['message']
        await asyncio.sleep(0)
        assert sent == [('sendMessage', {
            'chat_id': 42, 'text': '🧪 Тест уведомления Lucky Jet',
            'disable_notification': True,
        })]
        assert scheduled == [(42, 123, 5)]
        assert (await send())[0] == '429 Too Many Requests'
        assert len(sent) == 1
        gate.blocked.add(42)
        assert (await send())[0] == '403 Forbidden'
        gate.redis = None
        await gate.close()
    asyncio.run(run())


def test_admin_mobile_screen_and_hidden_non_admin():
    from playwright.async_api import async_playwright

    async def run():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={'width': 390, 'height': 844})
            failures = []
            page.on('pageerror', lambda err: failures.append(str(err)))
            is_admin = [True]
            users = [
                {'id': 42, 'status': 'authorized'},
                {'id': 45, 'status': 'blocked'},
            ]
            actions = []

            async def route(request):
                url = request.request.url
                if url.endswith(('/history.js', '/controls.js', '/admin.js')):
                    path = url.split('/')[-1]
                    await request.fulfill(content_type='application/javascript', body=response('/' + path)[2])
                elif url.startswith('https://example.test/app'):
                    await request.fulfill(content_type='text/html', body=response('/app')[2])
                elif '/api/admin' in url:
                    if not is_admin[0]:
                        await request.fulfill(status=403, content_type='application/json',
                                              body='{"error":"Только администратор."}')
                        return
                    if request.request.method == 'POST':
                        payload = request.request.post_data_json
                        actions.append(payload)
                        match = next(x for x in users if x['id'] == payload['target'])
                        assert payload['confirm'] is True
                        assert payload['confirm_id'] == str(payload['target'])
                        if payload['action'] == 'block':
                            match['status'] = 'blocked'
                        elif payload['action'] == 'revoke':
                            match['status'] = 'not_authorized'
                        elif payload['action'] == 'unblock':
                            match['status'] = 'not_authorized'
                        await request.fulfill(content_type='application/json',
                                              body=json.dumps({'message': 'Сохранено.', 'user': match}))
                    else:
                        await request.fulfill(content_type='application/json',
                                              body=json.dumps({'admin': True, 'authorized_count': 2,
                                                               'blocked_count': sum(x['status'] == 'blocked' for x in users),
                                                               'page': 0, 'page_size': 20, 'total': len(users),
                                                               'users': users}))
                elif '/api/controls' in url:
                    state = {'demo': {'enabled': False, 'active': False, 'balance_cents': 1000000,
                                      'base_stake_cents': 1000, 'trigger': 4, 'low_streak': 0},
                             'settings': {'notifications': True, 'sound': True, 'threshold': 4,
                                          'history_size': 20, 'delete_delay': 10}}
                    await request.fulfill(content_type='application/json', body=json.dumps(state))
                elif '/api/overview' in url:
                    state = {'coefficients': [], 'last_round_at': None,
                             'demo': {'balance_cents': 1000000, 'enabled': False,
                                      'active': False, 'base_stake_cents': 1000, 'trigger': 4}}
                    await request.fulfill(content_type='application/json', body=json.dumps(state))
                else:
                    await request.fulfill(content_type='application/javascript',
                                          body="window.Telegram={WebApp:{initData:'fixture',ready(){},expand(){},setHeaderColor(){},setBackgroundColor(){},close(){},showConfirm(text,callback){callback(true)},BackButton:{onClick(){},hide(){},show(){}},HapticFeedback:{selectionChanged(){}}}}")

            await page.route('**/*', route)
            await page.goto('https://example.test/app')
            await page.locator('nav button[data-tab=account]').click()
            await page.locator('#admin-section').wait_for(state='visible')
            assert await page.locator('.admin-user').count() == 2
            await page.locator('.admin-user').first.locator('.admin-danger').click()
            await page.get_by_text('Заблокирован', exact=True).first.wait_for()
            await page.wait_for_function("document.querySelectorAll('.admin-state-blocked').length === 2")
            await page.locator('.admin-user').first.get_by_text('Разблокировать').click()
            await page.wait_for_function("document.querySelectorAll('.admin-state-blocked').length === 1")
            assert len(actions) == 2
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')

            is_admin[0] = False
            await page.evaluate('window.clearAdminData()')
            await page.locator('nav button[data-tab=home]').click()
            await page.locator('nav button[data-tab=account]').click()
            await page.locator('#admin-section').wait_for(state='hidden')
            assert not failures, failures
            await browser.close()
    asyncio.run(run())
