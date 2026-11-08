"""Regression: DEMO history shows exactly one strategy and its own metrics."""
import asyncio
import json
from urllib.parse import parse_qs, urlparse

from mini_app import response


def test_mobile_demo_history_two_exclusive_accordions_and_own_stats():
    from playwright.async_api import async_playwright, expect

    async def run():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={'width': 390, 'height': 844})
            errors = []
            requests = []
            page.on('pageerror', lambda exc: errors.append(str(exc)))

            def stats(total, net):
                return {'currency': 'KRW', 'total': total, 'maximum': 100,
                        'recovered': total, 'lost': 0, 'incomplete': 0,
                        'stopped': 0, 'history_net_cents': net,
                        'average_steps': 1}

            async def route(req):
                url = req.request.url
                path = urlparse(url).path
                if path == '/access.js':
                    await req.fulfill(content_type='application/javascript',
                        body="window.accessState={premium:true,registered:true,username:'tester'};document.body.dataset.access='premium';window.canUsePremium=()=>true;")
                elif path in ('/history.js', '/controls.js'):
                    await req.fulfill(content_type='application/javascript', body=response(path)[2])
                elif path == '/app':
                    await req.fulfill(content_type='text/html', body=response('/app')[2])
                elif path == '/api/history':
                    view = parse_qs(urlparse(url).query).get('view', ['days'])[0]
                    requests.append(view)
                    if view in ('cycles', 'high_cycles'):
                        high = view == 'high_cycles'
                        data = {'view': view, 'total': 1, 'page': 0,
                                'stats': stats(1, -15000 if high else 30000),
                                'items': [{'index': 0, 'currency': 'KRW',
                                           'strategy': 'high_gap' if high else 'classic',
                                           'outcome': 'recovered',
                                           'ended_at': '2026-11-08T02:00:00Z',
                                           'steps': 1, 'net_cents': -15000 if high else 30000}]}
                    elif view in ('cycle', 'high_cycle'):
                        high = view == 'high_cycle'
                        data = {'view': view, 'item': {'index': 0,
                            'currency': 'KRW', 'strategy': 'high_gap' if high else 'classic',
                            'outcome': 'recovered', 'steps': 1,
                            'ended_at': '2026-11-08T02:00:00Z',
                            'started_at': '2026-11-08T01:58:00Z',
                            'net_cents': -15000 if high else 30000,
                            'total_stake_cents': 15000, 'payout_cents': 0,
                            'balance_before_cents': 1000000, 'balance_after_cents': 985000,
                            'rounds': [{'coefficient': '2', 'stake_cents': 15000,
                                        'payout_cents': 0}]}}
                    else:
                        data = {'view': view, 'page': 0, 'total': 0,
                                'days': [], 'items': []}
                    await req.fulfill(content_type='application/json', body=json.dumps(data))
                elif path == '/api/overview':
                    await req.fulfill(content_type='application/json', body=json.dumps({
                        'coefficients': [], 'last_round_at': None, 'summary': {},
                        'demo': {'currency': 'KRW', 'balance_cents': 1000000,
                                 'balance_verified': True,
                                 'high_gap': {'enabled': False}},
                        'status': {'last': '1.50x', 'fresh': True, 'age_seconds': 1,
                                   'uptime_seconds': 50, 'streak_count': 0,
                                   'threshold': 4}}))
                elif path == '/api/controls':
                    await req.fulfill(content_type='application/json', body=json.dumps({
                        'demo': {'currency': 'KRW', 'balance_cents': 1000000,
                                 'base_stake_cents': 15000, 'trigger': 3, 'low_streak': 0},
                        'high_gap': {'enabled': False},
                        'settings': {'notifications': True, 'sound': True,
                                     'threshold': 3, 'history_size': 20,
                                     'delete_delay': 10}}))
                else:
                    await req.fulfill(content_type='application/javascript',
                        body="window.Telegram={WebApp:{initData:'fixture',ready(){},expand(){},setHeaderColor(){},setBackgroundColor(){},close(){},showConfirm(t,cb){cb(true)},BackButton:{onClick(){},hide(){},show(){}},HapticFeedback:{selectionChanged(){}}}}")

            await page.route('**/*', route)
            await page.goto('https://example.test/app')
            await page.locator('nav [data-tab=history]').click()
            await page.locator('[data-history-view=cycles]').click()

            classic = page.locator('#demo-history-strategy-classic')
            high = page.locator('#demo-history-strategy-high')
            await classic.wait_for()
            assert await classic.get_attribute('aria-expanded') == 'false'
            assert await high.get_attribute('aria-expanded') == 'false'
            assert await page.locator('#history-content .history-item').count() == 0

            await classic.click()
            await page.locator('#history-content .history-item').wait_for()
            await expect(classic).to_have_attribute('aria-expanded', 'true')
            await expect(high).to_have_attribute('aria-expanded', 'false')
            assert '30' in await page.locator('#history-content').inner_text()
            assert 'cycles' in requests

            await high.click()
            await page.locator('#history-content .history-item').wait_for()
            await expect(classic).to_have_attribute('aria-expanded', 'false')
            await expect(high).to_have_attribute('aria-expanded', 'true')
            assert 'high_cycles' in requests
            assert await page.locator('#history-content .history-item').count() == 1
            assert 'Статистика' in await page.locator('#history-content').inner_text()

            await high.click()
            assert await high.get_attribute('aria-expanded') == 'false'
            assert await page.locator('#history-content .history-item').count() == 0
            assert await page.locator('#history-pager').is_hidden()

            await high.click()
            await page.locator('#history-content .history-item').click()
            assert 'high_cycle' in requests
            await expect(high).to_have_attribute('aria-expanded', 'true')
            await page.locator('#history-back').click()
            assert await high.get_attribute('aria-expanded') == 'true'
            assert await page.locator('#history-content .history-item').count() == 1
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert not errors, errors
            await browser.close()
    asyncio.run(run())


