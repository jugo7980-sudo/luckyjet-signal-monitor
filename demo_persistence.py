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
HIGH_COMPLETED = 'luckyjet:demo:high_completed_cycles'


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
        self.saved_high_history = {}
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
            high_histories = {}
            for uid, ledger in ledgers.items():
                state = ledger.dump()
                state_history = state.pop('history')
                high_state_history = state.pop('high_history', [])
                pending_archive = state.pop('pending_archive_cycles', [])
                history = json.dumps(state_history, separators=(',', ':'))
                high_history = json.dumps(high_state_history, separators=(',', ':'))
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
                if high_state_history or self.saved_high_history.get(str(uid)) is not None:
                    if self.saved_high_history.get(str(uid)) != high_history:
                        pipe.hset(HIGH_COMPLETED, uid, high_history)
                        if hasattr(pipe, 'hsetnx') and hasattr(pipe, 'zadd'):
                            for entry in high_state_history:
                                record = _cycle_archival_record(entry)
                                if record is None:
                                    continue
                                key, stamp, payload = record
                                pipe.hsetnx(ARCHIVE_ENTRIES + str(uid), key, payload)
                                pipe.zadd(ARCHIVE_CYCLES + str(uid), {key: stamp}, nx=True)
                    high_histories[str(uid)] = high_history
                if hasattr(pipe, 'hsetnx') and hasattr(pipe, 'zadd'):
                    for entry in pending_archive:
                        record = _cycle_archival_record(entry)
                        if record is None:
                            continue
                        key, stamp, payload = record
                        pipe.hsetnx(ARCHIVE_ENTRIES + str(uid), key, payload)
                        pipe.zadd(ARCHIVE_CYCLES + str(uid), {key: stamp}, nx=True)
                histories[uid] = history
            await pipe.execute()
            self.saved_history.update(histories)
            self.saved_high_history.update(high_histories)
            for ledger in ledgers.values():
                ledger.pending_archive_cycles.clear()

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

        high_histories = await redis.hgetall(HIGH_COMPLETED)
        for uid, raw in high_histories.items():
            ledger = ledgers.get(int(uid))
            if ledger is None:
                continue
            try:
                high_history = json.loads(raw)
                if not isinstance(high_history, list) or any(
                    not isinstance(entry, dict) for entry in high_history
                ):
                    raise ValueError('Invalid high-gap cycle history')
            except (ValueError, TypeError):
                logging.getLogger(__name__).error('Corrupt high-gap history skipped')
                continue
            ledger.high_history = high_history[:MAX_HISTORY]
            self.saved_high_history[str(uid)] = raw
            if hasattr(redis, 'zadd') and hasattr(redis, 'hsetnx'):
                for entry in ledger.high_history:
                    record = _cycle_archival_record(entry)
                    if record is None:
                        continue
                    key, stamp, payload = record
                    await redis.hsetnx(ARCHIVE_ENTRIES + str(uid), key, payload)
                    await redis.zadd(ARCHIVE_CYCLES + str(uid), {key: stamp}, nx=True)
