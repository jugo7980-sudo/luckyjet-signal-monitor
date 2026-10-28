"""Share a source refresh between automatic polling and button presses."""
import asyncio
import logging

log = logging.getLogger("luckyjet.refresh")


class HistoryRefresh:
    def __init__(self):
        self._fetch = None
        self._task = None

    def bind(self, fetch):
        self._fetch = fetch

    async def request(self):
        if self._task is not None and not self._task.done():
            return await asyncio.shield(self._task)
        if self._fetch is None:
            return {"ok": False, "changed": False, "worked": False}
        self._task = asyncio.create_task(self._run(self._fetch))
        return await asyncio.shield(self._task)

    async def _run(self, fetch):
        try:
            return await fetch()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("History refresh failed: %s", type(exc).__name__)
            return {"ok": False, "changed": False, "worked": False}

    async def close(self):
        self._fetch = None
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
