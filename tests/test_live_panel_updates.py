import asyncio
from live_panel_updates import LivePanelUpdates


def test_slow_display_does_not_block_new_requests_and_coalesces_them():
    async def run():
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []
        async def render():
            calls.append(1)
            started.set()
            await release.wait()
        updates = LivePanelUpdates(render)
        updates.request()
        await started.wait()
        for _ in range(100): updates.request()
        assert len(calls) == 1
        release.set()
        await updates.task
        assert len(calls) == 2
        await updates.close()
    asyncio.run(run())
