"""Redis-backed single-owner lease for Telegram Bot API long polling."""
from __future__ import annotations

import asyncio
import logging
import uuid


log = logging.getLogger("luckyjet.telegram_lease")

_REFRESH_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""

_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


class TelegramPollingLease:
    """Ensure only one deployed instance calls getUpdates at a time."""

    def __init__(
        self,
        redis_url,
        *,
        key="luckyjet:telegram_polling_lease",
        ttl_seconds=20,
        retry_seconds=2,
        client=None,
        owner=None,
    ):
        self.redis_url = str(redis_url or "").strip()
        self.key = key
        self.ttl_seconds = max(10, int(ttl_seconds))
        self.retry_seconds = max(0.2, float(retry_seconds))
        self.client = client
        self.owner = owner or uuid.uuid4().hex
        self._held = False
        self._owns_client = client is None
        self._unguarded = False

    async def _ensure_client(self):
        if self.client is not None:
            return True
        if not self.redis_url:
            if not self._unguarded:
                log.warning("REDIS_URL missing: Telegram polling lease disabled")
                self._unguarded = True
            return False

        from redis.asyncio import from_url

        self.client = from_url(self.redis_url, decode_responses=True)
        await self.client.ping()
        return True

    async def acquire(self):
        guarded = await self._ensure_client()
        if not guarded:
            self._held = True
            return True

        acquired = bool(
            await self.client.set(
                self.key,
                self.owner,
                ex=self.ttl_seconds,
                nx=True,
            )
        )
        if acquired:
            self._held = True
            log.info("Telegram polling lease acquired")
        return acquired

    async def wait_for_acquire(self):
        while not await self.acquire():
            log.info("Telegram polling lease busy; waiting for previous instance")
            await asyncio.sleep(self.retry_seconds)

    async def _heartbeat(self):
        if self.client is None or self._unguarded:
            while True:
                await asyncio.sleep(3600)

        interval = max(2.0, self.ttl_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            refreshed = await self.client.eval(
                _REFRESH_SCRIPT,
                1,
                self.key,
                self.owner,
                str(self.ttl_seconds),
            )
            if not refreshed:
                self._held = False
                raise RuntimeError("Telegram polling lease lost")

    async def release(self):
        if not self._held:
            return
        self._held = False
        if self.client is None or self._unguarded:
            return
        try:
            await self.client.eval(
                _RELEASE_SCRIPT,
                1,
                self.key,
                self.owner,
            )
            log.info("Telegram polling lease released")
        except Exception:
            log.exception("Telegram polling lease release failed")

    async def close(self):
        if self._owns_client and self.client is not None:
            await self.client.aclose()
            self.client = None

    async def run(self, awaitable):
        """Run an infinite poller while holding and refreshing the lease."""
        await self.wait_for_acquire()
        worker = asyncio.create_task(awaitable)
        heartbeat = asyncio.create_task(self._heartbeat())
        try:
            done, _ = await asyncio.wait(
                {worker, heartbeat},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat in done:
                error = heartbeat.exception()
                if not worker.done():
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
                if error is not None:
                    raise error
            if worker in done:
                return await worker
            return None
        finally:
            if not heartbeat.done():
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            if not worker.done():
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            await self.release()
            await self.close()
