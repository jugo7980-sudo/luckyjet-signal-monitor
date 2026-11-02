"""Coalesce display refreshes while calculations remain ordered in the caller."""
import asyncio
import logging

log = logging.getLogger(__name__)


class LivePanelUpdates:
    def __init__(self, render):
        self.render = render
        self.pending = False
        self.task = None

    def request(self):
        self.pending = True
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run())

    async def _run(self):
        while self.pending:
            self.pending = False
            try:
                await self.render()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('Panel refresh failed; next round will retry')

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
