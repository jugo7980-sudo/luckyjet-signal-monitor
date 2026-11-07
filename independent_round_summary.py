"""Independent, read-only analytics over the append-only completed-round archive.

This module never reads/writes DEMO, user history resets, strategy state or balances.
Each request scans the immutable Redis Stream in bounded batches, and distinguishes
archived observations from provably complete game history.
"""
from __future__ import annotations

import asyncio
import math
from urllib.parse import parse_qs

from mini_app_api import json_response, telegram_user
from round_archive import STREAM_KEY

MAX_WINDOW = 50000
BATCH = 300
MAX_MATCH_ROWS = 50


def _decode(value):
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def _threshold(value):
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError("Invalid threshold") from None
    if not math.isfinite(number) or not (1 <= number <= 100000):
        raise ValueError("Invalid threshold")
    return number


def _params(query):
    args = parse_qs(query, keep_blank_values=True, max_num_fields=3)
    if set(args) != {"rounds", "target", "since"} or any(len(v) != 1 for v in args.values()):
        raise ValueError("Invalid parameters")
    raw = args["rounds"][0]
    if not raw.isascii() or not raw.isdecimal() or not (1 <= len(raw) <= 6):
        raise ValueError("Invalid round count")
    count = int(raw)
    if not 1 <= count <= MAX_WINDOW:
        raise ValueError("Round count out of bounds")
    return count, _threshold(args["target"][0]), _threshold(args["since"][0])


def _has_discontinuity(markers, begin, end):
    """Marker position lies after begin and before end (newest-first)."""
    return any(begin < pos <= end for pos in markers)


def _stats(window, threshold, markers, *, start_index=0):
    matches = []
    values = []
    longest = running = 0
    for offset, row in enumerate(window):
        index = start_index + offset
        if index in markers:
            running = 0
        value = row["value"]
        values.append(value)
        if value >= threshold:
            matches.append({"index": index, **row})
            running = 0
        else:
            running += 1
            longest = max(longest, running)
    intervals = []
    for i in range(len(matches) - 1):
        newer, older = matches[i], matches[i + 1]
        if not _has_discontinuity(markers, newer["index"], older["index"]):
            intervals.append(older["index"] - newer["index"])
    return {
        "count": len(matches),
        "average": round(sum(values) / len(values), 2) if values else None,
        "maximum": max(values) if values else None,
        "below_two": sum(value < 2 for value in values),
        "longest_wait": longest,
        "minimum_interval": min(intervals) if intervals else None,
        "average_interval": round(sum(intervals) / len(intervals), 2) if intervals else None,
        "maximum_interval": max(intervals) if intervals else None,
        "intervals_count": len(intervals),
        "matches": matches,
    }


