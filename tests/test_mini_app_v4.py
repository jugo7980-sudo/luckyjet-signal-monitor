"""Version 4 parity, data isolation, owner-only credential flow and UI regressions."""
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
from mini_app_api import overview
from mini_app_controls import controls
from mini_app_history import history
from mini_app_site import site
from demo_probe import DemoProbe
from luckyjet_reliable_gate import ReliableTelegramPasswordGate
from luckyjet_signal.demo_ledger import DemoLedger


def signed(uid=42):
    fields = {'auth_date': str(int(time.time())), 'user': json.dumps({'id': uid})}
    check = '\n'.join(f'{key}={value}' for key, value in sorted(fields.items()))
    secret = hmac.new(b'WebAppData', b'fixture', hashlib.sha256).digest()
    fields['hash'] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return 'tma ' + urlencode(fields)


def decode(result):
    return result[0], json.loads(result[2])


def test_overview_recent_and_stats_with_authentication():
    async def run():
        source = [f'{1 + i/10:.2f}x' for i in range(100)]
        gate = ReliableTelegramPasswordGate(None, history_provider=lambda: source)
        gate.authorized.update({42, 43})
        gate.last_round = '1.00x'
        gate.last_round_at = time.time() - 4
        gate.current_streak = ['1.0x', '1.1x']
        for limit in (20, 30, 50, 100):
            code, data = decode(await overview(signed(), gate, 'fixture', f'limit={limit}'))
            assert code == '200 OK'
            assert len(data['coefficients']) == limit
            assert data['summary']['total'] == limit
            assert data['summary']['below_two'] == 10
            assert data['summary']['streak'] == 10
            assert data['summary']['maximum'] > 2
            assert data['status']['streak_count'] == 2
            assert data['status']['threshold'] == 4
            assert data['status']['fresh'] is True
            assert data['status']['age_seconds'] >= 3
        assert decode(await overview(signed(), gate, 'fixture', 'limit=101'))[0] == '400 Bad Request'
        assert decode(await overview(signed(), gate, 'fixture', 'limit=30&limit=100'))[0] == '400 Bad Request'
        gate.blocked.add(42)
        assert decode(await overview(signed(), gate, 'fixture'))[0] == '403 Forbidden'
        await gate.close()
    asyncio.run(run())


def test_maximum_step_can_be_selected_per_day_without_leaking_another_account():
    async def run():
        gate = ReliableTelegramPasswordGate(None)
        gate.authorized.update({42, 43})
        gate.demo_ledgers[42] = DemoLedger(
            daily_max_low_streak={'2026-11-05': 9, '2026-11-06': 5},
            daily_max_steps={'2026-11-05': 99},
        )
        code, data = decode(await history(signed(), gate, 'fixture', 'view=max_step_day&day=2026-11-05'))
        assert code == '200 OK' and data['steps'] == 9
        _, missing = decode(await history(signed(43), gate, 'fixture', 'view=max_step_day&day=2026-11-05'))
        assert missing['steps'] is None
        assert decode(await history(signed(), gate, 'fixture', 'view=max_step_day'))[0] == '400 Bad Request'
        await gate.close()
    asyncio.run(run())


def test_demo_probe_only_admin_and_rate_limited():
    async def run():
        gate = ReliableTelegramPasswordGate(None, admin_chat_id='99')
        gate.authorized.update({99, 42})
        gate.redis = object()
        gate.demo_probe = DemoProbe()
        payload = b'{"action":"demo_probe_start"}'
        assert decode(await controls(signed(42), gate, 'fixture', 'POST', payload))[0] == '403 Forbidden'
        code, result = decode(await controls(signed(99), gate, 'fixture', 'POST', payload))
        assert code == '200 OK' and result['probe']['running'] is True
        assert decode(await controls(signed(99), gate, 'fixture', 'POST', payload))[0] == '409 Conflict'
        await gate.demo_probe.close()
        assert decode(await controls(signed(99), gate, 'fixture', 'POST', payload))[0] == '429 Too Many Requests'
        gate.redis = None
        await gate.close()
    asyncio.run(run())


