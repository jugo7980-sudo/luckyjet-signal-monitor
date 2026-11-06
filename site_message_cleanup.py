"""Retry account-dialog cleanup; persist only message IDs and deadlines."""
import asyncio
import logging
import time

import httpx

log = logging.getLogger(__name__)


class SiteMessageCleanup:
    def __init__(self, account):
        self.account = account
        self.pending = {}
        self.task = None
        self.lock = asyncio.Lock()

    @property
    def redis(self):
        return getattr(self.account.gate, 'redis', None)

    @property
    def key(self):
        return f'luckyjet:site_cleanup:{self.account.owner_id}'

    async def restore(self):
        if self.redis is not None:
            self.pending.update({int(mid): float(deadline) for mid, deadline
                                 in (await self.redis.hgetall(self.key)).items()})
        if self.pending:
            self.start()

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())

    async def add(self, message_id, delay=0):
        if message_id is None:
            return
        async with self.lock:
            message_id = int(message_id)
            deadline = time.time() + delay
            self.pending[message_id] = deadline
            if self.redis is not None:
                try:
                    await self.redis.hset(self.key, str(message_id), str(deadline))
                except Exception:
                    log.warning('Account cleanup persistence unavailable')
        self.start()
        if not delay:
            await self.flush()

    async def flush(self, *, force=False):
        async with self.lock:
            for mid, deadline in list(self.pending.items()):
                if not force and deadline > time.time():
                    continue
                if self.redis is not None:
                    # Retry persistence as well when Redis was temporarily unavailable.
                    try:
                        await self.redis.hset(self.key, str(mid), str(deadline))
                    except Exception:
                        log.warning('Account cleanup persistence unavailable')
                try:
                    await self.account.request('deleteMessage', {
                        'chat_id': self.account.owner_id, 'message_id': mid})
                except httpx.HTTPStatusError as exc:
                    try:
                        reason = exc.response.json().get('description', '').lower()
                    except ValueError:
                        reason = ''
                    terminal = exc.response.status_code == 403 or (
                        exc.response.status_code == 400 and any(s in reason for s in (
                            'message to delete not found', 'message_id_invalid', "message can't be deleted")))
                    if not terminal:
                        continue
                    log.warning('Account message cleanup unavailable: message_id=%s status=%s', mid, exc.response.status_code)
                except Exception:
                    # Never log exception payloads from a credential dialog.
                    continue
                if self.redis is not None:
                    try:
                        await self.redis.hdel(self.key, str(mid))
                    except Exception:
                        continue
                self.pending.pop(mid, None)

    async def run(self):
        while self.pending:
            await asyncio.sleep(5)
            await self.flush()

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self.flush(force=True)
