"""Post-reset live views must not show old maxima or phantom historical days."""
import asyncio
import hashlib
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from fakeredis.aioredis import FakeRedis

from coefficient_stats import CoefficientStats
from luckyjet_reliable_gate import ReliableTelegramPasswordGate
from mini_app_controls import controls
from mini_app_history import history
from round_archive import STREAM_KEY
from test_archive_four_tabs import signed, query

TZ = ZoneInfo('Asia/Vladivostok')


def test_live_history_reset_recalculates_current_day_without_mutating_archive():
    async def run():
        redis = FakeRedis(decode_responses=True)
        gate = ReliableTelegramPasswordGate(None)
        gate.redis = redis
        gate.authorized.add(42)
        gate.coefficient_stats = CoefficientStats(redis)
        today = datetime.now(TZ).date().isoformat()
        base = int(datetime.now(TZ).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()*1000)
        await redis.zadd('luckyjet:coef:days', {today: base/1000})
        await redis.zadd('luckyjet:coef:high:days', {today: base/1000})
        old_hash = hashlib.sha256(b'old-high').hexdigest()[:16]
        old_stamp = (base + 1000) / 1000
        old = {'id': old_hash, 'value': 250, 'stamp': old_stamp, 'day': today}
        await redis.hset('luckyjet:coef:high:entries', old_hash, json.dumps(old))
        await redis.lpush(f'luckyjet:coef:high:{today}', old_hash)
        await redis.hset('luckyjet:coef:high:state',
                         mapping={'last_high': json.dumps(old), 'since': '5', 'gap': '0'})
        ledger = gate._demo_ledger(42)
        ledger.daily_max_low_streak[today] = 9
        await redis.xadd(STREAM_KEY,
                         {'type': 'round', 'coefficient': '1.2', 'observed_at': str(base+1000),
                          'round_id': 'old-1'}, id=f'{base+1000}-0')
        request = json.dumps({'action': 'history_reset', 'value': 'all', 'confirm': True}).encode()
        code, _, _ = await controls(signed(42), gate, 'fixture', 'POST', request)
        assert code == '200 OK'
        for view in ('days', 'high_days', 'max_steps'):
            code, data = await query(gate, 42, view)
            assert code == '200 OK'
            assert not data['days'], (view, data)
        # Retain source archive after all visible history clears.
        assert (await query(gate, 42, 'archive_day', day=today))[1]['summary']['total'] == 1
        marker = json.loads(await redis.hget('luckyjet:history:user-reset:42', 'days'))
        next_ms = max(int(marker['at_ms']) + 3000, base + 3000)
        await redis.xadd(STREAM_KEY, {'type':'round', 'round_id':'new-1',
                                     'coefficient': '1.3',
                                     'observed_at': str(next_ms)}, id=f'{next_ms}-0')
        code, data = await query(gate, 42, 'max_steps')
        assert code == '200 OK'
        assert data['days'][0]['steps'] == 1, data
        assert (await query(gate, 42, 'days'))[1]['days'] == [today]

        new_hash = hashlib.sha256(b'new-high').hexdigest()[:16]
        new_stamp = next_ms / 1000 + 1
        new = {'id': new_hash, 'value': 180, 'stamp': new_stamp, 'day': today}
        await redis.hset('luckyjet:coef:high:entries', new_hash, json.dumps(new))
        await redis.lpush(f'luckyjet:coef:high:{today}', new_hash)
        await redis.hset('luckyjet:coef:high:state', 'last_high', json.dumps(new))
        code, data = await query(gate, 42, 'high_days')
        assert code == '200 OK'
        assert data['days'] == [today]
        assert data['last_high']['value'] == 180
        assert (await query(gate, 42, 'archive_high_days'))[1]['days'][0]['day'] == today
        await redis.aclose()
        gate.redis = None
        await gate.close()
    asyncio.run(run())
