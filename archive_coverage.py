"""Read-only evidence of successful source polls, independent of DEMO/UI resets.

The source archive records what the collectors actually received. A successful
poll every minute is operational evidence, NOT proof that the third-party game
published every completed round or that a missing interval has a known result.
Historical days before this instrumentation are explicitly unavailable.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from round_archive import COVERAGE_PREFIX, COVERAGE_STARTED_KEY

TZ = ZoneInfo("Asia/Vladivostok")
MINUTES_PER_DAY = 1440
MINUTE_MS = 60_000


def _str(value):
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _clock(minute):
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _unavailable(day):
    return {
        "day": day,
        "status": "not_recorded",
        "tracked_from": None,
        "complete_day": False,
        "expected_minutes": None,
        "observed_minutes": None,
        "primary_minutes": None,
        "backup_minutes": None,
        "missing_minutes": None,
        "longest_missing_minutes": None,
        "missing_intervals": [],
    }


async def collector_minute_coverage(redis, day, *, now_ms=None):
    """Count minutes with at least one successful validated snapshot.

    A day is fully covered only when both day boundaries are past, telemetry
    started no later than the beginning of the day, and one or both collectors
    successfully recorded an observation in EACH completed minute.
    """
    baseline = _unavailable(day)
    if redis is None or not hasattr(redis, "get") or not hasattr(redis, "hkeys"):
        return baseline
    raw_started = await redis.get(COVERAGE_STARTED_KEY)
    try:
        started_ms = int(raw_started) if raw_started is not None else None
    except (ValueError, TypeError, OverflowError):
        started_ms = None
    if started_ms is None or started_ms <= 0:
        return baseline

    begin = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=TZ)
    begin_ms = int(begin.timestamp() * 1000)
    end_ms = int((begin + timedelta(days=1)).timestamp() * 1000)
    current_ms = int(time.time() * 1000 if now_ms is None else now_ms)
    if current_ms < begin_ms or started_ms >= end_ms:
        return baseline

    # Ignore the incomplete minute where this feature first came online; also
    # ignore the current incomplete minute while the day is still progressing.
    start_index = max(0, (max(started_ms, begin_ms) - begin_ms + MINUTE_MS - 1) // MINUTE_MS)
    end_index = min(MINUTES_PER_DAY, max(0, (min(current_ms, end_ms) - begin_ms) // MINUTE_MS))
    partial_scope = started_ms > begin_ms or current_ms < end_ms
    expected = max(0, end_index - start_index)

    fields = await redis.hkeys(COVERAGE_PREFIX + day)
    observed = set()
    primary = set()
    backup = set()
    for raw in fields:
        field = _str(raw)
        name, sep, minute_text = field.rpartition(":")
        if not sep or not minute_text.isdecimal():
            continue
        minute = int(minute_text)
        if not (start_index <= minute < end_index):
            continue
        observed.add(minute)
        if name == "primary":
            primary.add(minute)
        elif name == "backup":
            backup.add(minute)

    # Report missing stretches, not "missing rounds": the source might be
    # silent and individual round results cannot be reconstructed here.
    intervals = []
    run_start = None
    for minute in range(start_index, end_index):
        if minute not in observed:
            if run_start is None:
                run_start = minute
        elif run_start is not None:
            intervals.append((run_start, minute))
            run_start = None
    if run_start is not None:
        intervals.append((run_start, end_index))
    missing = sum(end - start for start, end in intervals)
    top_intervals = sorted(intervals, key=lambda value: (-(value[1] - value[0]), value[0]))[:5]
    top_intervals.sort()
    result = {
        "day": day,
        "status": (
            "poll_gaps" if missing > 0 else
            "partial_period" if partial_scope else
            "full_poll_coverage"
        ),
        "tracked_from": datetime.fromtimestamp(started_ms / 1000, TZ).isoformat(),
        "complete_day": current_ms >= end_ms and started_ms <= begin_ms,
        "expected_minutes": expected,
        "observed_minutes": len(observed),
        "primary_minutes": len(primary),
        "backup_minutes": len(backup),
        "missing_minutes": missing,
        "longest_missing_minutes": max((end - start for start, end in intervals), default=0),
        "missing_intervals": [{
            "start": _clock(start),
            "end": _clock(end),
            "minutes": end - start,
        } for start, end in top_intervals],
    }
    return result
