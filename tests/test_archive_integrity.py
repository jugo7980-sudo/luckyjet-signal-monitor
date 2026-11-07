"""Live archive coverage is evidence, never a fabricated completeness guarantee."""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fakeredis.aioredis import FakeRedis

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from archive_integrity import TZ, archive_day_evidence
from round_archive import STREAM_KEY


async def add(redis, dt, kind, rid="", source_time=""):
    ms = int(dt.timestamp() * 1000)
    await redis.xadd(
        STREAM_KEY,
        {"type": kind, "round_id": rid, "source_time": source_time,
         "observed_at": str(ms)},
        id=f"{ms}-0",
    )


def test_full_day_verifies_only_confirmed_round_ids_and_times():
    async def run():
        r = FakeRedis(decode_responses=True)
        start = datetime(2026, 11, 6, tzinfo=TZ)
        await add(r, start - timedelta(minutes=1), "round", "0",
                  (start - timedelta(minutes=1)).isoformat())
        await add(r, start + timedelta(minutes=10), "round", "1",
                  (start + timedelta(minutes=10)).isoformat())
        await add(r, start + timedelta(hours=19), "round", "2",
                  (start + timedelta(hours=19)).isoformat())
        await add(r, start + timedelta(days=1, minutes=1), "round", "3",
                  (start + timedelta(days=1, minutes=1)).isoformat())
        ok = await archive_day_evidence(r, "2026-11-06", legacy_total=2)
        assert ok["status"] == "verified", ok
        assert ok["archived_rounds"] == 2
        assert ok["counts_match"] is True
        wrong = await archive_day_evidence(r, "2026-11-06", legacy_total=200)
        assert wrong["status"] == "unverified"
        assert "не совпадают" in " ".join(wrong["reasons"])
        await r.aclose()
    asyncio.run(run())


def test_gap_marks_day_unverified_even_when_daily_counters_match():
    async def run():
        r = FakeRedis(decode_responses=True)
        start = datetime(2026, 11, 6, tzinfo=TZ)
        await add(r, start - timedelta(minutes=1), "round", "0",
                  (start - timedelta(minutes=1)).isoformat())
        await add(r, start + timedelta(hours=6), "round", "1",
                  (start + timedelta(hours=6)).isoformat())
        await add(r, start + timedelta(hours=12), "gap")
        await add(r, start + timedelta(hours=14), "round", "2",
                  (start + timedelta(hours=14)).isoformat())
        await add(r, start + timedelta(days=1, minutes=1), "round", "3",
                  (start + timedelta(days=1, minutes=1)).isoformat())
        bad = await archive_day_evidence(r, "2026-11-06", legacy_total=2)
        assert bad["gaps"] == 1
        assert bad["status"] == "unverified"
        assert "пропущенные" in " ".join(bad["reasons"])
        await r.aclose()
    asyncio.run(run())


def test_missing_game_times_or_baseline_cannot_be_reported_as_verified():
    async def run():
        r = FakeRedis(decode_responses=True)
        start = datetime(2026, 11, 6, tzinfo=TZ)
        await add(r, start + timedelta(hours=4), "baseline")
        await add(r, start + timedelta(hours=4, minutes=1), "round", "1")
        await add(r, start + timedelta(days=1, hours=1), "round", "2")
        status = await archive_day_evidence(r, "2026-11-06", legacy_total=1)
        assert status["status"] == "unverified"
        assert status["source_time_missing"] == 1
        assert status["baselines"] == 1
        assert not status["archive_started_before_day"]
        await r.aclose()
    asyncio.run(run())


def test_round_with_source_timestamp_from_previous_day_rejected_for_verification():
    async def run():
        r = FakeRedis(decode_responses=True)
        start = datetime(2026, 11, 6, tzinfo=TZ)
        await add(r, start - timedelta(minutes=1), "round", "0",
                  (start - timedelta(minutes=1)).isoformat())
        await add(r, start + timedelta(minutes=1), "round", "1",
                  (start - timedelta(hours=3)).isoformat())
        await add(r, start + timedelta(days=1, minutes=1), "round", "2",
                  (start + timedelta(days=1, minutes=1)).isoformat())
        status = await archive_day_evidence(r, "2026-11-06", legacy_total=1)
        assert status["status"] == "unverified"
        assert status["cross_day_timestamp_mismatches"] == 1
        await r.aclose()
    asyncio.run(run())
