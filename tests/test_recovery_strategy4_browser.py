"""Full Strategy 4 mobile UX: controls, settings, history, compact folds."""
import asyncio
import json
from urllib.parse import parse_qs, urlparse

from mini_app import response


def test_strategy4_mobile_controls_notifications_history_and_auto_close():
    from playwright.async_api import async_playwright, expect

    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            for width in (390, 320):
                page = await browser.new_page(viewport={'width': width, 'height': 844})
                errors, actions, views = [], [], []
                page.on('pageerror', lambda e: errors.append(str(e)))
                target = {'enabled': False, 'target': '1.32', 'trigger': 3,
                          'stake_cents': 15000, 'phase': 'waiting', 'streak': 2,
                          'steps': 0, 'total_stake_cents': 0, 'next_stake_cents': 15000}
                prefs = {'enabled': True, 'sound': True, 'delete_delay': 10,
                         'events': {'armed':True,'betting':True,'loss':False,'goal':True,'stopped':True}}
                demo = {'currency': 'KRW', 'enabled': False, 'active': False,
                        'balance_cents': 10000000, 'balance_verified': True,
                        'base_stake_cents': 15000, 'trigger': 3, 'low_streak': 0,
                        'high_gap': {'enabled': False},
                        'recovery_streak': target, 'reset_revision': 'test'}
                def controls_data():
                    return {'demo': demo, 'recovery_streak': target, 'recovery_streak_notifications': prefs,
                            'high_gap': {'enabled': False}, 'settings': {
                                'notifications': True, 'sound': True, 'threshold': 3,
                                'history_size': 20, 'delete_delay': 10}}
                async def route(req):
                    path = urlparse(req.request.url).path
                    if path == '/app':
                        await req.fulfill(content_type='text/html', body=response('/app')[2])
                    elif path == '/access.js':
                        await req.fulfill(content_type='application/javascript',
                            body="window.accessState={premium:true,registered:true,username:'tester'};"
                                 "document.body.dataset.access='premium';window.canUsePremium=()=>true;")
                    elif path in ('/history.js', '/controls.js', '/summary.js'):
                        await req.fulfill(content_type='application/javascript', body=response(path)[2])
                    elif path == '/api/overview':
                        await req.fulfill(content_type='application/json',
                            body=json.dumps({'coefficients': ['1.10x', '1.20x'],
                              'demo': demo, 'last_round_at': None,
                              'status': {'fresh': True, 'age_seconds': 1, 'streak_count': 0,
                                         'threshold': 3, 'uptime_seconds': 120}}))
                    elif path == '/api/controls':
                        if req.request.method == 'POST':
                            payload = req.request.post_data_json
                            actions.append(payload)
                            if payload['action'] == 'recovery_streak_config':
                                cfg = payload['value']
                                target.update(target=cfg['target'],trigger=cfg['trigger'],
                                              stake_cents=int(cfg['stake'])*100)
                            elif payload['action'] == 'recovery_streak_toggle':
                                target['enabled'] = not target['enabled']
                            elif payload['action'] == 'recovery_notice_event':
                                name = payload['value']
                                prefs['events'][name] = not prefs['events'][name]
                            elif payload['action'] == 'recovery_notice_toggle':
                                prefs['enabled'] = not prefs['enabled']
                        await req.fulfill(content_type='application/json', body=json.dumps(
                            {'message': 'Сохранено.', **controls_data()}))
                    elif path == '/api/history':
                        view = parse_qs(urlparse(req.request.url).query).get('view', ['days'])[0]
                        views.append(view)
                        data = {'view': view, 'page': 0, 'total': 0, 'days': [], 'items': []}
                        if view == 'recovery_cycles':
                            data.update({'total': 1, 'stats': {
                                'currency': 'KRW', 'total': 1, 'recovered': 1,
                                'lost': 0, 'incomplete': 0, 'stopped': 0,
                                'history_net_cents': 15000, 'average_steps': 2},
                                'items': [{'index': 0, 'currency': 'KRW',
                                    'strategy': 'recovery_streak',
                                    'outcome': 'recovered', 'net_cents': 15000,
                                    'steps': 2, 'ended_at': '2026-11-08T03:00:00Z'}]})
                        elif view == 'recovery_cycle':
                            data['item'] = {'index': 0, 'currency': 'KRW',
                                'strategy': 'recovery_streak', 'outcome': 'recovered',
                                'trigger_streak': 3, 'cashout_multiplier': '1.32',
                                'net_cents': 15000, 'total_stake_cents': 67700,
                                'payout_cents': 82700, 'balance_before_cents': 10000000,
                                'balance_after_cents': 10015000,
                                'started_at': '2026-11-08T02:58:00Z',
                                'ended_at': '2026-11-08T03:00:00Z',
                                'rounds': [{'stake_cents': 15000, 'coefficient': '1.10',
                                            'payout_cents': 0},
                                           {'stake_cents': 52700, 'coefficient': '1.32',
                                            'payout_cents': 82700}]}
                        await req.fulfill(content_type='application/json', body=json.dumps(data))
                    else:
                        await req.fulfill(content_type='application/javascript',
                            body="window.Telegram={WebApp:{initData:'fixture',ready(){},"
                                 "expand(){},setHeaderColor(){},setBackgroundColor(){},"
                                 "close(){},BackButton:{onClick(){},hide(){},show(){}},"
                                 "HapticFeedback:{selectionChanged(){}}}}")
                await page.route('**/*', route)
                await page.goto('https://example.test/app')
                await page.locator('nav [data-tab=demo]').click()
                await page.locator('#demo-recovery-accordion summary').first.click()
                await expect(page.locator('#recovery-streak-panel')).to_be_visible()
                await expect(page.locator('#recovery-streak-x')).to_have_value('1.32')
                await page.locator('#recovery-streak-x').fill('1.43')
                await page.locator('#recovery-streak-trigger').select_option('4')
                await page.locator('#recovery-streak-stake').fill('150')
                await page.locator('#recovery-streak-save').click()
                await page.wait_for_function('document.getElementById("recovery-streak-x").value === "1.43"')
                await expect(page.locator('#demo-recovery-preview')).to_contain_text('Выключена')
                await page.locator('#recovery-streak-toggle').click()
                await expect(page.locator('#recovery-streak-toggle')).to_have_text('Выключить стратегию №4')
                await page.locator('nav [data-tab=account]').click()
                assert not await page.locator('#demo-recovery-accordion').evaluate('(n) => n.open')
                await page.locator('#open-settings').click()
                await page.locator('#recovery-streak-notice-options summary').click()
                await expect(page.locator('#settings-recovery-enabled')).to_be_visible()
                await page.locator('#settings-recovery-loss').click()
                await expect(page.locator('#settings-recovery-loss')).to_have_text('ВКЛ')
                await page.locator('nav [data-tab=history]').click()
                assert not await page.locator('#recovery-streak-notice-options').evaluate('(n) => n.open')
                await page.locator('[data-history-view=cycles]').click()
                await page.locator('#demo-history-strategy-recovery').click()
                await page.locator('#history-demo-cycles > summary').click()
                await expect(page.locator('#history-demo-cycles .history-item')).to_be_visible()
                await page.locator('#history-demo-cycles .history-item').click()
                await expect(page.locator('#history-cycle-steps')).to_be_visible()
                await page.locator('#history-cycle-steps > summary').click()
                assert await page.locator('#history-cycle-steps .history-metric').count() == 2
                assert 'recovery_cycles' in views and 'recovery_cycle' in views
                await page.locator('nav [data-tab=home]').click()
                await page.locator('nav [data-tab=history]').click()
                await expect(page.locator('#demo-history-strategy-recovery')).to_have_attribute(
                    'aria-expanded', 'false')
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert not errors, errors
                await page.close()
            await browser.close()
    asyncio.run(run())
