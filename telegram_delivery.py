"""Paced Telegram requests and short-lived caches for unchanged panels."""
import asyncio
import copy
import time


class TelegramDelivery:
    def __init__(self, request, *, interval=0.05, cache_seconds=30):
        self.request = request
        self.interval = interval
        self.cache_seconds = cache_seconds
        self._pace = asyncio.Lock()
        self._next = 0
        self._cache = {}
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
            async with self._pace:
                await asyncio.sleep(max(0, self._next - time.monotonic()))
                self._next = time.monotonic() + self.interval
            result = await self.request(method, payload)
            if method.startswith('editMessage') and result.get('ok', True):
                self._cache[key] = (copy.deepcopy(payload), time.monotonic())
            if method == 'deleteMessage':
                self._cache = {k: v for k, v in self._cache.items() if k[1:] != key[1:]}
            # Bound cache growth for long-running bots.
            if len(self._cache) > 2000:
                now = time.monotonic()
                self._cache = {k: v for k, v in self._cache.items() if now - v[1] < self.cache_seconds}
            return result
