"""Safety tests for two concurrent collectors and append-only Redis archive."""
import asyncio
import sys
from pathlib import Path

import pytest
from fakeredis.aioredis import FakeRedis

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from round_archive import (
    RoundArchive, STREAM_KEY, SEEN_KEY, CURSOR_KEY, HEALTH_KEY,
    normalize_snapshot,
)


def snapshot(*ids):
    # Newest first, like the real Lucky Jet completed history endpoint.
    return [{"id": str(i), "coefficient": float(i) + 1} for i in ids]


def contents(events):
    return [(fields.get("type"), fields.get("round_id")) for _, fields in events]


def test_double_collector_atomic_dedupe_and_chronological_ingestion():
    async def run():
        redis = FakeRedis(decode_responses=True)
        one, two = RoundArchive(redis, "primary"), RoundArchive(redis, "backup")
        first = await one.append_snapshot(snapshot(3, 2, 1), observed_at_ms=1000)
        assert first == {"added": 3, "gap": False, "baseline": True, "duplicate": False}
        events = await redis.xrange(STREAM_KEY)
        assert contents(events) == [
            ("baseline", None), ("round", "1"), ("round", "2"), ("round", "3"),
        ]
        duplicates = await two.append_snapshot(snapshot(3, 2, 1), observed_at_ms=1100)
        assert duplicates["added"] == 0 and duplicates["duplicate"]
        assert await redis.get(CURSOR_KEY) == "3"

        # Two services receive the next completed rounds simultaneously.
        a, b = await asyncio.gather(
            one.append_snapshot(snapshot(6, 5, 4, 3), observed_at_ms=1200),
            two.append_snapshot(snapshot(6, 5, 4, 3), observed_at_ms=1201),
        )
        assert sorted([a["added"], b["added"]]) == [0, 3]
        assert await redis.get(CURSOR_KEY) == "6"
        assert contents(await redis.xrange(STREAM_KEY)) == [
            ("baseline", None), ("round", "1"), ("round", "2"),
            ("round", "3"), ("round", "4"), ("round", "5"), ("round", "6"),
        ]

        # Slow collector's stale snapshot must not rewind cursor or append.
        delayed = await two.append_snapshot(snapshot(4, 3, 2), observed_at_ms=1001)
        assert delayed["added"] == 0
        assert await redis.get(CURSOR_KEY) == "6"
        assert await redis.hlen(SEEN_KEY) == 6
        assert await redis.hget(HEALTH_KEY, "gaps") is None
        await redis.aclose()
    asyncio.run(run())


def test_missing_history_creates_explicit_gap_not_fake_continuity():
    async def run():
        redis = FakeRedis(decode_responses=True)
        writer = RoundArchive(redis, "primary")
        await writer.append_snapshot(snapshot(3, 2, 1), observed_at_ms=1000)
        res = await writer.append_snapshot(snapshot(10, 9, 8), observed_at_ms=2000)
        assert res == {"added": 3, "gap": True, "baseline": False, "duplicate": False}
        assert contents(await redis.xrange(STREAM_KEY)) == [
            ("baseline", None), ("round", "1"), ("round", "2"), ("round", "3"),
            ("gap", None), ("round", "8"), ("round", "9"), ("round", "10"),
        ]
        assert await redis.hget(HEALTH_KEY, "gaps") == "1"
        assert await redis.get(CURSOR_KEY) == "10"
        assert await redis.hlen(SEEN_KEY) == 6
        await redis.aclose()
    asyncio.run(run())


def test_missing_last_id_but_older_seen_round_is_safe_overlap():
    async def run():
        redis = FakeRedis(decode_responses=True)
        writer = RoundArchive(redis, "one")
        await writer.append_snapshot(snapshot(3, 2, 1), observed_at_ms=1000)
        # Another source returns a narrow but overlapping history with ID 2,
        # omitting the latest cursor 3. This is not a gap.
        r = await writer.append_snapshot(snapshot(5, 4, 2), observed_at_ms=2000)
        assert r["added"] == 2 and not r["gap"]
        assert await redis.get(CURSOR_KEY) == "5"
        assert contents(await redis.xrange(STREAM_KEY))[-2:] == [
            ("round", "4"), ("round", "5")
        ]
        await redis.aclose()
    asyncio.run(run())


def test_invalid_snapshot_never_moves_archive_cursor():
    async def run():
        redis = FakeRedis(decode_responses=True)
        writer = RoundArchive(redis, "one")
        for payload in ([{"id": None, "coefficient": 1.11}],
                        [{"id": "x", "coefficient": float("nan")}],
                        [{"id": "x", "coefficient": 0.5}], []):
            with pytest.raises(ValueError):
                await writer.append_snapshot(payload)
        assert not await redis.exists(STREAM_KEY)
        assert not await redis.exists(CURSOR_KEY)
        # Duplicate rows cannot lead to duplicate source IDs.
        assert normalize_snapshot(snapshot(1, 1, 2)) == [
            {"id": "1", "coefficient": "2.0", "source_time": ""},
            {"id": "2", "coefficient": "3.0", "source_time": ""},
        ]
        await redis.aclose()
    asyncio.run(run())


def test_existing_legacy_app_keys_are_not_modified():
    async def run():
        redis = FakeRedis(decode_responses=True)
        await redis.set("luckyjet:state", '{"last_round_id":"old"}')
        await redis.hset("luckyjet:demo_sessions", "42",
                         '{"balance_cents":1030000,"enabled":true}')
        writer = RoundArchive(redis, "backup")
        await writer.append_snapshot(snapshot(5, 4, 3), observed_at_ms=1000)
        assert await redis.get("luckyjet:state") == '{"last_round_id":"old"}'
        assert await redis.hget("luckyjet:demo_sessions", "42") == (
            '{"balance_cents":1030000,"enabled":true}'
        )
        await redis.aclose()
    asyncio.run(run())
