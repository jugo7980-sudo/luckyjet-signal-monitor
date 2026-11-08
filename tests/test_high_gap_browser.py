"""Mobile browser: both collapsible strategies, editable high-gap settings, history."""
import asyncio
import json
from urllib.parse import parse_qs, urlparse

from mini_app import response


def test_second_demo_strategy_mobile_controls_and_independent_history():
    from playwright.async_api import async_playwright

    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={'width': 390, 'height': 844})
            errors, posted, history_views = [], [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            high = {'enabled': False, 'phase': 'waiting_reference',
                    'reference': '100', 'target': '100', 'wait_rounds': 100,
                    'stake_cents': 1000, 'elapsed': 0, 'remaining': None,
                    'reference_value': '', 'reference_at': None, 'steps': 0,
                    'total_stake_cents': 0, 'active': False,
                    'last_outcome': None}
            demo = {'currency': 'KRW', 'reset_revision': 'revision',
                    'enabled': True, 'active': False, 'step': None,
                    'balance_cents': 1000000, 'balance_verified': True,
                    'base_stake_cents': 1000, 'trigger': 4, 'low_streak': 0,
                    'high_gap': high}
            controls_data = {
                'demo': demo, 'high_gap': high, 'admin': False, 'probe': None,
                'settings': {'notifications': True, 'sound': True,
                             'threshold': 4, 'history_size': 20,
                             'delete_delay': 10},
            }

            async def route(request):
                url = request.request.url
                if url.endswith('/access.js'):
                    await request.fulfill(content_type='application/javascript',
                        body="window.accessState={premium:true,registered:true,username:'tester'};document.body.dataset.access='premium';window.canUsePremium=()=>true;")
                elif url.split('?', 1)[0].endswith('/controls.js'):
                    await request.fulfill(content_type='application/javascript',
                        body=response('/controls.js')[2])
                elif url.split('?', 1)[0].endswith('/history.js'):
                    await request.fulfill(content_type='application/javascript',
                        body=response('/history.js')[2])
                elif url.startswith('https://example.test/app'):
                    await request.fulfill(content_type='text/html', body=response('/app')[2])
                elif '/api/controls' in url:
                    if request.request.method == 'POST':
                        payload = request.request.post_data_json
                        posted.append(payload)
                        if payload['action'] == 'high_gap_config':
                            values = payload['value']
                            high.update({'reference': values['reference'],
                                         'target': values['target'],
                                         'wait_rounds': values['wait_rounds'],
                                         'stake_cents': 1000})
                        if payload['action'] == 'high_gap_toggle':
                            high.update({'enabled': not high['enabled'],
                                         'phase': 'waiting', 'reference_value': '134.20',
                                         'elapsed': 73, 'remaining': 27})
                    await request.fulfill(content_type='application/json',
                        body=json.dumps({'message': 'Сохранено.', **controls_data}))
                elif '/api/history' in url:
                    q = parse_qs(urlparse(url).query)
                    view = q.get('view', ['days'])[0]
                    history_views.append(view)
                    if view == 'high_cycles':
                        data = {'view': view, 'total': 1, 'page': 0,
                                'items': [{'index': 0, 'currency': 'KRW',
                                           'outcome': 'recovered',
                                           'started_at': '2026-11-08T01:00:00Z',
                                           'ended_at': '2026-11-08T01:05:00Z',
                                           'strategy': 'high_gap',
                                           'cashout_multiplier': '100',
                                           'reference_threshold': '100',
                                           'steps': 3, 'net_cents': 97000}]}
                    else:
                        data = {'view': view, 'total': 0, 'page': 0,
                                'days': [], 'items': []}
                    await request.fulfill(content_type='application/json', body=json.dumps(data))
                elif '/api/overview' in url:
                    await request.fulfill(content_type='application/json',
                        body=json.dumps({
                            'coefficients': [], 'last_round_at': None, 'summary': None,
                            'demo': demo,
                            'status': {'age_seconds': None, 'last': None,
                                       'fresh': False, 'streak_count': 0,
                                       'threshold': 4, 'uptime_seconds': 50},
                        }))
                else:
                    await request.fulfill(content_type='application/javascript',
                        body="window.Telegram={WebApp:{initData:'fixture',ready(){},expand(){},setHeaderColor(){},setBackgroundColor(){},close(){},showConfirm(t,cb){cb(true)},BackButton:{onClick(){},hide(){},show(){}},HapticFeedback:{selectionChanged(){}}}}")
            await page.route('**/*', route)
            await page.goto('https://example.test/app')
            await page.locator('nav [data-tab=demo]').click()
            await page.locator('#demo-classic-accordion').wait_for()
            assert not await page.locator('#demo-classic-accordion').evaluate('(n) => n.open')
            await page.locator('#demo-classic-accordion summary').click()
            await page.locator('#demo-controls').wait_for(state='visible', timeout=10000)
            assert await page.locator('#demo-classic-accordion').evaluate('(n) => n.open')
            assert not await page.locator('#demo-high-accordion').evaluate('(n) => n.open')
            await page.locator('#demo-high-accordion summary').click()
            assert await page.locator('#demo-high-accordion').evaluate('(n) => n.open')
            await page.locator('#high-gap-panel').wait_for(state='visible', timeout=10000)
            await page.locator('#high-gap-reference').fill('100')
            await page.locator('#high-gap-wait').fill('100')
            await page.locator('#high-gap-target').fill('1.50')
            await page.locator('#high-gap-stake').fill('1000')
            await page.locator('#high-gap-filter-enabled').check()
            await page.locator('#high-gap-filter-threshold').fill('10')
            await page.locator('#high-gap-filter-limit').fill('999999999999999999999999')
            await page.locator('#high-gap-save').click()
            await page.get_by_text('Сохранено.', exact=True).wait_for()
            assert posted[0] == {'action': 'high_gap_config', 'value': {
                'reference': '100', 'wait_rounds': 100,
                'target': '1.50', 'stake': '1000',
                'filter_enabled': True, 'filter_threshold': '10',
                'filter_limit': '999999999999999999999999',
            }}
            await page.locator('#high-gap-toggle').click()
            from playwright.async_api import expect
            await expect(page.locator('#high-gap-state')).to_contain_text(
                'Ожидает 27 раундов', timeout=10000)
            assert posted[1] == {'action': 'high_gap_toggle'}
            assert await page.locator('#high-gap-history').count() == 0
            await page.locator('nav [data-tab=history]').click()
            await page.locator('[data-history-view=cycles]').click()
            await page.locator('#demo-history-strategy-high').click()
            await page.get_by_text('История циклов').wait_for()
            assert 'high_cycles' in history_views
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert not errors, errors
            await browser.close()
    asyncio.run(run())
