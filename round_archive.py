"""Append-only completed-round archive shared by independent source collectors.

Never resets historical keys. All writes for one source snapshot are atomic:
- only unique source round IDs are accepted
- overlapping snapshots are appended in chronological order
- duplicate and stale observations cannot move the cursor backwards
- source gaps become explicit stream records rather than invented results

Both collectors can run concurrently; Redis serializes Lua scripts.
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any

STREAM_KEY = "luckyjet:archive:v1:events"
SEEN_KEY = "luckyjet:archive:v1:seen"
CURSOR_KEY = "luckyjet:archive:v1:last_id"
HEALTH_KEY = "luckyjet:archive:v1:health"
COLLECTOR_HEALTH_PREFIX = "luckyjet:archive:v1:collector:"
COVERAGE_PREFIX = "luckyjet:archive:v1:coverage:"
COVERAGE_STARTED_KEY = "luckyjet:archive:v1:coverage:started_at"
COVERAGE_TZ = ZoneInfo("Asia/Vladivostok")

# No MAXLEN or expiration: archival retention must not silently discard history.
APPEND_LUA = r"""
local snapshots = cjson.decode(ARGV[1])
local source = ARGV[2]
local now = ARGV[3]
local previous = redis.call('GET', KEYS[3])
-- Track a successful, validated source snapshot (including duplicates).
-- One field per collector and Vladivostok minute, with no key expiry.
local function record_coverage()
    redis.call('HSETNX', KEYS[5], ARGV[4], now)
    redis.call('SET', KEYS[6], now, 'NX')
end
local anchor = 0
for i=1,#snapshots do
    if previous and snapshots[i].id == previous then anchor = i; break end
end
if anchor == 0 then
    -- A second collector may return an older view. Find a previously seen
    -- round in this view before deciding it represents a true discontinuity.
    for i=1,#snapshots do
        if redis.call('HEXISTS',KEYS[2],snapshots[i].id) == 1 then
            anchor = i
            break
        end
    end
end
local novel = 0
for i=1,#snapshots do
    if redis.call('HEXISTS',KEYS[2],snapshots[i].id) == 0 then
        novel = novel + 1
    end
end
if novel == 0 then
    redis.call('HSET',KEYS[4],'last_polled_at',now,'last_collector',source)
    record_coverage()
    return {0, 0, 0, 1}
end

local first = previous == false
local gap = not first and anchor == 0
if first then
    redis.call('XADD',KEYS[1],'*',
        'type','baseline','observed_at',now,'collector',source)
elseif gap then
    redis.call('XADD',KEYS[1],'*',
        'type','gap','previous_round_id',previous,
        'next_round_id',snapshots[#snapshots].id,
        'observed_at',now,'collector',source)
    redis.call('HINCRBY',KEYS[4],'gaps',1)
end

local inserted = 0
local limit = #snapshots
if anchor > 0 then limit = anchor - 1 end
for i=limit,1,-1 do
    local item = snapshots[i]
    if redis.call('HSETNX',KEYS[2],item.id,now) == 1 then
        local event_id = redis.call('XADD',KEYS[1],'*',
            'type','round','round_id',item.id,
            'coefficient',item.coefficient,
            'source_time',item.source_time,
            'observed_at',now,'collector',source)
        -- Stable source ID -> stream ID enables lossless catch-up after upgrades.
        redis.call('HSET',KEYS[2],item.id,event_id)
        inserted = inserted + 1
    end
end
if inserted > 0 then
    redis.call('SET',KEYS[3],snapshots[1].id)
    redis.call('HSET',KEYS[4],'last_round_at',now,
        'last_round_id',snapshots[1].id,
        'last_collector',source)
end
redis.call('HSET',KEYS[4],'last_polled_at',now)
record_coverage()
return {inserted, gap and 1 or 0, first and 1 or 0, 0}
"""


def normalize_snapshot(rounds: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Validate newest-first completed rounds, never guess an ID."""
    if not isinstance(rounds, list):
        raise ValueError("A completed-round snapshot must be a list")
    clean = []
    ids: set[str] = set()
    for entry in rounds[:50]:
        if not isinstance(entry, dict):
            continue
        identifier = str(entry.get("id") or "").strip()
        if not identifier or len(identifier) > 256 or identifier in ids:
            continue
        try:
            value = float(str(entry.get("coefficient")).rstrip("xX").replace(",", "."))
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(value) or not 1 <= value <= 100000:
            continue
        ids.add(identifier)
        stamp = next(
            (str(entry[key]) for key in (
                "finished_at", "finishedAt", "ended_at", "endedAt", "timestamp"
            ) if entry.get(key) is not None),
            "",
        )
        clean.append({
            "id": identifier, "coefficient": str(value),
            "source_time": stamp[:128],
        })
    if not clean:
        raise ValueError("Snapshot has no completed rounds with source IDs")
    return clean


class RoundArchive:
    def __init__(self, redis, collector_name: str):
        if not collector_name or len(collector_name) > 60:
            raise ValueError("Collector identity must be a short stable name")
        self.redis = redis
        self.name = collector_name

    async def append_snapshot(self, rounds, *, observed_at_ms=None) -> dict:
        snapshot = normalize_snapshot(rounds)
        timestamp_ms = int(time.time() * 1000 if observed_at_ms is None else observed_at_ms)
        now = str(timestamp_ms)
        minute = datetime.fromtimestamp(timestamp_ms / 1000, COVERAGE_TZ)
        coverage_key = COVERAGE_PREFIX + minute.strftime("%Y-%m-%d")
        minute_field = f"{self.name}:{minute.hour * 60 + minute.minute}"
        result = await self.redis.eval(
            APPEND_LUA, 6, STREAM_KEY, SEEN_KEY, CURSOR_KEY, HEALTH_KEY,
            coverage_key, COVERAGE_STARTED_KEY,
            json.dumps(snapshot, separators=(",", ":")), self.name, now, minute_field,
        )
        added, gap, baseline, duplicate = map(int, result)
        await self.redis.hset(
            COLLECTOR_HEALTH_PREFIX + self.name,
            mapping={"last_ok_at": now, "last_snapshot_size": len(snapshot),
                     "last_new_rounds": added},
        )
        return {"added": added, "gap": bool(gap), "baseline": bool(baseline),
                "duplicate": bool(duplicate)}

    async def heartbeat_error(self, reason: str):
        await self.redis.hset(
            COLLECTOR_HEALTH_PREFIX + self.name,
            mapping={"last_error_at": int(time.time()*1000),
                     "last_error_type": str(reason)[:90]},
        )