def test_site_login_otp_status_cancel_forget_and_owner_isolation():
    class Store:
        session = None
        def load(self): return self.session
        def save(self, value): self.session = value
        def clear(self): self.session = None

    class Driver:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return None
        async def login(self, email, password, code_callback):
            assert email == 'owner@example.org' and password == 'top-secret-789'
            assert await code_callback() == '812345'
            return {'cookies': [], 'origins': []}

    class Account:
        owner_id = 99
        status = 'disconnected'
        field = None
        busy = False
        attempt_timeout = 20
        input_timeout = 5
        generation = 0
        browser_lock = None
        _control = None
        def __init__(self):
            self.store = Store()
            self.browser_lock = asyncio.Lock()
            self._control = asyncio.Lock()
        def driver_factory(self): return Driver()
        async def cancel(self, *, notify=False): return None

    async def run():
        gate = ReliableTelegramPasswordGate(None, admin_chat_id='99')
        gate.authorized.update({99, 42})
        account = Account()
        gate.site_account = account
        async def call(uid=99, action=None, **kwargs):
            payload = {'action': action, **kwargs} if action else None
            return decode(await site(signed(uid), gate, 'fixture',
                'POST' if payload else 'GET', json.dumps(payload).encode() if payload else None))

        assert (await call(uid=42))[0] == '403 Forbidden'
        assert (await call(uid=42, action='forget', confirm=True))[0] == '403 Forbidden'
        assert (await call(uid=99, action='forget', confirm=False))[0] == '400 Bad Request'
        assert (await call(action='login', email='bad', password='x'))[0] == '400 Bad Request'
        code, result = await call(action='login', email='owner@example.org', password='top-secret-789')
        assert code == '200 OK' and result['phase'] in {'connecting', 'code'}
        for _ in range(100):
            await asyncio.sleep(0.005)
            if account._mini_app_web_login.phase == 'code':
                break
        assert account._mini_app_web_login.phase == 'code'
        assert (await call(action='code', code='12'))[0] == '400 Bad Request'
        code, result = await call(action='code', code='812345')
        assert code == '200 OK'
        await asyncio.wait_for(account._mini_app_web_login.task, 2)
        code, result = await call()
        assert code == '200 OK' and result['saved_session'] is True
        assert result['phase'] == 'connected'
        assert 'top-secret-789' not in json.dumps(result)
        assert '812345' not in json.dumps(result)
        assert (await call(action='forget', confirm=True))[0] == '200 OK'
        assert account.store.load() is None
        assert (await call())[1]['phase'] == 'disconnected'
        await gate.close()
    asyncio.run(run())


def test_home_colors_removed_quick_access_and_site_web_form():
    from urllib.parse import parse_qs, urlparse
    from playwright.async_api import async_playwright

    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={'width': 390, 'height': 844})
            errors, limits, logins = [], [], []
            phase = ['disconnected']
            page.on('pageerror', lambda e: errors.append(str(e)))
            async def route(req):
                if req.request.url.endswith('/access.js'):
                    await req.fulfill(content_type='application/javascript',
                        body="window.accessState={premium:true,registered:true,username:'tester'};document.body.dataset.access='premium';window.canUsePremium=()=>true;window.refreshAccessOverview?.();")
                    return
                url = req.request.url
                if url.startswith('https://example.test/app'):
                    await req.fulfill(content_type='text/html', body=response('/app')[2])
                elif url.endswith(('/history.js','/controls.js','/admin.js','/site.js')):
                    await req.fulfill(content_type='application/javascript', body=response('/'+url.split('/')[-1])[2])
                elif '/api/overview' in url:
                    limit = int(parse_qs(urlparse(url).query).get('limit', ['20'])[0])
                    limits.append(limit)
                    coef = ['1.30x', '3.50x', '11.10x'] + ['1.00x']*(limit-3)
                    await req.fulfill(content_type='application/json', body=json.dumps({
                        'coefficients': coef, 'last_round_at': int(time.time()),
                        'summary': {'total': limit, 'average': 1.4, 'maximum': 11.1,
                                    'below_two': limit-2, 'streak': 1},
                        'status': {'age_seconds': 1, 'fresh': True, 'last': '1.30x',
                                   'threshold': 4, 'streak_count': 2, 'uptime_seconds': 3700},
                        'demo': {'balance_cents': 1000000, 'enabled': False,
                                 'active': False, 'step': None, 'base_stake_cents': 1000, 'trigger': 4},
                    }))
                elif '/api/site' in url:
                    if req.request.method == 'POST':
                        body = req.request.post_data_json
                        logins.append(body['action'])
                        if body['action'] == 'login': phase[0] = 'code'
                        if body['action'] == 'code': phase[0] = 'connected'
                        if body['action'] == 'forget': phase[0] = 'disconnected'
                    await req.fulfill(content_type='application/json', body=json.dumps({
                        'phase': phase[0], 'saved_session': phase[0] == 'connected', 'error': None}))
                elif '/api/controls' in url:
                    await req.fulfill(content_type='application/json', body=json.dumps({
                        'admin': True, 'demo': {'balance_cents': 1000000, 'enabled': False,
                         'active': False, 'low_streak': 0, 'trigger': 4, 'base_stake_cents': 1000},
                        'settings': {'notifications': True, 'sound': True, 'threshold': 4,
                                     'history_size': 20, 'delete_delay': 10}}))
                elif '/api/admin' in url:
                    await req.fulfill(content_type='application/json', body=json.dumps({
                        'page': 0, 'page_size': 20, 'total': 0, 'authorized_count': 1,
                        'blocked_count': 0, 'users': []}))
                else:
                    await req.fulfill(content_type='application/javascript',
                        body="window.Telegram={WebApp:{initData:'fixture',ready(){},expand(){},setHeaderColor(){},setBackgroundColor(){},close(){},showConfirm(text,cb){cb(true)},BackButton:{onClick(){},hide(){},show(){}},HapticFeedback:{selectionChanged(){}}}}")
            await page.route('**/*', route)
            await page.goto('https://example.test/app')
            await page.locator('#coef-grid .coef').first.wait_for()
            assert await page.locator('#coef-grid .coef').count() == 20
            assert await page.get_by_text('Быстрый доступ').count() == 0
            assert await page.locator('.coef.mid').count() == 1
            assert await page.locator('.coef.high').count() == 1
            assert await page.locator('#summary-heading').inner_text() == 'Сводка архива · 150 раундов'
            await page.locator('#monitoring-accordion summary').click()
            await page.wait_for_function("document.querySelector('#monitor-uptime').textContent === '01:01:40'")
            assert await page.locator('#monitor-uptime').inner_text() == '01:01:40'
            assert await page.locator('.coef.high').evaluate('(e)=>getComputedStyle(e).color') != await page.locator('.coef.mid').evaluate('(e)=>getComputedStyle(e).color')
            await page.locator('#recent-limit').select_option('50')
            await page.wait_for_function("document.querySelectorAll('#coef-grid .coef').length === 50")
            await page.locator('nav [data-tab=account]').click()
            await page.locator('#site-connection-panel').wait_for(state='visible')
            assert not await page.locator('#site-connection-panel').evaluate('(e) => e.open')
            await page.locator('#site-connection-panel summary').click()
            await page.locator('#site-section').wait_for(state='visible')
            await page.locator('#site-technical-tools').wait_for(state='attached')
            assert not await page.locator('#site-technical-tools').evaluate('(e) => e.open')
            await page.locator('#site-technical-tools summary').click()
            assert await page.locator('#site-technical-tools').evaluate('(e) => e.open')
            await page.locator('#site-technical-tools summary').click()
            await page.locator('#site-email').fill('owner@example.org')
            await page.locator('#site-password').fill('top-secret-789')
            await page.locator('#site-login-form button[type=submit]').click()
            await page.locator('#site-code-form').wait_for(state='visible')
            assert await page.locator('#site-password').input_value() == ''
            await page.locator('#site-code').fill('812345')
            await page.locator('#site-code-form button[type=submit]').click()
            await page.locator('#site-forget').wait_for(state='visible')
            await page.locator('#site-forget').click()
            await page.locator('#site-forget').wait_for(state='hidden')
            assert logins == ['login','code','forget']
            assert limits[0] == 20 and 50 in limits  # Live view now polls every >=100ms.
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert not errors, errors
            await browser.close()
    asyncio.run(run())


