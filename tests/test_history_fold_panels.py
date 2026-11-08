"""UI regression for compact, exclusive factual-history panels."""
import asyncio
import json
from urllib.parse import parse_qs, urlparse

from mini_app import response


def test_mobile_history_folded_strategy_stats_full_long_cycle_distribution_and_archive():
    from playwright.async_api import async_playwright, expect

    async def run():
        async with async_playwright() as browser_tool:
            browser = await browser_tool.chromium.launch(headless=True)
            for width in (390, 320):
                page = await browser.new_page(viewport={'width': width, 'height': 844})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                calls = []
                amounts = [{'coefficient': '1.47', 'stake_cents': 15000,
                            'payout_cents': 0} for _ in range(92)]
                cycle = {
                    'strategy': 'high_gap', 'index': 0, 'currency': 'KRW',
                    'started_at': '2026-11-08T02:59:00Z',
                    'ended_at': '2026-11-08T03:32:00Z', 'steps': 92,
                    'outcome': 'reference_changed', 'reference_threshold': '100',
                    'reference_coefficient': '364.22', 'cashout_multiplier': '250',
                    'wait_rounds': 50, 'total_stake_cents': 1380000,
                    'payout_cents': 0, 'net_cents': -1380000,
                    'balance_before_cents': 7500000,
                    'balance_after_cents': 6120000, 'rounds': amounts,
                }
                stats = {'currency': 'KRW', 'total': 1, 'maximum': 100,
                         'recovered': 0, 'lost': 0, 'stopped': 0, 'incomplete': 0,
                         'reference_changed': 1, 'history_net_cents': -1380000,
                         'average_steps': 92}
                day = '2026-11-08'
                dist = {'complete_scan': True, 'total': 100,
                        'counts': {'below_1_5': 22, '1.5': 20, '2': 58, 'above_150': 0},
                        'high_ranges': {k: 0 for k in ('151-199', '200-299', '300-499',
                           '500-799', '800-999', '1000-1499', '1500-1999',
                           '2000-2999', '3000-4999', '5000+')},
                        'high_values': {}}
                async def route(request):
                    path = urlparse(request.request.url).path
                    if path == '/app':
                        await request.fulfill(content_type='text/html', body=response('/app')[2])
                    elif path == '/access.js':
                        await request.fulfill(content_type='application/javascript',
                            body="window.accessState={premium:true,registered:true,username:'tester'};"
                                 "document.body.dataset.access='premium';window.canUsePremium=()=>true;")
                    elif path in ('/history.js', '/summary.js'):
                        await request.fulfill(content_type='application/javascript',
                                              body=response(path)[2])
                    elif path == '/api/history':
                        view = parse_qs(urlparse(request.request.url).query).get('view', ['days'])[0]
                        calls.append(view)
                        data = {'view': view, 'page': 0, 'total': 1}
                        if view in ('days', 'archive_days'):
                            data.update(days=[day])
                        elif view in ('day', 'archive_day'):
                            data.update(day=day, summary={'total': 100},
                                        distribution=dist)
                        elif view in ('high_days', 'archive_high_days'):
                            data.update(days=[day])
                        elif view in ('high_day', 'archive_high_day'):
                            data.update(day=day, items=[{'id': 'high1', 'value': '120',
                                    'stamp': 1794100000, 'rounds': 5}])
                        elif view in ('max_steps', 'archive_max_steps'):
                            data.update(days=[{'day': day, 'steps': 92}])
                        elif view == 'high_cycles':
                            data.update(stats=stats, items=[{k:v for k,v in cycle.items()
                                if k != 'rounds'}])
                        elif view == 'high_cycle':
                            data['item'] = cycle
                        elif view == 'cycles':
                            data.update(stats={**stats, 'total': 1}, items=[{
                                'index': 0, 'strategy': 'classic', 'currency': 'KRW',
                                'steps': 1, 'net_cents': 15000, 'outcome': 'recovered',
                                'ended_at': '2026-11-08T02:30:00Z'}])
                        elif view == 'archive':
                            data.update(day=day, aggregate={'total': 100, 'lt2': 42},
                                        items=[{'type': 'round', 'round_id': '1',
                                                'coefficient': '2.7', 'observed_at': 1794100000000}],
                                        integrity={'status': 'poll_gaps', 'archived_rounds': 100,
                                                   'gaps': 2, 'poll_coverage': {'status': 'not_recorded'}})
                        else:
                            data.update(days=[], items=[])
                        await request.fulfill(content_type='application/json', body=json.dumps(data))
                    elif path == '/api/overview':
                        await request.fulfill(content_type='application/json', body=json.dumps({
                            'coefficients': [], 'last_round_at': None,
                            'demo': {'currency': 'KRW', 'balance_cents': 7500000,
                                     'enabled': False, 'active': False, 'high_gap': {}},
                            'status': {'age_seconds': 1, 'fresh': True,
                                       'streak_count': 0, 'threshold': 3}}))
                    else:
                        await request.fulfill(content_type='application/javascript',
                            body="window.Telegram={WebApp:{initData:'fixture',ready(){},"
                                 "expand(){},setHeaderColor(){},setBackgroundColor(){},"
                                 "close(){},BackButton:{onClick(){},hide(){},show(){}},"
                                 "HapticFeedback:{selectionChanged(){}}}}")
                await page.route('**/*', route)
                await page.goto('https://example.test/app')
                await page.locator('nav [data-tab=history]').click()
                await page.locator('[data-history-view=cycles]').click()
                await page.locator('#demo-history-strategy-high').click()
                stats_panel = page.locator('#history-demo-stats')
                list_panel = page.locator('#history-demo-cycles')
                await expect(stats_panel).to_be_visible()
                await expect(stats_panel).not_to_have_attribute('open', '')
                await expect(list_panel).not_to_have_attribute('open', '')
                await stats_panel.locator('summary').click()
                await expect(stats_panel).to_have_attribute('open', '')
                await expect(stats_panel.locator('.history-metric').first).to_be_visible()
                await list_panel.locator('summary').click()
                await expect(stats_panel).not_to_have_attribute('open', '')
                await expect(list_panel).to_have_attribute('open', '')
                await page.locator('#history-content button.history-item').first.click()
                steps = page.locator('#history-cycle-steps')
                outcome = page.locator('#history-cycle-outcome')
                params = page.locator('#history-cycle-params')
                await expect(steps).to_be_visible()
                assert await steps.locator('.history-metric').count() == 10
                assert 'последний: шаг 92' in await steps.locator('summary').inner_text()
                assert not await steps.locator('.history-metric').first.is_visible()
                assert await page.locator('.history-fold-steps').count() == 1
                assert await page.locator('#history-cycle-steps .history-fold').count() == 0
                await steps.locator('summary').click()
                await expect(steps.locator('.history-metric').first).to_contain_text('Шаг 92')
                await steps.locator('.history-step-more').click()
                await expect(steps.locator('.history-metric')).to_have_count(92)
                await expect(steps.locator('.history-metric').last).to_contain_text('Шаг 1')
                await outcome.locator('summary').click()
                await expect(steps).not_to_have_attribute('open', '')
                await expect(outcome).to_have_attribute('open', '')
                await expect(outcome.locator('.history-fold-summary')).to_contain_text('Чистый итог')
                await params.locator('summary').click()
                await expect(outcome).not_to_have_attribute('open', '')
                await expect(params).to_have_attribute('open', '')
                await page.locator('#history-back').click()
                await expect(list_panel).to_have_attribute('open', '')
                await page.locator('nav [data-tab=home]').click()
                await page.locator('nav [data-tab=history]').click()
                await expect(page.locator('#demo-history-strategy-high')).to_have_attribute(
                    'aria-expanded', 'false')

                await page.locator('[data-history-view=days]').click()
                await page.locator('#history-days-list > summary').click()
                await page.locator('#history-content .history-item').first.click()
                distribution = page.locator('#history-coeff-distribution')
                await expect(distribution).to_be_visible()
                await expect(distribution).not_to_have_attribute('open', '')
                assert await distribution.locator('.history-distribution-group').count() == 18
                await distribution.locator('summary').first.click()
                bands = distribution.locator('.history-distribution-group')
                await bands.nth(0).locator('summary').click()
                await bands.nth(1).locator('summary').click()
                await expect(bands.nth(0)).not_to_have_attribute('open', '')
                await expect(bands.nth(1)).to_have_attribute('open', '')

                await page.locator('[data-history-view=high_days]').click()
                await expect(page.locator('#history-high-days-list')).to_be_visible()
                await page.locator('#history-high-days-list > summary').click()
                await page.locator('#history-content .history-item').first.click()
                await expect(page.locator('#history-high-event-list')).to_be_visible()

                await page.locator('[data-history-view=max_steps]').click()
                await expect(page.locator('#history-max-step-days')).to_be_visible()
                await expect(page.locator('#history-max-step-days')).not_to_have_attribute('open', '')
                await page.locator('#history-max-step-days > summary').click()
                await expect(page.locator('#history-max-step-days .history-item')).to_be_visible()

                await page.locator('[data-history-view=days]').click()
                await page.locator('#history-management > summary').click()
                await page.locator('#history-archive').click()
                await page.locator('#history-days-list > summary').click()
                await page.locator('#history-days-list .history-item').first.click()
                await expect(page.locator('#history-coeff-distribution')).to_be_visible()
                assert 'archive_day' in calls
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert not errors, errors
                await page.close()
            await browser.close()
    asyncio.run(run())