def test_strategies_get_separate_stats_from_their_own_saved_journals():
    from fakeredis.aioredis import FakeRedis
    from luckyjet_reliable_gate import ReliableTelegramPasswordGate
    from luckyjet_signal.demo_ledger import DemoLedger
    from mini_app_history import history
    from test_archive_four_tabs import signed

    async def run():
        gate = ReliableTelegramPasswordGate(None)
        gate.authorized.add(42)
        gate.redis = FakeRedis(decode_responses=True)
        ledger = DemoLedger(currency='KRW', enabled=False,
                            balance_cents=1200000, initial_balance_cents=1000000)
        classic = {'currency': 'KRW', 'outcome': 'recovered', 'net_cents': 30000,
                   'rounds': [{'coefficient': '2', 'stake_cents': 15000}]}
        high = {'currency': 'KRW', 'strategy': 'high_gap', 'outcome': 'insufficient_balance', 'net_cents': -45000,
                'rounds': [{'coefficient': '2', 'stake_cents': 45000}]}
        ledger.history = [classic, classic]
        ledger.high_history = [high]
        gate.demo_ledgers[42] = ledger

        code, _, raw = await history(signed(42), gate, 'fixture', 'view=cycles')
        assert code == '200 OK'
        first = json.loads(raw)
        assert first['total'] == first['stats']['total'] == 2
        assert first['stats']['history_net_cents'] == 60000
        assert all(row['strategy'] == 'classic' for row in first['items'])

        code, _, raw = await history(signed(42), gate, 'fixture', 'view=high_cycles')
        assert code == '200 OK'
        second = json.loads(raw)
        assert second['total'] == second['stats']['total'] == 1
        assert second['stats']['history_net_cents'] == -45000
        assert all(row['strategy'] == 'high_gap' for row in second['items'])
        assert ledger.balance_cents == 1200000  # shared balance untouched
        await gate.redis.aclose()
        gate.redis = None
        await gate.close()
    asyncio.run(run())
