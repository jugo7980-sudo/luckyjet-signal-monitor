"""Reset only user's history presentation; preserve source receipts and DEMO state."""
import asyncio
import hashlib
import hmac
import json
import time
from datetime import datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fakeredis.aioredis import FakeRedis
from mini_app_controls import controls
from mini_app_history import history, _after_cycle_reset
from luckyjet_reliable_gate import ReliableTelegramPasswordGate
from round_archive import STREAM_KEY


def signed(uid):
    fields = {'auth_date': str(int(time.time())), 'user': json.dumps({'id': uid})}
    data = '\n'.join(f'{key}={value}' for key, value in sorted(fields.items()))
    secret = hmac.new(b'WebAppData', b'fixture', hashlib.sha256).digest()
    fields['hash'] = hmac.new(secret, data.encode(), hashlib.sha256).hexdigest()
    return 'tma ' + urlencode(fields)


class Stats:
    def __init__(self, redis):
        self.redis = redis

    async def days(self, page):
        items = await self.redis.zrevrange('luckyjet:coef:days', page * 10, page * 10 + 9)
        return items, await self.redis.zcard('luckyjet:coef:days')

    async def summary(self, day):
        return {'total': 999, 'lt2': 999}


def test_reset_all_scopes_is_per_user_and_read_only():
    async def run():
        redis = FakeRedis(decode_responses=True)
        gate = ReliableTelegramPasswordGate(None)
        gate.redis = redis
        gate.authorized.update((42, 43))
        gate.coefficient_stats = Stats(redis)
        day = datetime.now(ZoneInfo('Asia/Vladivostok')).date().isoformat()
        now = int(time.time() * 1000)
        await redis.zadd('luckyjet:coef:days', {day: now / 1000})
        before_ids = (now - 30000, now - 29000)
        for i, stamp in enumerate(before_ids):
            await redis.xadd(STREAM_KEY, {
                'type': 'round', 'round_id': f'old-{i}', 'coefficient': '1.2',
                'source_time': '', 'observed_at': str(stamp),
            }, id=f'{stamp}-0')
        original_ledger = gate._demo_ledger(42).dump()
        request = json.dumps({'action': 'history_reset', 'value': 'all', 'confirm': True}).encode()
        code, _, body = await controls(signed(42), gate, 'fixture', 'POST', request)
        assert code == '200 OK', body
        assert gate._demo_ledger(42).dump() == original_ledger
        assert await redis.xlen(STREAM_KEY) == 2
        saved = await redis.hgetall('luckyjet:history:user-reset:42')
        assert set(saved) == {'days', 'high_days', 'cycles', 'max_steps'}
        assert await redis.hgetall('luckyjet:history:user-reset:43') == {}
        for i, value in enumerate((1.1, 1.5, 3.2)):
            stamp = now + 1000 + i
            await redis.xadd(STREAM_KEY, {
                'type': 'round', 'round_id': f'new-{i}', 'coefficient': str(value),
                'source_time': '', 'observed_at': str(stamp),
            }, id=f'{stamp}-0')
        _, _, raw = await history(signed(42), gate, 'fixture', 'view=day&day=' + day)
        result = json.loads(raw)
        assert result['view_reset'] and result['summary']['total'] == 3
        assert result['summary']['lt2'] == 2
        _, _, raw = await history(signed(43), gate, 'fixture', 'view=day&day=' + day)
        assert json.loads(raw)['summary']['total'] == 999
        _, _, raw = await history(signed(42), gate, 'fixture', 'view=max_step_day&day=' + day)
        assert json.loads(raw)['steps'] == 2
        _, _, raw = await history(signed(42), gate, 'fixture', 'view=archive&day=' + day)
        archive = json.loads(raw)
        assert len(archive['items']) == 5
        assert archive['aggregate']['total'] == 5
        assert archive['aggregate']['lt2'] == 4
        assert archive['max_low_streak'] == 2
        assert {row['round_id'] for row in archive['items']} == {
            'old-0', 'old-1', 'new-0', 'new-1', 'new-2'}
        assert await redis.xlen(STREAM_KEY) == 5
        await redis.aclose()
        gate.redis = None
        await gate.close()
    asyncio.run(run())


def test_history_reset_requires_confirm_and_valid_scope():
    async def run():
        gate = ReliableTelegramPasswordGate(None)
        gate.authorized.add(42)
        gate.redis = FakeRedis(decode_responses=True)
        for scope, confirm in [('all', False), ('archive', True), (None, True)]:
            request = json.dumps({'action': 'history_reset', 'value': scope, 'confirm': confirm}).encode()
            code, _, _ = await controls(signed(42), gate, 'fixture', 'POST', request)
            assert code == '400 Bad Request'
        assert await gate.redis.hgetall('luckyjet:history:user-reset:42') == {}
        await gate.redis.aclose()
        gate.redis = None
        await gate.close()
    asyncio.run(run())


def test_cycle_filter_uses_completed_at_not_history_index():
    marker = {'at_ms': 1790000000000}
    assert _after_cycle_reset({'ended_at': '2027-01-01T00:00:00+00:00'}, marker)
    assert not _after_cycle_reset({'ended_at': '2026-01-01T00:00:00+00:00'}, marker)
    assert not _after_cycle_reset({'ended_at': 'not-a-date'}, marker)
