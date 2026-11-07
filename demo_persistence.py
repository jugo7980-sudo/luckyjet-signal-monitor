"""Atomic split storage for current demo state and completed cycles."""
import json
import asyncio
import logging
import hashlib
from datetime import datetime
from contextlib import AsyncExitStack

from luckyjet_signal.demo_ledger import MAX_HISTORY

ARCHIVE_CYCLES = 'luckyjet:demo:archive:cycles:'
ARCHIVE_ENTRIES = 'luckyjet:demo:archive:entries:'


def _cycle_archival_record(entry):
    if not isinstance(entry, dict):
        return None
    try:
        ended = datetime.fromisoformat(str(entry['ended_at']).replace('Z', '+00:00'))
        if ended.tzinfo is None:
            return None
        raw = json.dumps(entry, sort_keys=True, separators=(',', ':'))
        identifier = hashlib.sha256(raw.encode('utf-8')).hexdigest()
        return identifier, ended.timestamp(), raw
    except (KeyError, TypeError, ValueError, OverflowError):
        return None



class DemoPersistence:
    def __init__(self):
        self.saved_history = {}
        self.locks = {}

    async def save(self, redis, user_id, ledger):
        await self.save_many(redis, {str(int(user_id)): ledger})

    async def save_many(self, redis, ledgers):
        if not ledgers:
            return
        async with AsyncExitStack() as stack:
            for uid in sorted(ledgers):
                await stack.enter_async_context(self.locks.setdefault(uid, asyncio.Lock()))
            pipe = redis.pipeline()
            histories = {}
            for uid, ledger in ledgers.items():
                state = ledger.dump()
                state_history = state.pop('history')
                history = json.dumps(state_history, separators=(',', ':'))
                raw = json.dumps(state, separators=(',', ':'))
                pipe.hset('luckyjet:demo_sessions', uid, raw)
                if self.saved_history.get(str(uid)) != history:
                    pipe.hset('luckyjet:demo_completed_cycles', uid, history)
                    # Use the same Redis MULTI/EXEC transaction as the DEMO
                    # checkpoint. Archive entries are add-only and idempotent.
                    if hasattr(pipe, 'hsetnx') and hasattr(pipe, 'zadd'):
                        for entry in state_history:
                            record = _cycle_archival_record(entry)
                            if record is None:
                                continue
                            key, stamp, payload = record
                            pipe.hsetnx(ARCHIVE_ENTRIES + str(uid), key, payload)
                            pipe.zadd(ARCHIVE_CYCLES + str(uid), {key: stamp}, nx=True)
                histories[uid] = history
            await pipe.execute()
            self.saved_history.update(histories)

    async def restore(self, redis, ledgers):
        histories = await redis.hgetall('luckyjet:demo_completed_cycles')
        for uid, raw in histories.items():
            ledger = ledgers.get(int(uid))
            if ledger is not None:
                try:
                    history = json.loads(raw)
                    if not isinstance(history, list) or any(not isinstance(entry, dict) for entry in history):
                        raise ValueError('Invalid completed cycle history')
                except (ValueError, TypeError):
                    logging.getLogger(__name__).error('Corrupt cycle history skipped: user_id=%s', uid)
                    continue
                ledger.history = history
                ledger.backfill_daily_steps()
                ledger.history = ledger.history[:MAX_HISTORY]
                # Keep the persisted raw snapshot as the comparison baseline so
                # the next normal save compacts any oversized legacy history.
                self.saved_history[str(uid)] = raw
                # Existing in-memory histories from before archival support
                # can be imported without changing current DEMO state.
                if hasattr(redis, 'zadd') and hasattr(redis, 'hsetnx'):
                    for entry in ledger.history:
                        record = _cycle_archival_record(entry)
                        if record is None:
                            continue
                        key, stamp, payload = record
                        await redis.hsetnx(ARCHIVE_ENTRIES + str(uid), key, payload)
                        await redis.zadd(ARCHIVE_CYCLES + str(uid), {key: stamp}, nx=True)
