"""Archive tabs never apply reset markers; DEMO cycles survive every reset."""
import asyncio
import hashlib
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlencode
import hmac
import time

from fakeredis.aioredis import FakeRedis

from demo_persistence import DemoPersistence, ARCHIVE_CYCLES, ARCHIVE_ENTRIES
from luckyjet_signal.demo_ledger import DemoLedger
from luckyjet_reliable_gate import ReliableTelegramPasswordGate
from mini_app_controls import controls
from mini_app_history import history
from round_archive import STREAM_KEY

TZ = ZoneInfo('Asia/Vladivostok')


def signed(uid):
    fields = {'auth_date': str(int(time.time())),
              'user': json.dumps({'id': uid})}
    data = '\n'.join(f'{key}={value}' for key, value in sorted(fields.items()))
    secret = hmac.new(b'WebAppData', b'fixture', hashlib.sha256).digest()
    fields['hash'] = hmac.new(secret, data.encode(), hashlib.sha256).hexdigest()
    return 'tma ' + urlencode(fields)


async def query(gate, uid, view, **kwargs):
    code, _, raw = await history(signed(uid), gate, 'fixture',
                                 urlencode({'view': view, **kwargs}))
    return code, json.loads(raw)


def finish(ledger, *, currency=None):
    ledger.enabled = True
    ledger.set_trigger(2)
    for value in (1.2, 1.4, 2.5):
        ledger.process_round(value)
    assert ledger.history and ledger.history[0]['outcome'] == 'recovered'


def test_cycles_archived_across_demo_reset_and_user_reset_and_currencies():
    async def run():
        redis = FakeRedis(decode_responses=True)
        gate = ReliableTelegramPasswordGate(None)
        gate.redis = redis
        gate.authorized.update((42, 43))
        original = gate._demo_ledger(42)
        finish(original)
        await gate._demo_storage.save(redis, 42, original)
        first_id = (await redis.zrevrange(ARCHIVE_CYCLES + '42', 0, 0))[0]
        reset = json.dumps({'action': 'demo_reset', 'confirm': True, 'value': {
            'currency': 'KRW', 'balance': '100000', 'stake': '100',
            'revision': original.reset_revision,
        }}).encode()
        status, _, raw = await controls(signed(42), gate, 'fixture', 'POST', reset)
        assert status == '200 OK', raw
        original = gate._demo_ledger(42)
        assert original.history == []
        assert await redis.zcard(ARCHIVE_CYCLES + '42') == 1
        finish(original)
        await gate._demo_storage.save(redis, 42, original)
        assert await redis.zcard(ARCHIVE_CYCLES + '42') == 2
        code, cycles = await query(gate, 42, 'archive_cycles')
        assert code == '200 OK'
        assert cycles['total'] == 2
        assert {x['currency'] for x in cycles['items']} == {'USD', 'KRW'}
        code, stats = await query(gate, 42, 'archive_demo_stats')
        assert code == '200 OK'
        assert stats['currencies']['USD']['total'] == 1
        assert stats['currencies']['KRW']['total'] == 1
        assert first_id in [x['id'] for x in cycles['items']]
        code, detail = await query(gate, 42, 'archive_cycle', id=first_id)
        assert code == '200 OK'
        assert detail['item']['currency'] == 'USD'
        assert detail['item']['balance_before_cents'] is not None
        code, _ = await query(gate, 43, 'archive_cycle', id=first_id)
        assert code == '404 Not Found'
        req = json.dumps({'action': 'history_reset', 'confirm': True, 'value': 'all'}).encode()
        code, _, _ = await controls(signed(42), gate, 'fixture', 'POST', req)
        assert code == '200 OK'
        assert (await query(gate, 42, 'archive_cycles'))[1]['total'] == 2
        assert await redis.zcard(ARCHIVE_CYCLES + '42') == 2
        assert await redis.hlen(ARCHIVE_ENTRIES + '42') == 2
        await redis.aclose()
        gate.redis = None
        await gate.close()
    asyncio.run(run())


def test_source_archive_tabs_remain_read_only_after_history_reset():
    async def run():
        redis = FakeRedis(decode_responses=True)
        gate = ReliableTelegramPasswordGate(None)
        gate.redis = redis
        gate.authorized.add(42)
        start = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        begin = int(start.timestamp()*1000)
        for i, coefficient in enumerate((1.1, 1.3, 150.0, 1.2, 220.0, 3.0)):
            stamp = begin + (i + 2)*1000
            await redis.xadd(STREAM_KEY, {
                'type': 'round', 'round_id': str(i), 'coefficient': str(coefficient),
                'observed_at': str(stamp), 'source_time': '',
            }, id=f'{stamp}-0')
        before = await redis.xlen(STREAM_KEY)
        code, days = await query(gate, 42, 'archive_days')
        assert code == '200 OK'
        assert days['days'][0]['day'] == start.date().isoformat()
        code, result = await query(gate, 42, 'archive_day', day=start.date().isoformat())
        assert code == '200 OK'
        assert result['summary']['total'] == 6
        assert result['summary']['lt2'] == 3
        code, highs = await query(gate, 42, 'archive_high_day', day=start.date().isoformat())
        assert code == '200 OK'
        assert highs['total'] == 2
        assert highs['items'][0]['rounds'] == 2
        assert highs['items'][1]['rounds'] is None
        code, item = await query(gate, 42, 'archive_high_item',
                                 day=start.date().isoformat(), id=highs['items'][0]['id'])
        assert code == '200 OK' and item['item']['value'] == 220.0
        code, steps = await query(gate, 42, 'archive_max_step_day', day=start.date().isoformat())
        assert code == '200 OK' and steps['steps'] == 2
        request = json.dumps({'action': 'history_reset', 'value': 'all', 'confirm': True}).encode()
        status, _, _ = await controls(signed(42), gate, 'fixture', 'POST', request)
        assert status == '200 OK'
        assert (await query(gate, 42, 'archive_day', day=start.date().isoformat()))[1]['summary']['total'] == 6
        assert await redis.xlen(STREAM_KEY) == before
        await redis.aclose()
        gate.redis = None
        await gate.close()
    asyncio.run(run())


def test_restore_backfills_legacy_cycles_without_deleting_current_session():
    async def run():
        redis = FakeRedis(decode_responses=True)
        ledger = DemoLedger(enabled=True, trigger_streak=2)
        finish(ledger)
        original = ledger.history[:]
        await redis.hset('luckyjet:demo_completed_cycles', '42', json.dumps(original))
        restored = {42: DemoLedger()}
        store = DemoPersistence()
        await store.restore(redis, restored)
        assert restored[42].history == original
        assert await redis.zcard(ARCHIVE_CYCLES + '42') == 1
        await store.restore(redis, restored)
        assert await redis.zcard(ARCHIVE_CYCLES + '42') == 1
        await redis.aclose()
    asyncio.run(run())