async def analyze(redis, count, target, since):
    """Return snapshot-stable observed statistics, never alter source records."""
    # XREVRANGE's fixed initial upper bound excludes rounds inserted mid-request.
    rows = []
    markers = []
    seen_ids = set()
    duplicates = 0
    cursor = "+"
    scanned = 0
    limited_search = False
    search_budget = max(40000, 2 * count + 5000)
    last_target = last_since = None
    exhausted = False
    while len(rows) < 2 * count or last_target is None or last_since is None:
        if scanned >= search_budget:
            limited_search = True
            break
        batch = await redis.xrevrange(STREAM_KEY, max=cursor, min="-", count=BATCH)
        if not batch:
            exhausted = True
            break
        for stream_id, raw_fields in batch:
            scanned += 1
            fields = {_decode(k): _decode(v) for k, v in raw_fields.items()}
            kind = fields.get("type")
            if kind in {"gap", "baseline"}:
                markers.append(len(rows))
                continue
            if kind != "round":
                markers.append(len(rows))
                continue
            identifier = fields.get("round_id", "")
            if not identifier or identifier in seen_ids:
                duplicates += 1
                markers.append(len(rows))
                continue
            try:
                coefficient = float(fields["coefficient"])
                if not math.isfinite(coefficient) or coefficient < 1:
                    raise ValueError("Invalid coefficient")
            except (KeyError, ValueError, TypeError, OverflowError):
                markers.append(len(rows))
                continue
            seen_ids.add(identifier)
            row = {
                "round_id": identifier,
                "value": coefficient,
                "observed_at": fields.get("observed_at"),
                "source_time": fields.get("source_time") or None,
            }
            if coefficient >= target and last_target is None:
                last_target = len(rows)
            if coefficient >= since and last_since is None:
                last_since = len(rows)
            rows.append(row)
        cursor = "(" + _decode(batch[-1][0])
        if len(batch) < BATCH:
            exhausted = True
            break
        # Yield between bounded Redis calls; data collection is never paused.
        await asyncio.sleep(0)

    current_rows = rows[:count]
    previous_rows = rows[count:2 * count]
    active_markers = [n for n in markers if n <= count]
    current = _stats(current_rows, target, set(active_markers))
    previous = _stats(previous_rows, target, set(markers), start_index=count)
    active_matches = current.pop("matches")
    previous.pop("matches")

    details = []
    for i, record in enumerate(active_matches[:MAX_MATCH_ROWS]):
        older = active_matches[i + 1] if i + 1 < len(active_matches) else None
        distance = None
        if older and not _has_discontinuity(markers, record["index"], older["index"]):
            distance = older["index"] - record["index"]
        details.append({
            "round_id": record["round_id"],
            "coefficient": record["value"],
            "observed_at": record["observed_at"],
            "source_time": record["source_time"],
            "rounds_ago": record["index"],
            "interval_to_previous": distance,
        })
    full_window = len(current_rows) == count
    full_comparison = len(previous_rows) == count
    window_gaps = any(pos < count for pos in markers)
    comparison_gaps = any(pos < 2 * count for pos in markers)
    since_reliable = last_since is not None and not any(pos <= last_since for pos in markers)
    target_reliable = last_target is not None and not any(pos <= last_target for pos in markers)

    return {
        "rounds_requested": count,
        "rounds_available": len(current_rows),
        "target": target,
        "since_threshold": since,
        "source": "append_only_round_archive",
        "current": {**current, "complete_window": full_window and not window_gaps},
        "previous": {**previous, "complete_window": full_comparison and not comparison_gaps},
        "rounds_since_target": last_target if target_reliable else None,
        "rounds_since_since": last_since if since_reliable else None,
        "matches": details,
        "match_rows_limited": len(active_matches) > MAX_MATCH_ROWS,
        "quality": {
            "verified_game_history": False,
            "window_has_discontinuities": window_gaps,
            "comparison_has_discontinuities": comparison_gaps,
            "duplicate_entries": duplicates,
            "complete_window": full_window and not window_gaps,
            "complete_comparison": full_comparison and not comparison_gaps,
            "archive_exhausted": exhausted,
            "history_search_limited": limited_search,
            "unverified_source_timing": any(not r["source_time"] for r in current_rows),
        },
        "notice": "Статистика по сохранённым раундам, не прогноз и не доказательство полноты данных игрового сайта.",
    }


async def summary(authorization, gate, token, query=""):
    if not authorization.startswith("tma "):
        return json_response("401 Unauthorized", {"error": "Открой приложение через Telegram."})
    try:
        uid = telegram_user(authorization[4:], token)
    except (ValueError, KeyError, TypeError, AttributeError):
        return json_response("401 Unauthorized", {"error": "Сеанс истёк. Переоткрой приложение."})
    if gate is None:
        return json_response("503 Service Unavailable", {"error": "Бот запускается."})
    if not await gate.is_authorized(uid) or (uid in gate.blocked and not gate.is_admin(uid)):
        return json_response("403 Forbidden", {"error": "Недостаточно прав."})
    try:
        count, target, since = _params(query)
    except (ValueError, KeyError, TypeError):
        return json_response("400 Bad Request", {"error": "Раунды: 1–50000; X: от 1 до 100000."})
    redis = getattr(gate, "redis", None)
    if redis is None or not hasattr(redis, "xrevrange"):
        return json_response("503 Service Unavailable", {"error": "Архив недоступен."})
    try:
        result = await asyncio.wait_for(analyze(redis, count, target, since), timeout=25)
    except (TimeoutError, ConnectionError, OSError):
        return json_response("503 Service Unavailable", {"error": "Архив временно недоступен."})
    return json_response("200 OK", result)
