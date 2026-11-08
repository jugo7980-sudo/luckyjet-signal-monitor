"""Mini App history parity, authentication, pagination and mobile navigation."""
import asyncio
import hashlib
import hmac
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from mini_app import response
from mini_app_history import history, _high_coefficient_band
from luckyjet_reliable_gate import ReliableTelegramPasswordGate
from luckyjet_signal.demo_ledger import DemoLedger

DAY = '2026-11-06'
ITEM = 'abcdef0123456789'


def signed(uid=42):
    fields = {
        'auth_date': str(int(time.time())),
        'user': json.dumps({'id': uid}),
    }
    check = '\n'.join(f'{key}={value}' for key, value in sorted(fields.items()))
    secret = hmac.new(b'WebAppData', b'fixture', hashlib.sha256).digest()
    fields['hash'] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return 'tma ' + urlencode(fields)


class FakeRedis:
    async def xrange(self, key, min, max, count):
        return [
            ('1-0', {'type': 'round', 'coefficient': '1.50'}),
            ('2-0', {'type': 'round', 'coefficient': '1.99'}),
            ('3-0', {'type': 'round', 'coefficient': '2.00'}),
            ('4-0', {'type': 'round', 'coefficient': '5.99'}),
            ('5-0', {'type': 'round', 'coefficient': '150.99'}),
            ('6-0', {'type': 'round', 'coefficient': '151.00'}),
        ]

    async def zcard(self, key):
        assert key == 'luckyjet:coef:high:days'
        return 1

    async def zrevrange(self, key, first, last):
        assert key == 'luckyjet:coef:high:days'
        return [DAY.encode()] if first == 0 else []

    async def hgetall(self, key):
        assert key == 'luckyjet:coef:high:state'
        return {b'last_high': json.dumps({'value': '150', 'stamp': 1793980800}).encode(),
                b'since': b'8', b'gap': b'0'}

    async def llen(self, key):
        assert key == 'luckyjet:coef:high:' + DAY
        return 1

    async def lrange(self, key, first, last):
        assert key == 'luckyjet:coef:high:' + DAY
        return [ITEM.encode()] if first == 0 else []

    async def hmget(self, key, ids):
        assert key == 'luckyjet:coef:high:entries'
        return [json.dumps({'id': ITEM, 'value': '150', 'day': DAY,
                            'rounds': 29, 'stamp': 1793980800,
                            'previous': None, 'uncertain': False}).encode()]

    async def hget(self, key, item):
        assert key == 'luckyjet:coef:high:entries'
        return (await self.hmget(key, [item]))[0] if item == ITEM else None


class FakeStats:
    redis = FakeRedis()

    async def days(self, page):
        return ([DAY], 1) if page == 0 else ([], 1)

    async def summary(self, day):
        assert day == DAY
        return {'total': 100, 'lt2_tracking': 1, 'lt2': 42, '10': 7}


