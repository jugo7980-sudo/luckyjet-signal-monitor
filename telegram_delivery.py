"""Paced Telegram requests and short-lived caches for unchanged panels."""
import asyncio
import copy
import time
import httpx
from contextvars import ContextVar
from collections import OrderedDict

interactive = ContextVar("interactive_delivery", default=False)


class TelegramDelivery:
    def __init__(self, request, *, interval=0.05, cache_seconds=30):
        self.request = request
        self.interval = interval
        self.cache_seconds = cache_seconds
        self._pace = asyncio.Lock()
        self._next = 0
        self._cache = OrderedDict()
        self._urgent = 0
        self._locks = [asyncio.Lock() for _ in range(64)]

    async def __call__(self, method, payload):
        if not (method.startswith('editMessage') or method in {'sendMessage', 'deleteMessage'}):
            return await self.request(method, payload)
        key = (method, payload.get('chat_id'), payload.get('message_id'))
        lock = self._locks[hash(key[1:]) % len(self._locks)]
        async with lock:
            cached = self._cache.get(key)
            if method.startswith('editMessage') and cached and cached[0] == payload and time.monotonic() - cached[1] < self.cache_seconds:
                return {'ok': True, 'result': True}
            for attempt in range(3):
                urgent = interactive.get()
                if urgent:
                    self._urgent += 1
                try:
                    while True:
                        if not urgent and self._urgent:
                            await asyncio.sleep(0.001)
                            continue
                        async with self._pace:
                            delay = self._next - time.monotonic()
                            if delay <= 0:
                                self._next = time.monotonic() + self.interval
                                break
                        await asyncio.sleep(max(0.001, delay))
                finally:
                    if urgent:
                        self._urgent -= 1
                try:
                    result = await self.request(method, payload)
                    break
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 429 or attempt == 2:
                        raise
                    delay = max(0, float(exc.response.json().get('parameters', {}).get('retry_after', 1)))
                    async with self._pace:
                        self._next = max(self._next, time.monotonic() + delay)

            if method.startswith('editMessage') and result.get('ok', True):
                self._cache = OrderedDict((k, v) for k, v in self._cache.items() if k[1:] != key[1:])
                self._cache[key] = (copy.deepcopy(payload), time.monotonic())
            if method == 'deleteMessage':
                self._cache = OrderedDict((k, v) for k, v in self._cache.items() if k[1:] != key[1:])
            while len(self._cache) > 2000:
                self._cache.popitem(last=False)
            return result