def test_web_login_flow_shows_safe_site_diagnostics_without_exposing_secrets():
    from mini_app_site import WebLoginFlow
    from site_account import LoginProblem
    class Driver:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return None
        async def login(self, email, password, request_code):
            assert email == 'private@example.test'
            assert password == 'secret789'
            raise LoginProblem('Форма входа осталась открыта. Сайт не подтвердил авторизацию.')
    class Store:
        def save(self, data): raise AssertionError('Unexpected save')
    class Account:
        attempt_timeout = 3
        browser_lock = asyncio.Lock()
        store = Store()
        generation = 0
        status = 'disconnected'
        def driver_factory(self): return Driver()
    async def run():
        flow = WebLoginFlow(Account())
        await flow.login('private@example.test', 'secret789')
        assert flow.phase == 'error'
        assert flow.error == 'Форма входа осталась открыта. Сайт не подтвердил авторизацию.'
        assert 'private@example.test' not in flow.error and 'secret789' not in flow.error
    asyncio.run(run())


def test_web_login_flow_delivers_precredential_screenshot_only_to_owner():
    from mini_app_site import WebLoginFlow
    from site_account import LoginProblem
    calls = []
    class Driver:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        async def login(self, email, password, code_callback):
            error = LoginProblem('Сайт не дал нажать кнопку открытия входа.')
            error.page_screenshot = b'PNG'
            raise error
    class Account:
        attempt_timeout = 4
        browser_lock = asyncio.Lock()
        owner_id = 42
        status = 'disconnected'
        def driver_factory(self): return Driver()
        async def request(self, method, payload):
            calls.append((method, payload))
    async def run():
        flow = WebLoginFlow(Account())
        await flow.login('owner@example.test', 'never-store-this',)
        assert flow.phase == 'error'
        assert len(calls) == 1 and calls[0][0] == 'sendPhoto'
        assert calls[0][1]['chat_id'] == 42
        assert 'protect_content' not in calls[0][1]
        assert 'never-store-this' not in str(calls)
    asyncio.run(run())
