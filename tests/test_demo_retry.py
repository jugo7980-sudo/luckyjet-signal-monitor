import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from luckyjet_signal.demo_mode import wrap_click_demo_mode


class Page:
    def __init__(self, results):
        self.url = "https://example.test/casino/play/luckyjet"
        self.results = list(results)
        self.waits = []
        self.gotos = []

    async def evaluate(self, script):
        return self.results.pop(0) if self.results else False

    async def wait_for_timeout(self, ms):
        self.waits.append(ms)

    async def goto(self, url, **kwargs):
        self.gotos.append(url)
        self.url = url


def test_js_click_retries_before_delegate():
    page = Page([False, False, True])
    calls = []

    async def delegate(p):
        calls.append(1)
        return False

    wrapped = wrap_click_demo_mode(
        delegate,
        js_attempts=3,
        retry_ms=10,
    )
    result = asyncio.run(wrapped(page))

    assert result is True
    assert calls == []
    assert len(page.waits) >= 2
