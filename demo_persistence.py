"""Atomic split storage for current demo state and completed cycles."""
import json
import asyncio


class DemoPersistence:
    def __init__(self):
        self.saved_history = {}
        self.locks = {}

    async def save(self, redis, user_id, ledger):
        uid = str(int(user_id))
        async with self.locks.setdefault(uid, asyncio.Lock()):
            await self._save(redis, uid, ledger)

    async def _save(self, redis, uid, ledger):
        state = ledger.dump()
        history = json.dumps(state.pop('history'), separators=(',', ':'))
        raw = json.dumps(state, separators=(',', ':'))
        # Both keys change atomically on migration and on cycle completion.
        pipe = redis.pipeline()
        pipe.hset('luckyjet:demo_sessions', uid, raw)
        if self.saved_history.get(uid) != history:
            pipe.hset('luckyjet:demo_completed_cycles', uid, history)
        await pipe.execute()
        self.saved_history[uid] = history

    async def restore(self, redis, ledgers):
        histories = await redis.hgetall('luckyjet:demo_completed_cycles')
        for uid, raw in histories.items():
            ledger = ledgers.get(int(uid))
            if ledger is not None:
                history = json.loads(raw)
                if not isinstance(history, list):
                    raise ValueError('Invalid completed cycle history')
                ledger.history = history
                self.saved_history[str(uid)] = raw
