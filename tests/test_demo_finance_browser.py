"""Mobile regression: nested DEMO income/expense without changing history tabs."""
import asyncio
import json
from urllib.parse import parse_qs, urlparse

from mini_app import response


def test_mobile_demo_finance_filters_breakdown_paging_and_panel_reset():
    from playwright.async_api import async_playwright, expect

    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            for width in (320, 390):
                page = await browser.new_page(viewport={'width': width, 'height': 844})
                failures, requests = [], []
                page.on('pageerror', lambda exc: failures.append(str(exc)))

                operations = []
                for i in range(45):
                    strategy = ['classic', 'high_gap', 'target_streak'][i % 3]
                    operations.append({
                        'id': 'cycle-' + str(i) + ':0:net',
                        'type': 'expense', 'currency': 'KRW',
                        'strategy': strategy, 'step': i+1,
                        'amount_cents': 15000, 'net_cents': -15000,
                        'stake_cents': 15000, 'payout_cents': 0,
                        'round_id': 'r' + str(i),
                        'coefficient': '1.20', 'cashout_multiplier': '1.57',
                        'time': '2026-11-08T02:00:00+00:00',
                        'time_exact': i != 44,
                        'balance_before_cents': 1000000,
                        'balance_after_cents': 985000,
                    })
                operations.insert(0, {
                    'id': 'cycle-winning:0:net', 'type': 'income',
                    'currency': 'KRW', 'strategy': 'target_streak', 'step': 1,
                    'amount_cents': 30000, 'net_cents': 30000,
                    'stake_cents': 52700, 'payout_cents': 82700,
                    'round_id': 'r0', 'coefficient': '1.57',
                    'cashout_multiplier': '1.57', 'time': '2026-11-08T02:00:00+00:00',
                    'time_exact': True, 'balance_before_cents': 985000,
                    'balance_after_cents': 1067700,
                })
                async def route(req):
                    parsed = urlparse(req.request.url)
                    path = parsed.path
                    if path == '/app':
                        await req.fulfill(content_type='text/html', body=response('/app')[2])
                    elif path == '/access.js':
                        await req.fulfill(content_type='application/javascript',
                            body="window.accessState={premium:true,registered:true,username:'tester'};"
                                 "document.body.dataset.access='premium';window.canUsePremium=()=>true;")
                    elif path in ('/history.js','/summary.js','/controls.js'):
                        await req.fulfill(content_type='application/javascript', body=response(path)[2])
                    elif path == '/api/history':
                        query = parse_qs(parsed.query)
                        view = query.get('view', ['days'])[0]
                        requests.append(query)
                        if view == 'demo_finance':
                            strategy = query.get('strategy', ['all'])[0]
                            flow = query.get('flow', ['all'])[0]
                            filtered = [x for x in operations
                                if (strategy == 'all' or x['strategy'] == strategy)
                                and (flow == 'all' or x['type'] == flow)]
                            number = int(query.get('page', ['0'])[0])
                            data = {
                                'view': 'demo_finance', 'page': number,
                                'page_size': 40, 'total': len(filtered),
                                'period': query.get('period', ['all'])[0],
                                'strategy': strategy, 'flow': flow,
                                'day': query.get('day', [None])[0],
                                'totals': [{'currency': 'KRW', 'income_cents': 30000,
                                    'expense_cents': 675000, 'net_cents': -645000,
                                    'operations': 46}],
                                'items': filtered[number*40:(number+1)*40],
                                'truncated': False, 'invalid_rows': 0,
                                'unknown_time_rows': 0,
                                'time_note': 'Дата старого шага может соответствовать окончанию цикла.',
                            }
                        else:
                            data = {'view': view, 'total': 0, 'page': 0,
                                    'items': [], 'days': [], 'stats': {
                                        'currency': 'KRW', 'total': 0,
                                        'recovered': 0, 'lost': 0, 'incomplete': 0,
                                        'stopped': 0, 'history_net_cents': 0,
                                        'average_steps': 0}}
                        await req.fulfill(content_type='application/json', body=json.dumps(data))
                    elif path == '/api/overview':
                        await req.fulfill(content_type='application/json', body=json.dumps({
                            'coefficients': [], 'last_round_at': None,
                            'demo': {'enabled': False, 'currency': 'KRW',
                                     'balance_cents': 1000000,
                                     'high_gap': {'enabled': False}},
                            'status': {'fresh': True, 'age_seconds': 1,
                                       'streak_count': 0, 'threshold': 3}}))
                    else:
                        await req.fulfill(content_type='application/javascript',
                            body="window.Telegram={WebApp:{initData:'fixture',ready(){},expand(){},"
                                 "setHeaderColor(){},setBackgroundColor(){},close(){},"
                                 "BackButton:{onClick(){},hide(){},show(){}},"
                                 "HapticFeedback:{selectionChanged(){}}}}")
                await page.route('**/*', route)
                await page.goto('https://example.test/app')
                await page.locator('nav [data-tab=history]').click()
                await page.locator('[data-history-view=cycles]').click()
                await expect(page.locator('#demo-history-strategy-classic')).to_be_visible()
                assert await page.locator('[data-history-view]').count() == 4
                assert await page.locator('#demo-history-strategy-classic').count() == 1
                assert await page.locator('#demo-history-strategy-high').count() == 1
                assert await page.locator('#demo-history-strategy-target').count() == 1
                choice = page.locator('#demo-history-strategy-finance')
                await expect(choice).to_be_visible()
                await choice.click()
                await expect(choice).to_have_attribute('aria-expanded', 'true')
                await expect(page.locator('#history-finance-operations')).to_be_visible()
                await expect(page.locator('#history-finance-operations')).not_to_have_attribute('open', '')
                assert await page.locator('#history-finance-summary').count() == 0
                assert await page.locator('.history-finance-summary').count() == 1
                assert '+300' in await page.locator('.history-finance-summary').inner_text()
                await expect(page.locator('#history-reset-panel')).to_be_hidden()
                await page.locator('#history-finance-operations > summary').click()
                await expect(page.locator('#history-finance-operations')).to_have_attribute('open', '')
                assert await page.locator('.history-finance-entry').count() == 40
                await page.locator('.history-finance-entry summary').first.click()
                await expect(page.locator('.history-finance-entry-body').first).to_be_visible()
                content = await page.locator('.history-finance-entry-body').first.inner_text()
                assert 'Раунд' in content and 'Ставка' in content and 'Выплата' in content
                assert 'Чистый результат' in content
                assert '+300' in await page.locator('.history-finance-entry-head').first.inner_text()
                await expect(page.locator('#history-pager')).to_be_visible()
                await page.locator('#history-next').click()
                await page.wait_for_function(
                    'document.querySelectorAll(".history-finance-entry").length === 6')
                assert '1 / 2' not in await page.locator('#history-page-number').inner_text()
                assert '2 / 2' in await page.locator('#history-page-number').inner_text()
                await page.locator('[data-finance-filter=flow]').select_option('income')
                await page.wait_for_function(
                    'document.querySelectorAll(".history-finance-entry").length === 1')
                assert await page.locator('.history-finance-summary').count() == 1
                await page.locator('[data-finance-filter=strategy]').select_option('target_streak')
                await expect(page.locator('[data-finance-filter=strategy]')).to_have_value('target_streak')
                await page.locator('[data-finance-filter=period]').select_option('day')
                await expect(page.locator('#history-finance-day')).to_be_visible()
                assert any(q.get('period') == ['day'] and 'day' in q for q in requests)

                await page.locator('nav [data-tab=home]').click()
                await page.locator('nav [data-tab=history]').click()
                await expect(page.locator('#demo-history-strategy-finance')).to_have_attribute(
                    'aria-expanded', 'false')
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert not failures, failures
                await page.close()
            await browser.close()

    asyncio.run(run())
