"""Persist the latest notice per user; coalesce obsolete display updates."""
import asyncio
import json
import uuid
from contextvars import ContextVar
import logging

streak_view = ContextVar('streak_view', default=None)
log = logging.getLogger(__name__)
KEY = 'luckyjet:pending_notices'


class NotificationQueue:
    def __init__(self, gate):
        self.gate = gate
        self.queue = asyncio.Queue()
        self.pending = {}
        self.scheduled = set()
        self.lock = asyncio.Lock()
        self.task = None

    def _schedule(self, uid):
        if uid not in self.scheduled:
            self.scheduled.add(uid)
            self.queue.put_nowait(uid)
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())

    async def restore(self):
        if self.gate.redis:
            values = await self.gate.redis.hgetall(KEY)
            for uid, raw in values.items():
                try:
                    item = json.loads(raw)
                    user = int(uid)
                    if not isinstance(item['streak'], list):
                        raise ValueError('Invalid streak')
                    if user in self.gate.authorized:
                        self.pending[user] = item
                        self._schedule(user)
                    else:
                        await self.gate.redis.hdel(KEY, uid)
                except (ValueError, TypeError, KeyError):
                    log.error('Corrupt pending notice skipped: user_id=%s', uid)

    async def submit(self, users, streak, ending=None):
        async with self.lock:
            items = {int(uid): {'version': uuid.uuid4().hex, 'streak': list(streak), 'ending': ending}
                     for uid in list(users)}
            if self.gate.redis and items:
                pipe = self.gate.redis.pipeline()
                for uid, item in items.items():
                    pipe.hset(KEY, str(uid), json.dumps(item))
                await pipe.execute()
            self.pending.update(items)
            for uid in items:
                self._schedule(uid)

    async def run(self):
        while True:
            uid = await self.queue.get()
            item = self.pending.get(uid)
            try:
                if item is None:
                    continue
                token = streak_view.set((self.gate, item['streak']))
                try:
                    if uid in self.gate.authorized:
                        result = await self.gate._update_streak_notice(uid, ending=item['ending'])
                        if result is False:
                            raise RuntimeError('Telegram notice not applied')
                finally:
                    streak_view.reset(token)
                async with self.lock:
                    if self.pending.get(uid) == item:
                        if self.gate.redis:
                            await self.gate.redis.hdel(KEY, str(uid))
                        self.pending.pop(uid, None)
                    else:
                        self.queue.put_nowait(uid)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('Notification deferred')
                await asyncio.sleep(1)
                self.queue.put_nowait(uid)
            finally:
                if uid not in self.pending:
                    self.scheduled.discard(uid)
                self.queue.task_done()

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