def test_history_auth_pages_and_user_isolation():
    async def run():
        gate = ReliableTelegramPasswordGate(None)
        gate.authorized.update({42, 43})
        gate.coefficient_stats = FakeStats()
        gate.demo_ledgers[42] = DemoLedger(
            balance_cents=1000100,
            history=[{
                'started_at': '2026-11-06T10:00:00+00:00',
                'ended_at': '2026-11-06T10:01:00+00:00',
                'rounds': [{'coefficient': '3.1', 'stake_cents': 1000, 'payout_cents': 2000}],
                'outcome': 'recovered', 'net_cents': 100,
                'total_stake_cents': 1000, 'payout_cents': 2000,
                'balance_before_cents': 1000000, 'balance_after_cents': 1000100,
            }],
        )
        async def fetch(query, uid=42, auth=None):
            status, _, body = await history(signed(uid) if auth is None else auth, gate, 'fixture', query)
            return status, json.loads(body)

        for query, key, expected in [
            ('view=days', 'days', [DAY]),
            ('view=day&day=' + DAY, 'summary', {'total': 100, 'lt2_tracking': 1, 'lt2': 42, '10': 7}),
            ('view=high_days', 'days', [DAY]),
            ('view=high_day&day=' + DAY, 'total', 1),
            ('view=high_item&id=' + ITEM, 'view', 'high_item'),
            ('view=cycles', 'total', 1),
            ('view=cycle&index=0', 'view', 'cycle'),
            ('view=max_steps', 'view', 'max_steps'),
        ]:
            status, data = await fetch(query)
            assert status == '200 OK', (query, data)
            assert data[key] == expected, query
        _, daily = await fetch('view=day&day=' + DAY)
        assert daily['distribution']['total'] == 6
        assert daily['distribution']['counts']['1.5'] == 2
        assert daily['distribution']['counts']['2'] == 1
        assert daily['distribution']['counts']['5'] == 1
        assert daily['distribution']['counts']['150'] == 1
        assert daily['distribution']['counts']['above_150'] == 1
        assert daily['distribution']['high_ranges']['151-199'] == 1
        assert sum(daily['distribution']['high_ranges'].values()) == 1
        assert daily['distribution']['high_values']['151-199'] == [['151.00', 1]]
        _, stats = await fetch('view=demo_stats')
        assert stats['stats']['recovered'] == 1
        assert stats['stats']['since_reset_cents'] == 100
        _, other = await fetch('view=cycles', uid=43)
        assert other['total'] == 0 and other['items'] == []
        assert (await fetch('view=cycle&index=0', uid=43))[0] == '404 Not Found'

        for query in [
            'view=../../secret', 'view=days&page=-1', 'view=days&page=999999999',
            'view=day&day=2026-99-99', 'view=day&day=2099-01-01',
            'view=high_item&id=../../secrets', 'view=days&view=cycles',
            'view=cycle&index=101',
        ]:
            assert (await fetch(query))[0] == '400 Bad Request', query
        assert (await fetch('view=days', auth=''))[0] == '401 Unauthorized'
        gate.blocked.add(42)
        assert (await fetch('view=cycles'))[0] == '403 Forbidden'
        assert (await fetch('view=days', uid=99))[0] == '403 Forbidden'
        await gate.close()
    asyncio.run(run())


def test_high_coefficient_bands_do_not_overlap():
    assert _high_coefficient_band(150.99) is None
    for low, high, name in [
        (151, 199.99, '151-199'),
        (200, 299.99, '200-299'),
        (300, 499.99, '300-499'),
        (500, 799.99, '500-799'),
        (800, 999.99, '800-999'),
        (1000, 1499.99, '1000-1499'),
        (1500, 1999.99, '1500-1999'),
        (2000, 2999.99, '2000-2999'),
        (3000, 4999.99, '3000-4999'),
        (5000, 999999, '5000+'),
    ]:
        assert _high_coefficient_band(low) == name
        assert _high_coefficient_band(high) == name
    assert _high_coefficient_band(199.999) == '151-199'
    assert _high_coefficient_band(299.999) == '200-299'


