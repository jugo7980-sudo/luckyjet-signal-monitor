"""Paced Telegram requests and short-lived caches for unchanged panels."""
import asyncio
import copy
import time
import httpx
from contextvars import ContextVar
from collections import OrderedDict

interactive = ContextVar("interactive_delivery", default=False)
notification = ContextVar("notification_delivery", default=False)


def color_buttons(payload):
    markup = payload.get('reply_markup')
    if not isinstance(markup, dict) or 'inline_keyboard' not in markup:
        return payload
    result = dict(payload)
    result['reply_markup'] = copy.deepcopy(markup)
    for row in result['reply_markup']['inline_keyboard']:
        for button in row:
            action = button.get('callback_data', '').split(':', 1)[0]
            style = {'menu_main': 'primary', 'site_login': 'success',
                     'demo_reset_confirm': 'danger', 'settings_reset_confirm': 'danger',
                     'site_forget_confirm': 'danger', 'admin_confirm': 'danger'}.get(action)
            if style:
                button.setdefault('style', style)
    return result


class TelegramDelivery:
    def __init__(self, request, *, interval=0.05, cache_seconds=30):
        self.request = request
        self.interval = interval
        self.cache_seconds = cache_seconds
        self._pace = asyncio.Lock()
        self._next = 0
        self._cache = OrderedDict()
        self._waiting = [0, 0, 0]
        self._locks = [asyncio.Lock() for _ in range(64)]

    async def __call__(self, method, payload):
        payload = color_buttons(payload)
        if not (method.startswith('editMessage') or method in {'sendMessage', 'deleteMessage'}):
            return await self.request(method, payload)
        key = (payload.get('chat_id'), payload.get('message_id'))
        lock = self._locks[hash(key) % len(self._locks)]
        async with lock:
            cached = self._cache.get(key)
            if method.startswith('editMessage') and cached and cached[0] == method and cached[1] == payload and time.monotonic() - cached[2] < self.cache_seconds:
                return {'ok': True, 'result': True}
            for attempt in range(3):
                priority = 2 if notification.get() else 1 if interactive.get() else 0
                self._waiting[priority] += 1
                try:
                    while True:
                        if any(self._waiting[priority + 1:]):
                            await asyncio.sleep(0.001)
                            continue
                        async with self._pace:
                            delay = self._next - time.monotonic()
                            if delay <= 0:
                                self._next = time.monotonic() + self.interval
                                break
                        await asyncio.sleep(max(0.001, delay))
                finally:
                    self._waiting[priority] -= 1
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
                self._cache.pop(key, None)
                self._cache[key] = (method, copy.deepcopy(payload), time.monotonic())
            if method == 'deleteMessage':
                self._cache.pop(key, None)
            while len(self._cache) > 2000:
                self._cache.popitem(last=False)
            return result
