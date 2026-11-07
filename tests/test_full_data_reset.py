"""One-time complete reset: purge all user history/settings and prevent backfill."""
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

fakeredis = pytest.importorskip("fakeredis.aioredis")
pytest.importorskip("lupa")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from full_data_reset import (
    RESET_ID, RESET_MARKER_KEY, PRESERVE, apply_complete_data_reset,
)
from coefficient_stats import CoefficientStats


def test_full_reset_all_accounts_history_demo_settings_and_auth():
    async def run():
        redis = fakeredis.FakeRedis(decode_responses=True)
        await redis.set("luckyjet:telegram_polling_lease", "owner")
        await redis.set("luckyjet:release_version", "2026-11-02-release-1")
        await redis.set("luckyjet:access_reset_version", "2026-11-07-access-reset-1")
        await redis.set("another-app:keep", "unrelated")
        await redis.set("luckyjet:state", json.dumps({
            "previous": [4.2, 11.5], "streak": 5, "active": True,
            "last_round_id": "old-round",
        }))
        await redis.hset("luckyjet:demo_sessions", mapping={
            "42": json.dumps({"balance_cents": 1142000, "history": ["win"]}),
            "99": json.dumps({"balance_cents": 1560000, "history": ["loss"]}),
        })
        await redis.hset("luckyjet:demo_completed_cycles", "42", '["win"]')
        await redis.sadd("luckyjet:authorized_users", "42", "99")
        await redis.hset("luckyjet:web_registered", "42", '{"id":42}')
        await redis.hset("luckyjet:settings", "42", "custom")
        await redis.hset("luckyjet:thresholds", "42", "6")
        await redis.hset("luckyjet:history_messages", "42", "1234")
        await redis.hset("luckyjet:coef:2026-11-06", "total", "300")
        await redis.zadd("luckyjet:coef:days", {"2026-11-06": 123})
        await redis.hset("luckyjet:coef:high:entries", "xyz", '{"value": 1000}')
        await redis.set("luckyjet:public_restart:cooldown", "42")
        await redis.set("luckyjet:web_session:after-reset-20261107:test", "42")
        # Simulate a large number of completed rounds, use tiny SCAN batches.
        for i in range(150):
            await redis.set(f"luckyjet:coef:seen:{i}", "1")
        assert await apply_complete_data_reset(redis, scan_count=9) is True
        remaining = set(await redis.keys("luckyjet:*"))
        assert remaining == PRESERVE, remaining
        assert await redis.get(RESET_MARKER_KEY) == RESET_ID
        assert await redis.get("luckyjet:telegram_polling_lease") == "owner"
        assert await redis.get("another-app:keep") == "unrelated"
        assert await redis.get("luckyjet:state") is None
        assert await redis.hgetall("luckyjet:demo_sessions") == {}
        assert await redis.hgetall("luckyjet:web_registered") == {}
        assert await redis.smembers("luckyjet:authorized_users") == set()
        assert await redis.zcard("luckyjet:coef:days") == 0
        await redis.hset("luckyjet:demo_sessions", "99", '{"balance_cents":1000000}')
        assert await apply_complete_data_reset(redis) is False
        assert await redis.hget("luckyjet:demo_sessions", "99") is not None
        await redis.aclose()
    asyncio.run(run())


def test_first_post_reset_source_snapshot_is_not_counted_again():
    async def run():
        redis = fakeredis.FakeRedis(decode_responses=True)
        stats = CoefficientStats(redis)
        stats.reset_baseline_pending = True
        now = datetime.now(timezone.utc).timestamp()
        original = [
            {"id": "old-b", "coefficient": "4.50", "finished_at": now - 4},
            {"id": "old-a", "coefficient": "1.20", "finished_at": now - 8},
        ]
        await stats.ingest(original)
        assert not stats.reset_baseline_pending
        assert await redis.keys("luckyjet:coef:days") == []
        assert await redis.get("luckyjet:coef:seen:old-a") == "1"
        assert await redis.get("luckyjet:coef:seen:old-b") == "1"
        new = {"id": "new-c", "coefficient": "10.75", "finished_at": now + 2}
        await stats.ingest([new, *original])
        assert await redis.zcard("luckyjet:coef:days") == 1
        assert int(await redis.hget("luckyjet:coef:" + stats.day(now), "total")) == 1
        assert await redis.exists("luckyjet:coef:seen:new-c") == 1
        await stats.ingest([new, *original])
        assert int(await redis.hget("luckyjet:coef:" + stats.day(now), "total")) == 1
        await redis.aclose()
    asyncio.run(run())
