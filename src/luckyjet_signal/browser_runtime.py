from __future__ import annotations

from playwright.async_api import async_playwright as _async_playwright

LOW_MEMORY_ARGS = (
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-site-isolation-trials",
    "--renderer-process-limit=1",
)


class _ChromiumProxy:
    def __init__(self, chromium):
        self._chromium = chromium

    async def launch(self, **kwargs):
        existing = list(kwargs.pop("args", []) or [])
        merged = existing + [
            flag
            for flag in LOW_MEMORY_ARGS
            if flag not in existing
        ]
        return await self._chromium.launch(
            args=merged,
            **kwargs,
        )


class _PlaywrightProxy:
    def __init__(self, playwright):
        self._playwright = playwright
        self.chromium = _ChromiumProxy(
            playwright.chromium
        )

    def __getattr__(self, name):
        return getattr(self._playwright, name)


class _ManagerProxy:
    def __init__(self, manager):
        self._manager = manager

    async def __aenter__(self):
        playwright = await self._manager.__aenter__()
        return _PlaywrightProxy(playwright)

    async def __aexit__(self, *args):
        return await self._manager.__aexit__(*args)


def make_low_memory_async_playwright(factory):
    def wrapped():
        return _ManagerProxy(factory())

    return wrapped


low_memory_async_playwright = make_low_memory_async_playwright(
    _async_playwright
)
