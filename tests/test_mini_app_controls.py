"""Write protections and mobile UI for user-scoped Mini App controls."""
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
from mini_app_controls import controls
from fakeredis.aioredis import FakeRedis
from luckyjet_reliable_gate import ReliableTelegramPasswordGate


def signed(uid=42):
    fields = {'auth_date': str(int(time.time())), 'user': json.dumps({'id': uid})}
    data = '\n'.join(f'{key}={value}' for key, value in sorted(fields.items()))
    secret = hmac.new(b'WebAppData', b'fixture', hashlib.sha256).digest()
    fields['hash'] = hmac.new(secret, data.encode(), hashlib.sha256).hexdigest()
    return 'tma ' + urlencode(fields)


def test_user_scoped_controls_writes_and_invariants():
    async def run():
        gate = ReliableTelegramPasswordGate(None, history_provider=lambda: ['1.20x', '1.50x'])
        gate.authorized.update({42, 43})
        gate.redis = FakeRedis(decode_responses=True)
        saved_demo, saved_settings, refreshed, cleared = [], [], [], []

        async def persist_demo(uid): saved_demo.append(uid)
        async def persist_settings(uid): saved_settings.append(uid)
        async def ensure_private(uid): refreshed.append(uid)
        async def clear_notice(uid): cleared.append(uid)
        gate._persist_demo = persist_demo
        gate._persist_user = persist_settings
        gate.ensure_private_history = ensure_private
        gate._clear_streak_notice = clear_notice

        async def request(action=None, *, value=None, confirm=False, uid=42, auth=None, body=None):
            if action is None and body is None:
                method = 'GET'
            else:
                method = 'POST'
                if body is None:
                    body = json.dumps({'action': action, 'value': value, 'confirm': confirm}).encode()
            code, _, raw = await controls(signed(uid) if auth is None else auth, gate, 'fixture', method, body)
            return code, json.loads(raw)

        code, state = await request()
        assert code == '200 OK' and state['demo']['enabled'] is False
        assert (await request(uid=43))[1]['demo']['balance_cents'] == 1_000_000

        assert (await request('demo_toggle'))[1]['demo']['enabled'] is True
        assert (await request('demo_trigger', value=10))[1]['demo']['trigger'] == 10
        assert (await request('demo_stake', value='25.50'))[1]['demo']['base_stake_cents'] == 2550
        gate._demo_ledger(42).active = True
        assert (await request('demo_toggle'))[0] == '409 Conflict'
        assert (await request('demo_trigger', value=3))[0] == '409 Conflict'
        assert (await request('demo_stake', value='30'))[0] == '409 Conflict'
        assert (await request('demo_reset'))[0] == '400 Bad Request'
        assert (await request('demo_reset', confirm=True, value={'currency':'USD','balance':'10000','stake':'10','revision':'legacy'}))[1]['demo']['balance_cents'] == 1_000_000

        assert (await request('notifications_toggle'))[1]['settings']['notifications'] is False
        assert cleared == [42]
        assert (await request('sound_toggle'))[1]['settings']['sound'] is False
        assert (await request('notification_threshold', value=8))[1]['settings']['threshold'] == 8
        assert (await request('history_size', value=50))[1]['settings']['history_size'] == 50
        assert refreshed == [42]
        assert (await request('delete_delay', value=60))[1]['settings']['delete_delay'] == 60
        assert (await request('settings_reset'))[0] == '400 Bad Request'
        _, reset = await request('settings_reset', confirm=True)
        assert reset['settings'] == {
            'notifications': True, 'sound': True,
            'threshold': 4, 'history_size': 20, 'delete_delay': 10,
        }
        assert saved_demo == [42, 42, 42]
        assert len(saved_settings) == 6

        for action, value in [('demo_trigger', 99), ('notification_threshold', 1),
                              ('history_size', 200), ('delete_delay', 5)]:
            assert (await request(action, value=value))[0] == '400 Bad Request'
        assert (await request(body=b'{}'))[0] == '400 Bad Request'
        assert (await request(body=b'{"action":"admin_block","value":43}'))[0] == '400 Bad Request'
        assert (await request(auth=''))[0] == '401 Unauthorized'
        gate.blocked.add(42)
        assert (await request('demo_reset', confirm=True))[0] == '403 Forbidden'
        assert (await request(uid=99))[0] == '403 Forbidden'
        gate.redis = None
        await gate.close()
    asyncio.run(run())


