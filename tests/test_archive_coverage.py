"""Source-poll coverage is an independent immutable-minute audit, not a round count."""
import asyncio
from datetime import datetime, timedelta

from fakeredis.aioredis import FakeRedis

from archive_coverage import collector_minute_coverage, TZ
from archive_integrity import archive_day_evidence
from round_archive import (
    RoundArchive, COVERAGE_PREFIX, COVERAGE_STARTED_KEY,
    STREAM_KEY,
)


def millis(dt):
    return int(dt.timestamp() * 1000)


def test_captures_successful_duplicate_polls_by_both_collectors():
    async def run():
        r = FakeRedis(decode_responses=True)
        dt = datetime(2026, 11, 6, 3, 15, 5, tzinfo=TZ)
        row = [{'id': 'game-1', 'coefficient': 1.38}]
        primary = RoundArchive(r, 'primary')
        backup = RoundArchive(r, 'backup')
        assert (await primary.append_snapshot(row, observed_at_ms=millis(dt)))['added'] == 1
        assert (await backup.append_snapshot(row, observed_at_ms=millis(dt) + 1000))['duplicate']
        coverage = await r.hgetall(COVERAGE_PREFIX + '2026-11-06')
        assert sorted(coverage) == ['backup:195', 'primary:195']
        assert int(await r.get(COVERAGE_STARTED_KEY)) == millis(dt)
        assert await r.xlen(STREAM_KEY) == 2
        assert (await primary.append_snapshot(row, observed_at_ms=millis(dt) + 30_000))['duplicate']
        assert await r.hlen(COVERAGE_PREFIX + '2026-11-06') == 2
        await r.aclose()
    asyncio.run(run())


def test_detects_minute_without_any_collector_and_keeps_source_data():
    async def run():
        r = FakeRedis(decode_responses=True)
        dt = datetime(2026, 11, 6, tzinfo=TZ)
        day_ms = millis(dt)
        await r.set(COVERAGE_STARTED_KEY, day_ms - 60_000)
        await r.hset(COVERAGE_PREFIX + '2026-11-06', mapping={
            'primary:0': day_ms, 'backup:1': day_ms + 60_000,
            'backup:3': day_ms + 180_000, 'primary:4': day_ms + 240_000,
        })
        await r.hset('luckyjet:demo_sessions', '42', '{"balance_cents":123456}')
        data = await collector_minute_coverage(r, '2026-11-06', now_ms=day_ms + 5*60_000)
        assert data['status'] == 'poll_gaps'
        assert data['expected_minutes'] == 5
        assert data['observed_minutes'] == 4
        assert data['primary_minutes'] == 2
        assert data['backup_minutes'] == 2
        assert data['missing_minutes'] == 1
        assert data['longest_missing_minutes'] == 1
        assert data['missing_intervals'] == [{
            'start': '00:02', 'end': '00:03', 'minutes': 1,
        }]
        assert await r.hget('luckyjet:demo_sessions', '42') == '{"balance_cents":123456}'
        await r.aclose()
    asyncio.run(run())


def test_full_1440_minutes_proves_poll_evidence_not_completeness_of_game():
    async def run():
        r = FakeRedis(decode_responses=True)
        dt = datetime(2026, 11, 6, tzinfo=TZ)
        await r.set(COVERAGE_STARTED_KEY, millis(dt) - 60_000)
        await r.hset(COVERAGE_PREFIX + '2026-11-06',
                     mapping={f'primary:{i}': 'yes' for i in range(1440)})
        data = await collector_minute_coverage(
            r, '2026-11-06', now_ms=millis(dt + timedelta(days=1, minutes=1)))
        assert data['status'] == 'full_poll_coverage'
        assert data['complete_day'] is True
        assert data['observed_minutes'] == 1440
        assert data['missing_minutes'] == 0
        assert data['backup_minutes'] == 0
        assert data['missing_intervals'] == []
        await r.aclose()
    asyncio.run(run())


def test_never_implies_coverage_for_pre_instrumentation_days():
    async def run():
        r = FakeRedis(decode_responses=True)
        day0 = datetime(2026, 11, 6, tzinfo=TZ)
        day1 = datetime(2026, 11, 7, tzinfo=TZ)
        early = await collector_minute_coverage(r, '2026-11-06',
                                                now_ms=millis(day1 + timedelta(days=2)))
        assert early['status'] == 'not_recorded'
        assert early['observed_minutes'] is None
        await r.set(COVERAGE_STARTED_KEY, millis(day1 + timedelta(hours=11)))
        early = await collector_minute_coverage(r, '2026-11-06',
                                                now_ms=millis(day1 + timedelta(days=2)))
        assert early['status'] == 'not_recorded'
        part = await collector_minute_coverage(r, '2026-11-07',
                                               now_ms=millis(day1 + timedelta(days=1)))
        assert part['status'] == 'poll_gaps'
        assert part['complete_day'] is False
        assert part['expected_minutes'] > 0
        await r.aclose()
    asyncio.run(run())


def test_daily_integrity_embeds_coverage_without_rewriting_archive():
    async def run():
        r = FakeRedis(decode_responses=True)
        day = datetime(2026, 11, 6, tzinfo=TZ)
        before = millis(day - timedelta(minutes=1))
        inside = millis(day + timedelta(minutes=4))
        after = millis(day + timedelta(days=1, minutes=1))
        for i, stamp in enumerate([before, inside, after]):
            await r.xadd(STREAM_KEY, {
                'type': 'round', 'round_id': str(i), 'coefficient': '1.5',
                'observed_at': str(stamp), 'source_time': '',
            }, id=f'{stamp}-0')
        original = await r.xlen(STREAM_KEY)
        await r.set(COVERAGE_STARTED_KEY, millis(day) - 100_000)
        await r.hset(COVERAGE_PREFIX+'2026-11-06', 'primary:4', 'ok')
        result = await archive_day_evidence(r, '2026-11-06', legacy_total=1)
        assert result['archived_rounds'] == 1
        assert result['poll_coverage']['missing_minutes'] > 0
        assert result['poll_coverage']['observed_minutes'] == 1
        assert await r.xlen(STREAM_KEY) == original
        await r.aclose()
    asyncio.run(run())
