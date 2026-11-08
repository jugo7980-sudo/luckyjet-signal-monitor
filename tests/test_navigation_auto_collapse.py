"""Bottom-tab navigation resets only visual expansion state, not DEMO settings."""
import asyncio
import json
from urllib.parse import parse_qs, urlparse

from mini_app import response


def test_bottom_navigation_closes_all_disclosures_and_keeps_strategy_data():
    from playwright.async_api import async_playwright, expect

    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            for width in (390, 320):
                page = await browser.new_page(viewport={'width': width, 'height': 844})
                errors = []
                page.on('pageerror', lambda err: errors.append(str(err)))
                high = {'enabled': True, 'phase': 'waiting', 'reference': '100',
                        'target': '250', 'wait_rounds': 50, 'elapsed': 21,
                        'remaining': 29, 'stake_cents': 15000,
                        'filter_enabled': True, 'filter_threshold': '10',
                        'filter_limit': '3', 'filter_count': 1,
                        'reference_value': '364.22', 'steps': 0,
                        'total_stake_cents': 0}
                demo = {'enabled': True, 'active': False, 'step': None,
                        'balance_cents': 7500000, 'balance_verified': True,
                        'initial_balance_cents': 10000000, 'currency': 'KRW',
                        'reset_revision': 'test', 'base_stake_cents': 15000,
                        'trigger': 3, 'low_streak': 0, 'high_gap': high}
                control = {'admin': False, 'demo': demo, 'high_gap': high,
                           'settings': {'notifications': True, 'sound': True,
                                        'threshold': 3, 'history_size': 20,
                                        'delete_delay': 10}}
                async def route(request):
                    url = request.request.url
                    path = urlparse(url).path
                    if url.startswith('https://example.test/app'):
                        await request.fulfill(content_type='text/html', body=response('/app')[2])
                    elif path == '/access.js':
                        await request.fulfill(content_type='application/javascript',
                            body="window.accessState={premium:true,registered:true,username:'tester'};"
                                 "document.body.dataset.access='premium';"
                                 "window.canUsePremium=()=>true;window.refreshAccessOverview?.();")
                    elif path in ('/summary.js', '/history.js', '/controls.js'):
                        await request.fulfill(content_type='application/javascript',
                            body=response(path)[2])
                    elif path in ('/admin.js', '/site.js'):
                        await request.fulfill(content_type='application/javascript', body='')
                    elif path == '/api/overview':
                        await request.fulfill(content_type='application/json',
                            body=json.dumps({'coefficients': ['1.2x', '2.4x'],
                                             'last_round_at': 1794000000, 'demo': demo,
                                             'status': {'age_seconds': 1, 'fresh': True,
                                                        'streak_count': 0, 'threshold': 3,
                                                        'last': '2.4x',
                                                        'uptime_seconds': 120}}))
                    elif path == '/api/controls':
                        await request.fulfill(content_type='application/json',
                            body=json.dumps(control))
                    elif path == '/api/history':
                        view = parse_qs(urlparse(url).query).get('view', ['days'])[0]
                        if view == 'cycles':
                            data = {'view': view, 'total': 1, 'page': 0,
                                    'stats': {'currency': 'KRW', 'total': 1, 'maximum': 100,
                                              'recovered': 1, 'lost': 0, 'incomplete': 0,
                                              'stopped': 0, 'history_net_cents': 15000,
                                              'average_steps': 1},
                                    'items': [{'index': 0, 'currency': 'KRW',
                                               'steps': 1, 'outcome': 'recovered',
                                               'net_cents': 15000,
                                               'ended_at': '2026-11-08T02:00:00Z'}]}
                        else:
                            data = {'view': view, 'total': 0, 'page': 0,
                                    'days': [], 'items': []}
                        await request.fulfill(content_type='application/json', body=json.dumps(data))
                    elif path == '/api/round-summary':
                        await request.fulfill(content_type='application/json',
                            body=json.dumps({'rounds_requested': 150, 'rounds_available': 0,
                                             'current': {}, 'previous': {},
                                             'matches': [],
                                             'quality': {'complete_window': False}}))
                    else:
                        await request.fulfill(content_type='application/javascript',
                            body="window.Telegram={WebApp:{initData:'fixture',ready(){},"
                                 "expand(){},setHeaderColor(){},setBackgroundColor(){},"
                                 "close(){},BackButton:{onClick(){},hide(){},show(){}},"
                                 "HapticFeedback:{selectionChanged(){}}}}")

                await page.route('**/*', route)
                await page.goto('https://example.test/app')
                await page.locator('#summary-accordion').wait_for(state='visible')
                assert not await page.locator('#summary-accordion').evaluate('(n) => n.open')
                await page.locator('#summary-accordion summary').first.click()
                await expect(page.locator('#summary-accordion')).to_have_attribute('open', '')
                await page.locator('#summary-stats-toggle').click()
                await expect(page.locator('#summary-stats-toggle')).to_have_attribute('aria-expanded', 'true')
                # Clicking the currently selected tab does not count as leaving it.
                await page.locator('nav [data-tab=home]').click()
                assert await page.locator('#summary-accordion').evaluate('(n) => n.open')
                await page.locator('nav [data-tab=demo]').click()
                assert not await page.locator('#summary-accordion').evaluate('(n) => n.open')
                assert await page.locator('#summary-stats-toggle').get_attribute('aria-expanded') == 'false'
                assert await page.locator('#summary-extra-stats').is_hidden()

                await page.locator('#demo-high-accordion summary').first.click()
                await expect(page.locator('#demo-high-accordion')).to_have_attribute('open', '')
                await page.locator('#demo-management summary').click()
                await page.locator('#demo-metrics-disclosure summary').click()
                assert await page.locator('#demo-management').evaluate('(n) => n.open')
                assert await page.locator('#demo-metrics-disclosure').evaluate('(n) => n.open')
                # Nested help has its own toggle instead of a <details> element.
                await page.evaluate("""() => {
                    const button = document.getElementById('high-gap-help-toggle');
                    const body = document.getElementById('high-gap-help-body');
                    button.setAttribute('aria-expanded', 'true');
                    body.hidden = false;
                }""")
                await page.locator('nav [data-tab=history]').click()
                for id in ('demo-high-accordion', 'demo-management', 'demo-metrics-disclosure'):
                    assert not await page.locator('#' + id).evaluate('(n) => n.open')
                assert await page.locator('#high-gap-help-toggle').get_attribute('aria-expanded') == 'false'
                assert await page.locator('#high-gap-help-body').is_hidden()

                await page.locator('[data-history-view=cycles]').click()
                await page.locator('#demo-history-strategy-classic').click()
                await expect(page.locator('#demo-history-strategy-classic')).to_have_attribute('aria-expanded', 'true')
                await page.locator('nav [data-tab=account]').click()
                await page.locator('nav [data-tab=history]').click()
                await expect(page.locator('#demo-history-strategy-classic')).to_have_attribute('aria-expanded', 'false')
                assert await page.locator('#history-content .history-item').count() == 0

                await page.locator('nav [data-tab=account]').click()
                assert not await page.locator('#low-streak-notice-options').evaluate('(n) => n.open')
                await page.locator('#open-settings').click()
                await expect(page.locator('#settings-panel')).to_be_visible()
                await page.locator('#low-streak-notice-options summary').click()
                await page.locator('#app-release-notes summary').click()
                assert await page.locator('#low-streak-notice-options').evaluate('(n) => n.open')
                await page.locator('nav [data-tab=home]').click()
                assert await page.locator('#settings-panel').is_hidden()
                assert await page.locator('#open-settings').get_attribute('aria-expanded') == 'false'
                assert not await page.locator('#low-streak-notice-options').evaluate('(n) => n.open')
                assert not await page.locator('#app-release-notes').evaluate('(n) => n.open')
                await page.locator('nav [data-tab=account]').click()
                assert await page.locator('#settings-panel').is_hidden()
                await page.locator('nav [data-tab=demo]').click()
                assert not await page.locator('#demo-high-accordion').evaluate('(n) => n.open')
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert not errors, errors
                await page.close()
            await browser.close()
    asyncio.run(run())