def test_controls_mobile_ui_reads_changes_and_confirms_reset():
    from playwright.async_api import async_playwright

    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={'width': 390, 'height': 844})
            errors, actions = [], []
            pause_controls = {"active": False}
            control_started, control_resume = asyncio.Event(), asyncio.Event()
            state = {
                'demo': {'currency':'USD', 'reset_revision':'legacy', 'enabled': False, 'active': False, 'balance_cents': 1_000_000,
                         'base_stake_cents': 1000, 'trigger': 4, 'low_streak': 1,
                         'step': None, 'next_stake_cents': None},
                'settings': {'notifications': True, 'sound': True, 'threshold': 4,
                             'history_size': 20, 'delete_delay': 10},
            }
            page.on('pageerror', lambda error: errors.append(str(error)))

            async def route(request):
                if request.request.url.endswith('/access.js'):
                    await request.fulfill(content_type='application/javascript',
                        body="window.accessState={premium:true,registered:true,username:'tester'};document.body.dataset.access='premium';window.canUsePremium=()=>true;window.refreshAccessOverview?.();")
                    return
                url = request.request.url
                if url.endswith('/controls.js') or url.split('?', 1)[0].endswith('/history.js'):
                    await request.fulfill(content_type='application/javascript',
                                          body=response('/controls.js' if url.endswith('/controls.js') else '/history.js')[2])
                elif url.startswith('https://example.test/app'):
                    await request.fulfill(content_type='text/html', body=response('/app')[2])
                elif '/api/controls' in url:
                    if request.request.method == 'POST':
                        payload = request.request.post_data_json
                        action = payload['action']
                        actions.append(action)
                        if action == 'demo_toggle':
                            state['demo']['enabled'] = not state['demo']['enabled']
                        elif action == 'demo_trigger':
                            state['demo']['trigger'] = payload['value']
                        elif action == 'demo_stake':
                            state['demo']['base_stake_cents'] = int(float(payload['value']) * 100)
                        elif action == 'demo_reset':
                            assert payload['confirm'] is True
                            assert payload['value'] == {'currency':'RUB','balance':'50000','stake':'25.50','revision':'legacy'}
                            state['demo'].update(currency='RUB',reset_revision='new',balance_cents=5000000, base_stake_cents=2550)
                        elif action == 'notifications_toggle':
                            state['settings']['notifications'] = not state['settings']['notifications']
                        elif action == 'sound_toggle':
                            state['settings']['sound'] = not state['settings']['sound']
                        elif action == 'notification_threshold':
                            state['settings']['threshold'] = payload['value']
                        elif action == 'history_size':
                            state['settings']['history_size'] = payload['value']
                        elif action == 'delete_delay':
                            state['settings']['delete_delay'] = payload['value']
                        elif action == 'settings_reset':
                            assert payload['confirm'] is True
                            state['settings'].update(notifications=True, sound=True, threshold=4,
                                                     history_size=20, delete_delay=10)
                    if pause_controls['active']:
                        control_started.set()
                        await control_resume.wait()
                    await request.fulfill(content_type='application/json',
                                          body=json.dumps({'message': 'Сохранено.', **state}))
                elif '/api/overview' in url:
                    demo = state['demo']
                    await request.fulfill(content_type='application/json',
                        body=json.dumps({'coefficients': [], 'last_round_at': None,
                                         'demo': {**demo, 'base_stake_cents': demo['base_stake_cents']}}))
                else:
                    await request.fulfill(content_type='application/javascript',
                        body="window.Telegram={WebApp:{initData:'fixture',ready(){},expand(){},setHeaderColor(){},setBackgroundColor(){},close(){},showConfirm(text,callback){callback(true)},BackButton:{onClick(){},hide(){},show(){}},HapticFeedback:{selectionChanged(){}}}}")
            await page.route('**/*', route)
            await page.goto('https://example.test/app')

            await page.locator('nav [data-tab=demo]').click()
            await page.locator('#demo-classic-accordion summary').click()
            await page.locator('#demo-controls').wait_for(state='visible')
            await page.locator('#demo-toggle').click()
            options = await page.locator('#demo-trigger-select option').evaluate_all('(items) => items.map(item => item.value)')
            assert options == [str(n) for n in range(2, 11)]
            await page.locator('#demo-trigger-select').select_option('10')
            await page.locator('#demo-stake-input').fill('25.50')
            await page.locator('#demo-stake-save').click()
            # An async control refresh can temporarily disable or fold the panel.
            await page.wait_for_function(
                "document.querySelector('#demo-reset') && !document.querySelector('#demo-reset').disabled")
            if not await page.locator('#demo-classic-accordion').evaluate('(node) => node.open'):
                await page.locator('#demo-classic-accordion summary').click()
            # v0.2.9 keeps destructive actions inside a separate disclosure.
            if not await page.locator('#demo-management').evaluate('(node) => node.open'):
                await page.locator('#demo-management summary').click()
            await page.locator('#demo-reset').click()
            await page.locator('#demo-reset-currency').select_option('RUB')
            await page.locator('#demo-reset-balance').fill('50000')
            await page.locator('#demo-reset-stake').fill('25.50')
            await page.locator('#demo-reset-submit').click()
            await page.wait_for_function("document.getElementById('header-balance').textContent.includes('RUB')")
            assert '₽' in await page.locator('#header-balance').inner_text()
            assert await page.locator('#demo-stake-input').input_value() == '25.50'
            # v0.2.9 keeps destructive actions inside a separate disclosure.
            if not await page.locator('#demo-management').evaluate('(node) => node.open'):
                await page.locator('#demo-management summary').click()
            await page.locator('#demo-reset').click()
            await page.locator('#demo-reset-currency').select_option('KRW')
            await page.locator('#demo-reset-balance').fill('1000.50')
            await page.locator('#demo-reset-stake').fill('10')
            assert await page.locator('#demo-reset-submit').is_disabled()
            await page.locator('#demo-reset-balance').fill('1000')
            assert await page.locator('#demo-reset-submit').is_enabled()
            await page.locator('#demo-reset-cancel').click()
            assert actions.count('demo_reset') == 1

            await page.locator('nav [data-tab=account]').click()
            await page.locator('#open-settings').click()
            await page.locator('#settings-controls').wait_for(state='visible')
            await page.locator('#settings-notifications').click()
            await page.locator('#settings-sound').click()
            await page.locator('#settings-threshold').select_option('8')
            await page.locator('#settings-history-size').select_option('50')
            await page.locator('#settings-delete-delay').select_option('60')
            await page.locator('#settings-reset').click()
            await page.wait_for_function("document.getElementById('settings-notifications').textContent === 'ВКЛ'")
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert actions == [
                'demo_toggle', 'demo_trigger', 'demo_stake', 'demo_reset',
                'notifications_toggle', 'sound_toggle', 'notification_threshold',
                'history_size', 'delete_delay', 'settings_reset',
            ]
            # A response from the previous identity must not repaint its money after logout.
            pause_controls['active'] = True
            await page.locator('#settings-sound').click()
            await asyncio.wait_for(control_started.wait(), 3)
            await page.evaluate("window.accessState={premium:false};window.canUsePremium=()=>false;window.clearControlsData();document.getElementById('header-balance').textContent='—'")
            async with page.expect_response('**/api/controls'):
                control_resume.set()
            await page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
            assert await page.locator('#header-balance').inner_text() == '—'
            assert await page.locator('#demo-controls').is_hidden()
            assert not errors, errors
            await browser.close()
    asyncio.run(run())