def test_history_static_asset_and_mobile_flow():
    from playwright.async_api import async_playwright
    from urllib.parse import parse_qs, urlparse

    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={'width': 390, 'height': 844})
            errors = []
            calls = []
            page.on('pageerror', lambda error: errors.append(str(error)))

            async def route(request):
                if request.request.url.endswith('/access.js'):
                    await request.fulfill(content_type='application/javascript',
                        body="window.accessState={premium:true,registered:true,username:'tester'};document.body.dataset.access='premium';window.canUsePremium=()=>true;window.refreshAccessOverview?.();")
                    return
                url = request.request.url
                if url.split('?', 1)[0].endswith(('/history.js', '/summary.js')):
                    await request.fulfill(content_type='application/javascript',
                                          body=response('/' + url.split('/')[-1])[2])
                elif url.startswith('https://example.test/app'):
                    await request.fulfill(content_type='text/html', body=response('/app')[2])
                elif '/api/history' in url:
                    q = parse_qs(urlparse(url).query)
                    view = q.get('view', ['days'])[0]
                    calls.append(view)
                    data = {'view': view, 'page': int(q.get('page', [0])[0]), 'total': 1}
                    if view == 'days':
                        data['days'] = [DAY]
                    elif view == 'day':
                        data.update(day=DAY, summary={'total': 100, 'lt2_tracking': 1, 'lt2': 45},
                                    distribution={'complete_scan': True, 'total': 4,
                                                  'counts': {'1.5': 1, '2': 2, 'above_150': 1},
                                                  'high_ranges': {'151-199': 1},
                                                  'high_values': {'151-199': [['151.00', 1]]}})
                    elif view == 'high_days':
                        data.update(days=[DAY], last_high={'value': '150', 'stamp': 1793980800},
                                    since_high=7, gap=False)
                    elif view == 'high_day':
                        data.update(day=DAY, items=[{'id': ITEM, 'value': '150', 'stamp': 1793980800,
                                                     'rounds': 29}])
                    elif view == 'high_item':
                        data['item'] = {'value': '150', 'stamp': 1793980800, 'rounds': 29}
                    elif view == 'cycles':
                        data['stats'] = {'currency': 'USD', 'total': 1, 'maximum': 100,
                                         'recovered': 1, 'lost': 0, 'incomplete': 0,
                                         'stopped': 0, 'history_net_cents': 500,
                                         'average_steps': 3}
                        data['items'] = [{'index': 0, 'steps': 3, 'ended_at': '2026-11-06T10:00:00Z',
                                          'outcome': 'recovered', 'net_cents': 500}]
                    elif view == 'cycle':
                        data['item'] = {'index': 0, 'steps': 3, 'rounds': [],
                                        'net_cents': 500, 'outcome': 'recovered'}
                    elif view == 'demo_stats':
                        data['stats'] = {'total': 1, 'maximum': 100, 'recovered': 1, 'lost': 0,
                                          'incomplete': 0, 'stopped': 0, 'since_reset_cents': 500,
                                          'history_net_cents': 500, 'average_steps': 3}
                    elif view == 'max_steps':
                        data['days'] = [{'day': DAY, 'steps': 5}]
                    await request.fulfill(content_type='application/json', body=json.dumps(data))
                elif '/api/overview' in url:
                    await request.fulfill(content_type='application/json', body=json.dumps({
                        'coefficients': [], 'last_round_at': None,
                        'demo': {'balance_cents': 1000000, 'enabled': False, 'active': False,
                                 'step': None, 'base_stake_cents': 1000, 'trigger': 4},
                    }))
                else:
                    await request.fulfill(content_type='application/javascript',
                        body="window.Telegram={WebApp:{initData:'fixture',ready(){},expand(){},setHeaderColor(){},setBackgroundColor(){},close(){},BackButton:{onClick(){},hide(){},show(){}},HapticFeedback:{selectionChanged(){}}}}")

            await page.route('**/*', route)
            await page.goto('https://example.test/app')
            await page.locator('nav [data-tab=history]').click()
            await page.locator('#history-days-list > summary').click()
            await page.get_by_text('06.11.2026').first.wait_for()
            assert await page.locator('#history-date').input_value() == '2026-11-06'
            assert not await page.locator('#history-content .history-date-search').evaluate('(n) => n.open')
            await page.locator('#history-content button.history-item').first.click()
            await page.get_by_text('Всего раундов').wait_for()
            warning = page.locator('#history-content .history-note-critical').first
            assert 'разное число' in await warning.inner_text()
            assert await warning.locator('.history-note-text').evaluate(
                "(n) => getComputedStyle(n).whiteSpace === 'normal'")
            await page.locator('#history-coeff-distribution > summary').click()
            await page.get_by_text('x151–x199').click()
            await page.get_by_text('x151.00').wait_for()
            assert await page.get_by_text('x151.00').count() == 1
            assert await page.locator('.history-distribution-group').count() >= 18
            await page.evaluate("""() => {
                window.savedHistoryGroup = [...document.querySelectorAll(
                    '#history-content details.history-distribution-group'
                )].find(n => n.querySelector('summary').textContent.includes('x151–x199'));
            }""")
            async with page.expect_response(lambda r: '/api/history?' in r.url and 'view=day' in r.url):
                await page.locator('#history-refresh').click()
            await page.wait_for_function("document.querySelector('#history-status').textContent === ''")
            assert await page.evaluate("""() => window.savedHistoryGroup ===
                [...document.querySelectorAll('#history-content details.history-distribution-group')]
                    .find(n => n.querySelector('summary').textContent.includes('x151–x199'))
                && window.savedHistoryGroup.open""")
            await page.locator('#history-content .history-metric .summary-question').first.click()
            assert "прежней сводке" in await page.locator('#summary-metric-text').inner_text()
            await page.locator('#summary-metric-close').click()
            await page.locator('#history-back').click()

            await page.locator('[data-history-view=high_days]').click()
            await page.locator('#history-high-days-list > summary').click()
            await page.locator('#history-content button.history-item').first.click()
            await page.locator('#history-high-event-list > summary').click()
            await page.locator('#history-content button.history-item').first.click()
            await page.get_by_text('29 раундов').first.wait_for()
            await page.locator('#history-back').click()

            await page.locator('[data-history-view=cycles]').click()
            await page.locator('#demo-history-strategy-classic').click()
            await page.locator('#history-demo-stats > summary').click()
            await page.get_by_text('Среднее шагов').wait_for()
            await page.locator('#history-demo-cycles > summary').click()
            await page.locator('#history-content button.history-item').first.click()
            await page.locator('#history-cycle-outcome > summary').wait_for()

            await page.locator('[data-history-view=max_steps]').click()
            await page.locator('#history-max-step-days > summary').click()
            await page.get_by_text('5 шаг.').wait_for()
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert not errors, errors
            assert {'days', 'day', 'high_days', 'high_day', 'high_item', 'cycles',
                    'cycle', 'max_steps'} <= set(calls)
            await browser.close()
    asyncio.run(run())


