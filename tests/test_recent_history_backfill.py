"""Historical coefficients must be available on first request, not accumulated live."""
import asyncio
import json
from fakeredis.aioredis import FakeRedis
from luckyjet_reliable_gate import ReliableTelegramPasswordGate
from mini_app_api import overview, _recent_from_archive
from round_archive import STREAM_KEY
from test_mini_app_api import signed


def test_archive_values_and_boundaries_are_exact_and_no_rounds_are_invented():
    async def run():
        redis = FakeRedis(decode_responses=True)
        values = []
        t = 1
        for i in range(1, 121):
            if i in (1, 55, 96):
                await redis.xadd(STREAM_KEY, {'type': 'baseline' if i == 1 else 'gap'},
                                 id=f'{t * 1000}-0')
                t += 1
            v = '1.50' if i in (100, 101) else f'{1 + i / 100:.2f}'
            values.append(f'{float(v):.2f}x')
            await redis.xadd(STREAM_KEY,
                             {'type': 'round', 'round_id': str(i), 'coefficient': v},
                             id=f'{t * 1000}-0')
            t += 1
        gate = ReliableTelegramPasswordGate(None, history_provider=lambda: list(reversed(values[-20:])))
        gate.authorized.add(42)
        gate.redis = redis
        for limit in (20, 30, 50, 100):
            code, _, payload = await overview('tma ' + signed(), gate, 'fixture', f'limit={limit}')
            data = json.loads(payload)
            assert code == '200 OK'
            assert data['limit'] == limit
            assert data['coefficients'] == list(reversed(values[-limit:]))
            assert len(data['coefficients']) == limit
            assert data['status']['history_count'] == limit
            assert data['summary']['total'] == limit
        assert await redis.xlen(STREAM_KEY) == 123  # read-only: no archive mutation
        assert values[99] == values[100] == '1.50x'
        assert (await _recent_from_archive(redis, 100))[19] == values[100]
        await redis.aclose()
        gate.redis = None
        await gate.close()
    asyncio.run(run())


def test_short_archive_falls_back_to_available_live_and_does_not_pad():
    async def run():
        redis = FakeRedis(decode_responses=True)
        for i in range(1, 13):
            await redis.xadd(STREAM_KEY, {'type': 'round', 'round_id': str(i),
                                         'coefficient': '1.50'}, id=f'{i*1000}-0')
        live = [f'{i}.00x' for i in range(20, 0, -1)]
        gate = ReliableTelegramPasswordGate(None, history_provider=lambda: live)
        gate.authorized.add(42)
        gate.redis = redis
        status, _, body = await overview('tma ' + signed(), gate, 'fixture', 'limit=100')
        data = json.loads(body)
        assert status == '200 OK'
        assert data['coefficients'] == live
        assert data['status']['history_count'] == 20
        assert (await redis.xlen(STREAM_KEY)) == 12
        await redis.aclose()
        gate.redis = None
        await gate.close()
    asyncio.run(run())
