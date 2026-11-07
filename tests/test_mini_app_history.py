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
from mini_app_history import history
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
                if url.endswith('/history.js'):
                    await request.fulfill(content_type='application/javascript', body=response('/history.js')[2])
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
                        data.update(day=DAY, summary={'total': 100, 'lt2_tracking': 1, 'lt2': 45})
                    elif view == 'high_days':
                        data.update(days=[DAY], last_high={'value': '150', 'stamp': 1793980800},
                                    since_high=7, gap=False)
                    elif view == 'high_day':
                        data.update(day=DAY, items=[{'id': ITEM, 'value': '150', 'stamp': 1793980800,
                                                     'rounds': 29}])
                    elif view == 'high_item':
                        data['item'] = {'value': '150', 'stamp': 1793980800, 'rounds': 29}
                    elif view == 'cycles':
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
            await page.get_by_text('06.11.2026').first.wait_for()
            await page.locator('#history-content button.history-item').first.click()
            await page.get_by_text('Всего раундов').wait_for()
            await page.locator('#history-back').click()

            await page.locator('[data-history-view=high_days]').click()
            await page.locator('#history-content button.history-item').first.click()
            await page.locator('#history-content button.history-item').first.click()
            await page.get_by_text('29 раундов').first.wait_for()
            await page.locator('#history-back').click()

            await page.locator('[data-history-view=cycles]').click()
            await page.locator('#history-content button.history-item').first.click()
            await page.get_by_text('Среднее шагов').wait_for()
            await page.locator('#history-back').click()
            await page.locator('#history-content button.history-item').nth(1).click()
            await page.get_by_text('Чистый итог').wait_for()

            await page.locator('[data-history-view=max_steps]').click()
            await page.get_by_text('5 шаг.').wait_for()
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert not errors, errors
            assert {'days', 'day', 'high_days', 'high_day', 'high_item', 'cycles',
                    'cycle', 'demo_stats', 'max_steps'} <= set(calls)
            await browser.close()
    asyncio.run(run())
