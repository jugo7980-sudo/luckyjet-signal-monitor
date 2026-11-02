"""Ordered ephemeral notices, isolated from round calculations."""
import asyncio
from contextvars import ContextVar
import logging

streak_view = ContextVar('streak_view', default=None)
log = logging.getLogger(__name__)


class NotificationQueue:
    def __init__(self, gate):
        self.gate = gate
        self.queue = asyncio.Queue(maxsize=128)
        self.task = None

    async def submit(self, users, streak, ending=None):
        if not users:
            return
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())
        await self.queue.put((list(users), list(streak), ending))

    async def run(self):
        while True:
            users, streak, ending = await self.queue.get()
            token = streak_view.set((self.gate, streak))
            try:
                for uid in users:
                    if uid in self.gate.authorized:
                        try:
                            await self.gate._update_streak_notice(uid, ending=ending)
                        except Exception:
                            log.exception('Notification failed')
            finally:
                streak_view.reset(token)
                self.queue.task_done()

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
