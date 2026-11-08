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


def test_telegram_logout_persists_after_reload_and_explicit_login_restores_premium():
    from playwright.async_api import async_playwright, expect
    from urllib.parse import urlparse

    async def run():
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True)
            page=await browser.new_page(viewport={'width':390,'height':844})
            errors,actions=[],[]
            signed_out=False
            private_calls=[]
            page.on('pageerror',lambda e:errors.append(str(e)))

            async def route(req):
                nonlocal signed_out
                url=urlparse(req.request.url)
                path=url.path
                if path=='/app':
                    await req.fulfill(content_type='text/html',body=response('/app')[2])
                elif path in ('/access.js','/history.js','/controls.js','/summary.js',
                              '/admin.js','/site.js','/strategy-tiles.js'):
                    await req.fulfill(content_type='application/javascript',
                                      body=response(path)[2])
                elif path=='/api/access':
                    if req.request.method=='POST':
                        action=req.request.post_data_json.get('action')
                        actions.append(action)
                        if action=='logout':
                            signed_out=True
                        elif action=='login':
                            signed_out=False
                    data=({'verified':False,'registered':False,'premium':False,
                           'username':None,'signed_out':True,'balance_cents':None}
                          if signed_out else
                          {'verified':True,'registered':True,'premium':True,
                           'username':'tester_42','currency':'KRW','balance_cents':7018500,
                           'balance_verified':True})
                    await req.fulfill(content_type='application/json',body=json.dumps(data))
                elif path=='/api/public':
                    await req.fulfill(content_type='application/json',
                        body=json.dumps({'coefficients':['1.10x','1.33x'],
                                         'limit':20,'last_round_at':None}))
                elif path in ('/api/overview','/api/controls','/api/history',
                              '/api/round-summary','/api/admin','/api/site'):
                    private_calls.append(path)
                    if signed_out:
                        await req.fulfill(status=401,content_type='application/json',
                            body='{"error":"Сначала войди через Telegram."}')
                    elif path=='/api/overview':
                        await req.fulfill(content_type='application/json',
                            body=json.dumps({'coefficients':['1.10x','1.33x'],
                                'status':{'fresh':True,'age_seconds':1},
                                'demo':{'balance_cents':7018500,'currency':'KRW',
                                  'enabled':False,'active':False,
                                  'balance_verified':True,'base_stake_cents':100000,
                                  'trigger':3},'last_round_at':None}))
                    elif path=='/api/controls':
                        await req.fulfill(content_type='application/json',
                            body=json.dumps({'demo':{'currency':'KRW','enabled':False,
                                'active':False,'balance_cents':7018500,
                                'balance_verified':True,'base_stake_cents':100000,
                                'trigger':3},'settings':{'notifications':True,'sound':True,
                                'threshold':3,'history_size':20,'delete_delay':10}}))
                    elif path=='/api/history':
                        await req.fulfill(content_type='application/json',
                            body='{"view":"days","days":[],"items":[],"total":0,"page":0}')
                    else:
                        await req.fulfill(status=403,content_type='application/json',
                            body='{"error":"Недоступно."}')
                else:
                    await req.fulfill(content_type='application/javascript',
                        body="window.Telegram={WebApp:{initData:'fixture',ready(){},"
                             "expand(){},setHeaderColor(){},setBackgroundColor(){},"
                             "close(){},BackButton:{onClick(){},hide(){},show(){}},"
                             "HapticFeedback:{selectionChanged(){}}}}")

            await page.route('**/*',route)
            await page.goto('https://example.test/app')
            await page.wait_for_function("document.body.dataset.access === 'premium'")
            await page.locator('nav [data-tab=account]').click()
            await expect(page.locator('#account-logout')).to_be_visible()
            await page.locator('#account-logout').click()
            await expect(page.locator('#account-logout-confirm')).to_be_visible()
            await page.locator('#account-logout-cancel').click()
            await expect(page.locator('#account-logout-confirm')).to_be_hidden()
            assert actions==[]  # Cancel does not revoke auth.
            await page.locator('#account-logout').click()
            await page.locator('#account-logout-confirm-button').click()
            await page.wait_for_function("document.body.dataset.access === 'guest'")
            assert actions==['logout']
            await expect(page.locator('#header-balance')).to_have_text('—')
            await expect(page.locator('#account-logout-card')).to_be_hidden()
            await expect(page.locator('#page-account .access-open')).to_have_text(
                'Войти через Telegram')
            count=len(private_calls)

            # New navigation/reload stays signed out even though Telegram initData
            # is still present. No private account data should be fetched.
            await page.reload()
            await page.wait_for_function("document.body.dataset.access === 'guest'")
            await page.locator('nav [data-tab=account]').click()
            await expect(page.locator('#account-logout-card')).to_be_hidden()
            await expect(page.locator('#page-account .access-open')).to_be_visible()
            assert len(private_calls)==count
            await page.locator('#page-account .access-open').click()
            await expect(page.locator('#access-telegram-login')).to_be_visible()
            await page.locator('#access-telegram-login').click()
            await page.wait_for_function("document.body.dataset.access === 'premium'")
            assert actions==['logout','login']
            await expect(page.locator('#header-balance')).not_to_have_text('—')
            await expect(page.locator('#account-logout')).to_be_visible()
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert not errors,errors
            await browser.close()

    asyncio.run(run())
