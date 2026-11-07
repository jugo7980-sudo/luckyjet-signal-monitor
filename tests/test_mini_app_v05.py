"""Live home refresh UI and 100ms non-overlapping poll regression."""
import asyncio
import json
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from mini_app import response


def test_v05_compact_refresh_summary_and_throttled_polling():
    from playwright.async_api import async_playwright

    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={'width': 390, 'height': 844})
            errors, limits = [], []
            counter = {'active': 0, 'peak': 0}
            page.on('pageerror', lambda error: errors.append(str(error)))

            async def route(req):
                if req.request.url.endswith('/access.js'):
                    await req.fulfill(content_type='application/javascript',
                        body="window.accessState={premium:true,registered:true,username:'tester'};document.body.dataset.access='premium';window.canUsePremium=()=>true;window.refreshAccessOverview?.();")
                    return
                url = req.request.url
                if url.startswith('https://example.test/app'):
                    await req.fulfill(content_type='text/html', body=response('/app')[2])
                elif url.endswith(('/history.js', '/controls.js', '/admin.js', '/site.js')):
                    await req.fulfill(content_type='application/javascript',
                        body=response('/' + url.split('/')[-1])[2])
                elif '/api/overview' in url:
                    counter['active'] += 1
                    counter['peak'] = max(counter['peak'], counter['active'])
                    try:
                        limit = int(parse_qs(urlparse(url).query).get('limit', ['20'])[0])
                        limits.append(limit)
                        await asyncio.sleep(0.07)
                        count = 7 if limit == 20 else limit
                        coefficients = ['1.50x', '3.00x', '12.00x'] + ['1.60x'] * (count - 3)
                        data = {
                            'limit': limit, 'coefficients': coefficients,
                            'last_round_at': int(time.time()),
                            'summary': {'total': count, 'average': 2.25, 'maximum': 12.0,
                                'below_two': count - 2, 'streak': 1},
                            'status': {'last': '1.50x', 'age_seconds': 1, 'fresh': True,
                                'threshold': 4, 'streak_count': 1, 'uptime_seconds': 123},
                            'demo': {'balance_cents': 1000000, 'enabled': False,
                                'active': False, 'step': None, 'base_stake_cents': 1000,
                                'trigger': 4},
                        }
                        await req.fulfill(content_type='application/json', body=json.dumps(data))
                    finally:
                        counter['active'] -= 1
                elif '/api/controls' in url:
                    await req.fulfill(content_type='application/json', body=json.dumps({
                        'admin': False,
                        'demo': {'enabled': False, 'active': False, 'balance_cents': 1000000,
                            'base_stake_cents': 1000, 'trigger': 4, 'low_streak': 0},
                        'settings': {'notifications': True, 'sound': True, 'threshold': 4,
                            'history_size': 20, 'delete_delay': 10}}))
                elif '/api/admin' in url:
                    await req.fulfill(status=403, content_type='application/json', body='{"error":"Forbidden"}')
                elif '/api/site' in url:
                    await req.fulfill(status=403, content_type='application/json', body='{"error":"Forbidden"}')
                else:
                    await req.fulfill(content_type='application/javascript',
                        body="window.Telegram={WebApp:{initData:'fixture',ready(){},expand(){},setHeaderColor(){},setBackgroundColor(){},close(){},BackButton:{onClick(){},hide(){},show(){}},HapticFeedback:{selectionChanged(){}}}}")

            await page.route('**/*', route)
            await page.goto('https://example.test/app')
            button = page.locator('#refresh-data')
            assert await button.count() == 1
            assert await button.locator('svg.refresh-rotate').count() == 1
            assert await page.locator('.below-tiles #refresh-data').count() == 1
            assert await page.locator('#page-home > #refresh-data').count() == 0
            await page.wait_for_function("document.querySelector('#summary-heading').textContent.includes('150 раундов')")
            await page.wait_for_function("document.querySelectorAll('#coef-grid .coef').length === 7")
            assert '0,1 с' in await page.locator('#live-status').inner_text()
            await page.wait_for_function("document.querySelector('#refresh-data').dataset.state==='idle'")
            # Inspect the synchronous click handler before the 70ms mocked
            # response can complete. Waiting separately for the transient
            # loading state is flaky on a busy CI runner.
            feedback = await page.evaluate("""() => {
                const button = document.querySelector('#refresh-data');
                button.click();
                return {state: button.dataset.state, busy: button.getAttribute('aria-busy')};
            }""")
            assert feedback == {'state': 'loading', 'busy': 'true'}
            await page.wait_for_function("document.querySelector('#refresh-data').dataset.state==='success'")
            assert await button.locator('svg.refresh-check').count() == 1

            await page.locator('#recent-limit').select_option('50')
            assert await page.locator('#summary-heading').inner_text() == 'Сводка архива · 150 раундов'
            await page.wait_for_function("document.querySelectorAll('#coef-grid .coef').length === 50")
            await page.locator('#recent-limit').select_option('100')
            assert await page.locator('#summary-heading').inner_text() == 'Сводка архива · 150 раундов'
            await page.wait_for_function("document.querySelectorAll('#coef-grid .coef').length === 100")
            # Cached coefficient nodes should survive polls when the source data is unchanged.
            await page.evaluate("window.__firstCell = document.querySelector('#coef-grid .coef')")
            recorded = len(limits)
            for _ in range(50):
                if len(limits) >= recorded + 2:
                    break
                await asyncio.sleep(0.02)
            assert len(limits) >= recorded + 2
            assert await page.evaluate("window.__firstCell === document.querySelector('#coef-grid .coef')")
            assert counter['peak'] == 1

            await page.locator('nav [data-tab=account]').click()
            await asyncio.sleep(0.15)
            count_after_switch = len(limits)
            await asyncio.sleep(0.35)
            assert len(limits) == count_after_switch
            await page.locator('nav [data-tab=home]').click()
            await page.wait_for_function("document.querySelector('#coef-grid .coef') !== null")
            count_before = len(limits)
            for _ in range(50):
                if len(limits) > count_before:
                    break
                await asyncio.sleep(0.02)
            assert len(limits) > count_before
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert not errors, errors
            await browser.close()

    asyncio.run(run())
