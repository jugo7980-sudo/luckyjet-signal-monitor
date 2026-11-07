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
                elif url.endswith('/history.js'):
                    await request.fulfill(content_type='application/javascript', body=response('/history.js')[2])
                elif url.startswith('https://example.test/app'):
                    await request.fulfill(content_type='text/html', body=response('/app')[2])
                elif '/api/history' in url:
                    q = parse_qs(urlparse(url).query)
                    view = q.get('view', ['days'])[0]
                    reads.append(view)
                    if view == 'archive':
                        more = 'cursor' not in q
                        data = {'view': 'archive', 'day': '2026-11-08',
                                'items': [{'type': 'round', 'round_id': 'round-1', 'coefficient': '2.5',
                                           'observed_at': '1794000000000', 'source_time': ''}],
                                'next_cursor': '1794000000000-0' if more else None}
                        if more:
                            data['integrity'] = {
                                'status': 'unverified', 'archived_rounds': 5,
                                'gaps': 0, 'reasons': ['тестовый пропуск'],
                                'poll_coverage': {
                                    'status': 'poll_gaps', 'expected_minutes': 5,
                                    'observed_minutes': 3, 'primary_minutes': 2,
                                    'backup_minutes': 2, 'missing_minutes': 2,
                                    'longest_missing_minutes': 2,
                                    'missing_intervals': [{
                                        'start': '00:02', 'end': '00:04', 'minutes': 2,
                                    }],
                                },
                            }
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
            await page.locator('#history-content').get_by_text('История Кэф').wait_for()
            await page.locator('#history-reset-scope').click()
            await page.locator('#history-content').get_by_text('История Кэф').wait_for()
            assert writes[0] == {'action': 'history_reset', 'value': 'days', 'confirm': True}
            await page.locator('[data-history-view=high_days]').click()
            await page.locator('#history-reset-scope').click()
            assert writes[1]['value'] == 'high_days'
            await page.locator('#history-archive').click()
            await page.get_by_text('Фактический архив', exact=True).wait_for()
            await page.get_by_text('Минуты успешных опросов').wait_for()
            await page.get_by_text('00:02–00:04', exact=False).wait_for()
            assert await page.locator('#history-reset-scope').is_hidden()
            await page.get_by_text('Показать более ранние записи').click()
            assert reads.count('archive') >= 2
            await page.locator('#history-reset-all').click()
            assert writes[-1] == {'action': 'history_reset', 'value': 'all', 'confirm': True}
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert not errors, errors
            await browser.close()
    asyncio.run(run())
