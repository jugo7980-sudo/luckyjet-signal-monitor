"""Mobile history reset and read-only archive navigation."""
import asyncio
import json
from urllib.parse import parse_qs, urlparse

from mini_app import response


def test_mobile_archive_and_reset_controls():
    from playwright.async_api import async_playwright

    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={'width': 390, 'height': 844})
            errors, writes, reads = [], [], []
            page.on('pageerror', lambda error: errors.append(str(error)))

            async def route(request):
                url = request.request.url
                if url.endswith('/access.js'):
                    await request.fulfill(content_type='application/javascript',
                        body="window.accessState={premium:true,registered:true,username:'tester'};document.body.dataset.access='premium';window.canUsePremium=()=>true;")
                elif url.split('?', 1)[0].endswith('/history.js'):
                    await request.fulfill(content_type='application/javascript', body=response('/history.js')[2])
                elif url.startswith('https://example.test/app'):
                    await request.fulfill(content_type='text/html', body=response('/app')[2])
                elif '/api/history' in url:
                    q = parse_qs(urlparse(url).query)
                    view = q.get('view', ['days'])[0]
                    reads.append(view)
                    if view == 'archive_days':
                        data = {'view': view, 'days': [{'day': '2026-11-08', 'partial': True,
                                                       'has_records': True}],
                                'total': 1, 'page': 0}
                    elif view == 'archive_day':
                        data = {'view': view, 'day': '2026-11-08',
                                'summary': {'total': 5, 'lt2': 2, 'lt2_tracking': 1,
                                            'new_threshold_tracking': 1},
                                'integrity': {'status': 'unverified', 'poll_coverage': None},
                                'truncated': False}
                    elif view == 'archive_high_days':
                        data = {'view': view, 'days': [{'day': '2026-11-08', 'count': 1}],
                                'total': 1, 'page': 0}
                    elif view == 'archive_cycles':
                        data = {'view': view, 'items': [], 'total': 0, 'page': 0}
                    elif view == 'archive_max_steps':
                        data = {'view': view, 'days': [{'day': '2026-11-08', 'steps': 3}],
                                'total': 1, 'page': 0}
                    else:
                        data = {'view': view, 'days': ['2026-11-08'] if view != 'max_steps' else [],
                                'page': 0, 'total': 1, 'items': []}
                    await request.fulfill(content_type='application/json', body=json.dumps(data))
                elif '/api/controls' in url:
                    writes.append(request.request.post_data_json)
                    await request.fulfill(content_type='application/json',
                                          body=json.dumps({'message': 'Сохранено.'}))
                elif '/api/overview' in url:
                    await request.fulfill(content_type='application/json',
                        body=json.dumps({'coefficients': [], 'last_round_at': None,
                                         'demo': {'currency': 'USD', 'balance_cents': 1000000,
                                                  'enabled': False, 'active': False,
                                                  'step': None, 'base_stake_cents': 1000, 'trigger': 4}}))
                else:
                    await request.fulfill(content_type='application/javascript',
                        body="window.Telegram={WebApp:{initData:'fixture',ready(){},expand(){},setHeaderColor(){},setBackgroundColor(){},close(){},showConfirm(t,cb){cb(true)},BackButton:{onClick(){},hide(){},show(){}},HapticFeedback:{selectionChanged(){}}}}")
            await page.route('**/*', route)
            await page.goto('https://example.test/app')
            await page.locator('nav [data-tab=history]').click()
            await page.locator('#history-content .history-date-search').wait_for(state='attached')
            assert not await page.locator('#history-content .history-date-search').evaluate('(n) => n.open')
            await page.locator('#history-content .history-date-search summary').click()
            await page.locator('#history-date').wait_for(state='visible')
            assert await page.locator('#history-date').input_value() != ''
            assert not await page.locator('#history-pager').is_visible()
            await page.locator('#history-management summary').click()
            async with page.expect_response(lambda res: '/api/controls' in res.url and
                    res.request.post_data_json.get('value') == 'days'):
                await page.locator('#history-reset-scope').click()
            await page.wait_for_function("document.querySelector('#history-status').textContent === '' && !document.querySelector('#history-content').dataset.loading")
            assert writes[0] == {'action': 'history_reset', 'value': 'days', 'confirm': True}
            await page.locator('[data-history-view=high_days]').click()
            async with page.expect_response(lambda res: '/api/controls' in res.url and
                    res.request.post_data_json.get('value') == 'high_days'):
                await page.locator('#history-reset-scope').click()
            assert writes[1]['value'] == 'high_days'
            await page.locator('[data-history-view=days]').click()
            await page.locator('#history-archive').click()
            await page.locator('#history-archive-exit').get_by_text('Архив').wait_for()
            assert await page.locator('#history-reset-panel').is_hidden()
            assert await page.locator('#history-archive-launch').is_hidden()
            assert await page.locator('#history-live-tabs').is_hidden()
            assert await page.locator('#history-archive-tabs').is_visible()
            assert await page.locator('[data-archive-view=archive_days]').is_visible()
            await page.locator('[data-archive-view=archive_high_days]').click()
            assert 'archive_high_days' in reads
            await page.locator('[data-archive-view=archive_cycles]').click()
            assert 'archive_cycles' in reads
            await page.locator('[data-archive-view=archive_max_steps]').click()
            assert 'archive_max_steps' in reads
            await page.locator('#history-exit-archive').click()
            assert await page.locator('#history-reset-panel').is_visible()
            async with page.expect_response(lambda res: '/api/controls' in res.url and
                    res.request.post_data_json.get('value') == 'all'):
                await page.locator('#history-reset-all').click()
            assert writes[-1] == {'action': 'history_reset', 'value': 'all', 'confirm': True}
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert not errors, errors
            await browser.close()
    asyncio.run(run())
