import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from luckyjet_signal.browser_runtime import make_low_memory_async_playwright, LOW_MEMORY_ARGS


class FakeChromium:
    def __init__(self):
        self.kwargs = None

    async def launch(self, **kwargs):
        self.kwargs = kwargs
        return "browser"


class FakePlaywright:
    def __init__(self):
        self.chromium = FakeChromium()


class FakeManager:
    def __init__(self):
        self.p = FakePlaywright()
        self.exited = False

    async def __aenter__(self):
        return self.p

    async def __aexit__(self, *args):
        self.exited = True


def test_low_memory_wrapper_injects_flags_and_preserves_options():
    manager = FakeManager()
    factory = lambda: manager
    wrapped_factory = make_low_memory_async_playwright(factory)

    async def run():
        async with wrapped_factory() as p:
            result = await p.chromium.launch(headless=True, args=["--existing"])
            assert result == "browser"

    asyncio.run(run())

    assert manager.exited is True
    assert manager.p.chromium.kwargs["headless"] is True
    args = manager.p.chromium.kwargs["args"]
    assert "--existing" in args
    for flag in LOW_MEMORY_ARGS:
        assert flag in args
