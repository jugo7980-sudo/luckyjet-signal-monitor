"""Archive watchdog: gap detection, stale collectors, deduplicated alerts."""
import asyncio
import sys
from pathlib import Path

from fakeredis.aioredis import FakeRedis

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from archive_watchdog import (
    WATCHDOG_LEASE, WATCHDOG_STATE, assess_health, check_once,
)
from round_archive import HEALTH_KEY, COLLECTOR_HEALTH_PREFIX


def test_assess_health_checks_new_rounds_and_both_collectors():
    now = 1_000_000
    good = {"last_round_at": str(now - 1000)}
    ok = {"last_ok_at": str(now - 1000)}
    assert assess_health(good, ok, ok, now_ms=now)[0] == "healthy"
    assert assess_health({"last_round_at": str(now-200_000)}, ok, ok, now_ms=now)[0] == "no_new_rounds"
    assert assess_health(good, {}, ok, now_ms=now)[0] == "collector_stale"
    assert assess_health(good, {}, {}, now_ms=now)[0] == "both_collectors_stale"


def test_watchdog_only_alerts_on_new_incidents_and_recovery():
    async def run():
        redis = FakeRedis(decode_responses=True)
        now = 1_000_000
        sent = []
        async def send(message):
            sent.append(message)
            return True

        await redis.hset(HEALTH_KEY, mapping={"last_round_at": now - 1000, "gaps": 0})
        for name in ("primary", "backup"):
            await redis.hset(COLLECTOR_HEALTH_PREFIX+name, "last_ok_at", now - 1000)
        await redis.hset("luckyjet:demo_sessions", "42", '{"balance_cents":123456}')
        assert await check_once(redis, "primary", send, now_ms=now) == "initialized"
        assert sent == []
        assert await check_once(redis, "backup", send, now_ms=now) == "not_leader"
        await redis.delete(WATCHDOG_LEASE)

        assert await check_once(redis, "backup", send, now_ms=now) == "unchanged"
        await redis.delete(WATCHDOG_LEASE)
        await redis.hset(COLLECTOR_HEALTH_PREFIX+"backup", "last_ok_at", now - 200_000)
        assert await check_once(redis, "primary", send, now_ms=now) == "notified"
        assert len(sent) == 1 and "резервный" in sent[-1]
        await redis.delete(WATCHDOG_LEASE)
        assert await check_once(redis, "backup", send, now_ms=now) == "unchanged"
        assert len(sent) == 1
        await redis.delete(WATCHDOG_LEASE)

        await redis.hset(COLLECTOR_HEALTH_PREFIX+"backup", "last_ok_at", now)
        assert await check_once(redis, "primary", send, now_ms=now) == "notified"
        assert "оба сборщика работают" in sent[-1]
        await redis.delete(WATCHDOG_LEASE)

        await redis.hset(HEALTH_KEY, "gaps", 1)
        assert await check_once(redis, "backup", send, now_ms=now) == "notified"
        assert "разрыв истории" in sent[-1]
        await redis.delete(WATCHDOG_LEASE)
        assert await check_once(redis, "primary", send, now_ms=now) == "unchanged"
        assert len(sent) == 3
        assert await redis.hget("luckyjet:demo_sessions", "42") == '{"balance_cents":123456}'
        await redis.aclose()
    asyncio.run(run())


def test_failed_delivery_is_retried_once_without_advancing_state():
    async def run():
        redis = FakeRedis(decode_responses=True)
        now = 1_000_000
        good = now - 1000
        await redis.hset(HEALTH_KEY, mapping={"last_round_at": good, "gaps": 0})
        for name in ("primary", "backup"):
            await redis.hset(COLLECTOR_HEALTH_PREFIX+name, "last_ok_at", good)
        async def ok(message):
            return True
        async def fail(message):
            return False
        assert await check_once(redis, "primary", ok, now_ms=now) == "initialized"
        await redis.delete(WATCHDOG_LEASE)
        await redis.hset(HEALTH_KEY, "last_round_at", now - 200000)
        assert await check_once(redis, "backup", fail, now_ms=now) == "delivery_failed"
        assert await redis.hget(WATCHDOG_STATE, "status") == "healthy"
        await redis.delete(WATCHDOG_LEASE)
        assert await check_once(redis, "primary", ok, now_ms=now) == "notified"
        assert await redis.hget(WATCHDOG_STATE, "status") == "no_new_rounds"
        await redis.aclose()
    asyncio.run(run())
