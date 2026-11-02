"""Atomic split storage for current demo state and completed cycles."""
import json
import asyncio
from contextlib import AsyncExitStack


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
                history = json.dumps(state.pop('history'), separators=(',', ':'))
                raw = json.dumps(state, separators=(',', ':'))
                pipe.hset('luckyjet:demo_sessions', uid, raw)
                if self.saved_history.get(uid) != history:
                    pipe.hset('luckyjet:demo_completed_cycles', uid, history)
                histories[uid] = history
            await pipe.execute()
            self.saved_history.update(histories)

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
