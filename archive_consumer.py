"""Replay the durable round archive into the existing bot and DEMO ledgers.

Disabled by default until collectors are verified. In archive mode the web/bot
service does not open the game source. This allows site login, UI deployments
and CAPTCHA work to proceed without pausing *collection*. On restart the
consumer replays every available archived event in order before showing a new
balance. Per-user DEMO round IDs prevent double staking after a crash.

This bridges to a future independently deployed DEMO worker; UI and DEMO are
still co-located until their control channels have been migrated.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from types import SimpleNamespace

import main as base
from coefficient_stats import event_time
from round_archive import STREAM_KEY, SEEN_KEY, CURSOR_KEY

log = logging.getLogger("luckyjet.archive_consumer")
UI_CURSOR_KEY = "luckyjet:archive:v1:ui:cursor"
_SOURCE_STREAM_ID = re.compile(r"^\d+-\d+$")


async def select_initial_cursor(redis, gate, last_round_id):
    """Align the archived log with the last source round already processed.

    Never replay the full initial source snapshot into an existing DEMO balance.
    If history cannot be aligned, flag DEMO as unverified and begin at the
    current archive head, rather than inventing transactions.
    """
    existing = await redis.get(UI_CURSOR_KEY)
    if existing and _SOURCE_STREAM_ID.fullmatch(existing):
        return existing
    identifier = str(last_round_id or "")
    mapped = await redis.hget(SEEN_KEY, identifier) if identifier else None
    if mapped and _SOURCE_STREAM_ID.fullmatch(mapped):
        await redis.set(UI_CURSOR_KEY, mapped, nx=True)
        log.info("Archive consumer anchored to last confirmed legacy source ID")
        return mapped
    while True:
        latest = await redis.xrevrange(STREAM_KEY, count=1)
        if latest:
            cursor = latest[0][0]
            await gate.on_history_gap()
            await redis.set(UI_CURSOR_KEY, cursor, nx=True)
            log.warning("Archive/legacy source cursor could not be aligned; gap recorded")
            return cursor
        await asyncio.sleep(1.0)


def parse_event_timestamp(fields):
    source_time = fields.get("source_time") or ""
    event = event_time({"timestamp": source_time}) if source_time else None
    if event is not None:
        return event.timestamp(), False
    try:
        return int(fields["observed_at"]) / 1000, True
    except (KeyError, ValueError, TypeError):
        raise ValueError("Archive event has no usable timestamp") from None


async def process_archive_event(redis, gate, state, panel, stream_id, fields):
    """Process one event; commit legacy cursor only after DEMO state is saved."""
    kind = fields.get("type")
    if kind == "gap":
        await gate.on_history_gap()
        await redis.set(UI_CURSOR_KEY, stream_id)
        return
    if kind == "baseline":
        await redis.set(UI_CURSOR_KEY, stream_id)
        return
    if kind != "round":
        raise ValueError("Unknown archive event type: " + str(kind))

    rid = str(fields.get("round_id") or "")
    formatted = base.format_history_coefficient(fields.get("coefficient"))
    if not rid or formatted is None:
        raise ValueError("Invalid completed round in archive")
    timestamp, uncertain = parse_event_timestamp(fields)
    # The per-user DEMO ledger saves this source ID with each balance update,
    # so replay after a process crash cannot charge the same stake twice.
    previous_panel = list(panel.history)
    panel.history = base.prepend_history(panel.history, formatted, limit=panel.limit)
    try:
        await gate.on_round(formatted, round_id=rid)
    except Exception:
        panel.history = previous_panel
        raise
    stats = getattr(gate, "coefficient_stats", None)
    if stats is not None:
        await stats.record(
            formatted, round_id=rid, timestamp=timestamp,
            uncertain=uncertain, ordered=True,
        )
    state.last_round_id = rid
    state.previous = [float(formatted[:-1]), *state.previous][:20]
    # Transactional cursor + state update. DEMO writes occurred beforehand.
    # A crash before this commit triggers a safe replay guarded by source ID.
    async with redis.pipeline(transaction=True) as pipe:
        pipe.set("luckyjet:state", json.dumps(state.dump()))
        pipe.set(UI_CURSOR_KEY, stream_id)
        await pipe.execute()


async def consume_archive(gate, state, panel):
    """Follow immutable round events, independently of site login/browser."""
    await gate.ready_event.wait()
    redis = gate.redis
    if redis is None:
        raise RuntimeError("Archive consumer requires persistent Redis")
    cursor = await select_initial_cursor(redis, gate, state.last_round_id)
    log.info("Archive consumer active; reading append-only source events")
    while True:
        messages = await redis.xread({STREAM_KEY: cursor}, count=100, block=3000)
        if not messages:
            continue
        for _, batch in messages:
            for event_id, fields in batch:
                await process_archive_event(redis, gate, state, panel, event_id, fields)
                cursor = event_id
