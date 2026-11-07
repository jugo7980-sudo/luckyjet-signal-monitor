"""Archive view must remain read-only, date-bounded and cursor-paginated."""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import fakeredis.aioredis
import pytest

from mini_app_history import archive_page, _date
from round_archive import STREAM_KEY


def test_archive_cursor_pages_preserve_source_rows():
    async def run():
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        day = '2026-11-08'
        start = int(datetime(2026, 11, 8, tzinfo=ZoneInfo('Asia/Vladivostok')).timestamp() * 1000)
        for i in range(115):
            await redis.xadd(STREAM_KEY, {
                'type': 'round',
                'round_id': 'round-' + str(i),
                'coefficient': str(1.1 + i / 100),
                'source_time': '',
                'observed_at': str(start + i * 1000),
                'collector': 'primary',
            }, id=f'{start + i * 1000}-0')
        first = await archive_page(redis, day)
        second = await archive_page(redis, day, first['next_cursor'])
        third = await archive_page(redis, day, second['next_cursor'])
        assert [len(a['items']) for a in (first, second, third)] == [50, 50, 15]
        assert first['next_cursor'] and second['next_cursor']
        assert third['next_cursor'] is None
        rows = first['items'] + second['items'] + third['items']
        assert len({row['round_id'] for row in rows}) == 115
        assert rows[0]['round_id'] == 'round-114'
        assert rows[-1]['round_id'] == 'round-0'
        assert all('collector' not in row for row in rows)
        assert await redis.xlen(STREAM_KEY) == 115
        await redis.aclose()
    asyncio.run(run())


def test_archive_day_bounds_and_event_types():
    async def run():
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        day0 = int(datetime(2026, 11, 7, tzinfo=ZoneInfo('Asia/Vladivostok')).timestamp() * 1000)
        day1 = int(datetime(2026, 11, 8, tzinfo=ZoneInfo('Asia/Vladivostok')).timestamp() * 1000)
        day2 = int(datetime(2026, 11, 9, tzinfo=ZoneInfo('Asia/Vladivostok')).timestamp() * 1000)
        for i, stamp in enumerate((day0, day1, day1 + 1, day2)):
            await redis.xadd(STREAM_KEY, {
                'type': 'round' if i % 2 else 'gap',
                'round_id': str(i),
                'coefficient': '1.5',
                'observed_at': str(stamp),
            }, id=f'{stamp}-0')
        result = await archive_page(redis, '2026-11-08')
        assert [row['round_id'] for row in result['items']] == ['2', '1']
        assert result['next_cursor'] is None
        assert (await archive_page(redis, '2026-11-07'))['items'][0]['round_id'] == '0'
        await redis.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('cursor', ['bad', '0', '1-*', '-1-0', '1-2x', '1-' + '2' * 50])
def test_archive_rejects_non_stream_cursor(cursor):
    async def run():
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        with pytest.raises(ValueError):
            await archive_page(redis, '2026-11-08', cursor)
        await redis.aclose()
    asyncio.run(run())


def test_date_never_accepts_future_or_invalid_values():
    with pytest.raises(ValueError):
        _date('2099-01-01')
    with pytest.raises(ValueError):
        _date('2026-02-30')
