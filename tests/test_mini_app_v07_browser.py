"""Guest mode: no privileged network calls, lock screens and Telegram-to-Premium UI."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from mini_app import response


def test_guest_locks_and_premium_telegram_upgrade_mobile_browser():
    from playwright.async_api import async_playwright

    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={'width': 390, 'height': 844})
            errors, calls = [], []
            profile = {'verified': False, 'registered': False, 'premium': False, 'username': None}
            page.on('pageerror', lambda err: errors.append(str(err)))

            async def route(req):
                url = req.request.url
                if url.startswith('https://example.test/app'):
                    await req.fulfill(content_type='text/html', body=response('/app')[2])
                elif url.endswith(('/history.js', '/controls.js', '/admin.js', '/site.js', '/access.js')):
                    await req.fulfill(content_type='application/javascript',
                                      body=response('/' + url.split('/')[-1])[2])
                elif '/api/public' in url:
                    calls.append('/api/public')
                    await req.fulfill(content_type='application/json', body=json.dumps({
                        'coefficients': ['1.21x', '3.51x', '11.10x'], 'limit': 20,
                        'last_round_at': None}))
                elif '/api/access' in url:
                    if req.request.method == 'POST':
                        action = req.request.post_data_json['action']
                        calls.append('access:' + action)
                        if action in {'telegram_login', 'register'}:
                            profile.update(verified=True, registered=True, premium=False,
                                           username='guest_123')
                        elif action == 'premium':
                            profile.update(premium=True, balance_cents=1010000)
                    await req.fulfill(content_type='application/json', body=json.dumps(profile))
                elif '/api/overview' in url:
                    calls.append('/api/overview')
                    await req.fulfill(content_type='application/json', body=json.dumps({
                        'coefficients': ['1.21x', '3.51x', '11.10x'], 'limit': 20,
                        'last_round_at': None,
                        'summary': {'total': 3, 'average': 5.27, 'maximum': 11.1,
                                    'below_two': 1, 'streak': 1},
                        'status': {'age_seconds': 1, 'fresh': True, 'last': '1.21x',
                                   'threshold': 4, 'streak_count': 1, 'uptime_seconds': 123},
                        'demo': {'balance_cents': 1010000, 'enabled': False, 'active': False,
                                 'step': None, 'base_stake_cents': 1000, 'trigger': 4},
                    }))
                elif '/api/controls' in url:
                    await req.fulfill(content_type='application/json', body=json.dumps({
                        'admin': False,
                        'demo': {'enabled': False, 'active': False, 'balance_cents': 1010000,
                            'base_stake_cents': 1000, 'trigger': 4, 'low_streak': 0},
                        'settings': {'notifications': True, 'sound': True, 'threshold': 4,
                            'history_size': 20, 'delete_delay': 10}}))
                elif '/api/history' in url:
                    await req.fulfill(content_type='application/json', body=json.dumps({
                        'view':'days', 'days':[], 'total':0, 'page':0}))
                elif '/api/admin' in url or '/api/site' in url:
                    await req.fulfill(status=403, content_type='application/json', body='{"error":"Forbidden"}')
                else:
                    await req.fulfill(content_type='application/javascript',
                        body="window.Telegram={WebApp:{initData:'',ready(){},expand(){},setHeaderColor(){},setBackgroundColor(){},close(){},BackButton:{onClick(){},hide(){},show(){}},HapticFeedback:{selectionChanged(){}}}}")

            await page.route('**/*', route)
            await page.goto('https://example.test/app')
            await page.locator('#coef-grid .coef').first.wait_for()
            await page.wait_for_function("document.body.dataset.access === 'guest'")
            assert await page.locator('#coef-grid .coef').count() == 3
            assert '/api/overview' not in calls
            assert await page.locator('#private-overview').is_hidden()
            assert await page.locator('#top-login').inner_text() == 'Войти'
            assert await page.locator('#header-balance').inner_text() == '—'
            assert await page.locator('.below-tiles #refresh-data').is_visible()
            assert await page.locator('#topbar-right').count() == 0
            assert await page.locator('header.topbar').evaluate('(e)=>getComputedStyle(e).position') == 'sticky'
            for tab in ['demo', 'history', 'account']:
                await page.locator('nav [data-tab=' + tab + ']').click()
                assert await page.locator('#page-' + tab + ' .locked-section').is_visible()
                assert await page.locator('#page-' + tab + ' .premium-only').first.is_hidden()
            await page.locator('#page-account .access-open').click()
            assert await page.locator('#access-modal').is_visible()
            assert 't.me/jugo69996' in await page.locator('#access-modal .lock-contact').get_attribute('href')
            await page.evaluate("window.onTelegramAuth({id:'42',username:'guest_123'})")
            await page.wait_for_function("document.querySelector('#top-login').textContent === '@guest_123'")
            assert not await page.locator('#top-premium').is_hidden()
            await page.locator('#access-key').fill('test-private-key')
            await page.locator('#access-premium-form button').click()
            await page.wait_for_function("document.body.dataset.access === 'premium'")
            await page.locator('nav [data-tab=home]').click()
            await page.locator('#private-overview').wait_for(state='visible')
            assert await page.locator('#header-balance').inner_text() != '—'
            await page.wait_for_function("document.querySelector('#summary-heading').textContent === 'Сводка архива · 150 раундов'")
            assert '/api/overview' in calls
            assert 'access:telegram_login' in calls and 'access:premium' in calls
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert not errors, errors
            await browser.close()

    asyncio.run(run())