def test_latest_step_first_compact_all_steps_and_archive_all_four_strategies():
    """The four DEMO journals and immutable archive show newest step without scrolling."""
    from playwright.async_api import async_playwright, expect
    from urllib.parse import parse_qs, urlparse

    rounds = [{'coefficient': '1.20', 'stake_cents': 15000,
               'payout_cents': 0, 'round_id': 'r' + str(i)}
              for i in range(1, 99)]
    rounds.append({'coefficient': '2.00', 'stake_cents': 15000,
                   'payout_cents': 30000, 'round_id': 'r99'})
    strategies = {'cycles': 'classic', 'high_cycles': 'high_gap',
                  'target_cycles': 'target_streak', 'recovery_cycles': 'recovery_streak'}
    reverse_strategy = {'cycle': 'classic', 'high_cycle': 'high_gap',
                        'target_cycle': 'target_streak', 'recovery_cycle': 'recovery_streak'}
    detail = {'index': 0, 'id': 'cycle-99', 'currency': 'KRW',
              'started_at': '2026-11-09T01:00:00Z',
              'ended_at': '2026-11-09T02:00:00Z',
              'outcome': 'recovered', 'net_cents': -1455000,
              'balance_before_cents': 20000000, 'balance_after_cents': 18545000,
              'payout_cents': 30000, 'total_stake_cents': 1485000,
              'base_stake_cents': 15000, 'rounds': rounds, 'steps': 99}

    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={'width': 390, 'height': 844})
            errors, calls = [], []
            page.on('pageerror', lambda e: errors.append(str(e)))

            async def route(request):
                url = request.request.url
                if url.endswith('/access.js'):
                    await request.fulfill(content_type='application/javascript',
                        body="window.accessState={premium:true,registered:true,username:'tester'};"
                             "document.body.dataset.access='premium';"
                             "window.canUsePremium=()=>true;window.refreshAccessOverview?.();")
                elif urlparse(url).path in ('/history.js', '/summary.js'):
                    await request.fulfill(content_type='application/javascript',
                        body=response(urlparse(url).path)[2])
                elif urlparse(url).path == '/app':
                    await request.fulfill(content_type='text/html', body=response('/app')[2])
                elif urlparse(url).path == '/api/history':
                    view = parse_qs(urlparse(url).query).get('view', ['days'])[0]
                    calls.append(view)
                    data = {'view': view, 'total': 1, 'page': 0}
                    if view == 'days':
                        data['days'] = [DAY]
                    elif view in strategies:
                        data['stats'] = {'currency': 'KRW', 'total': 1, 'maximum': 100,
                            'recovered': 1, 'lost': 0, 'incomplete': 0,
                            'stopped': 0, 'history_net_cents': -1455000, 'average_steps': 99}
                        data['items'] = [{**detail, 'strategy': strategies[view], 'rounds': []}]
                    elif view in reverse_strategy:
                        data['item'] = {**detail, 'strategy': reverse_strategy[view]}
                    elif view == 'archive_cycles':
                        data['items'] = [{**detail, 'strategy': 'recovery_streak', 'rounds': []}]
                    elif view == 'archive_cycle':
                        data['item'] = {**detail, 'strategy': 'recovery_streak'}
                    elif view == 'archive_days':
                        data['days'] = [DAY]
                    else:
                        data['items'] = []
                    await request.fulfill(content_type='application/json', body=json.dumps(data))
                elif urlparse(url).path == '/api/overview':
                    await request.fulfill(content_type='application/json',
                        body='{"coefficients":[],"last_round_at":null}')
                else:
                    await request.fulfill(content_type='application/javascript',
                        body="window.Telegram={WebApp:{initData:'fixture',ready(){},expand(){},"
                             "setHeaderColor(){},setBackgroundColor(){},close(){},"
                             "BackButton:{onClick(){},hide(){},show(){}},"
                             "HapticFeedback:{selectionChanged(){}}}}")

            await page.route('**/*', route)
            await page.goto('https://example.test/app')
            await page.locator('nav [data-tab=history]').click()

            async def assert_steps():
                fold = page.locator('#history-cycle-steps')
                await expect(fold).to_be_visible()
                preview = fold.locator('summary .history-fold-label small')
                await expect(preview).to_contain_text('99 записей')
                await expect(preview).to_contain_text('последний: шаг 99')
                await expect(preview).to_contain_text('2.00x')
                # Even while collapsed, the latest action and its actual net
                # must be visible in the compact summary.
                assert '+' in await preview.inner_text()
                await fold.locator('summary').click()
                entries = fold.locator('.history-steps-list .history-metric')
                await expect(entries).to_have_count(10)
                await expect(entries.first).to_contain_text('Шаг 99 ·')
                await expect(entries.last).to_contain_text('Шаг 90 ·')
                await expect(fold.locator('.history-step-more')).to_have_text(
                    'Показать все 99 шагов')
                await fold.locator('.history-step-more').click()
                await expect(entries).to_have_count(99)
                await expect(entries.first).to_contain_text('Шаг 99 ·')
                await expect(entries.last).to_contain_text('Шаг 1 ·')
                await expect(fold.locator('.history-step-latest')).to_be_visible()
                await fold.locator('.history-step-latest').click()
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                await fold.locator('.history-step-more').click()
                await expect(entries).to_have_count(10)
                await expect(fold.locator('.history-step-latest')).to_be_hidden()

            for number, (list_view, detail_view) in enumerate([
                ('cycles', 'cycle'), ('high_cycles', 'high_cycle'),
                ('target_cycles', 'target_cycle'), ('recovery_cycles', 'recovery_cycle')], 1):
                await page.evaluate('(number) => window.openStrategyHistory(number)', number)
                await expect(page.locator('#history-demo-cycles')).to_be_visible()
                await page.locator('#history-demo-cycles > summary').click()
                await page.locator('#history-demo-cycles .history-item').first.click()
                await expect(page.locator('#history-cycle-steps')).to_be_visible()
                await assert_steps()
                assert list_view in calls and detail_view in calls

            # An archived cycle uses the same renderer but never edits archive.
            await page.locator('[data-history-view=days]').click()
            await page.locator('#history-management > summary').click()
            await page.locator('#history-archive').click()
            await page.locator('[data-archive-view=archive_cycles]').click()
            await expect(page.locator('#history-archive-cycles')).to_be_visible()
            await page.locator('#history-archive-cycles > summary').click()
            await page.locator('#history-archive-cycles .history-item').first.click()
            await assert_steps()
            assert 'archive_cycle' in calls
            assert not errors, errors
            await browser.close()

    asyncio.run(run())
