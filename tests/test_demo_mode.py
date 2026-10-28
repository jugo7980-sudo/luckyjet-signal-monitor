import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from luckyjet_signal.demo_mode import wrap_click_demo_mode


class Page:
    def __init__(self, *, js_result=True):
        self.url = "https://example.test/casino/play/luckyjet?x=1"
        self.gotos = []
        self.evals = 0
        self.js_result = js_result

    async def wait_for_timeout(self, ms):
        pass

    async def goto(self, url, **kwargs):
        self.gotos.append((url, kwargs))
        self.url = url

    async def evaluate(self, script):
        self.evals += 1
        return self.js_result


def test_reopens_original_game_after_demo_redirect():
    page = Page(js_result=False)

    async def delegate(p):
        p.url = "https://example.test/"
        return True

    wrapped = wrap_click_demo_mode(delegate, js_attempts=1)
    result = asyncio.run(wrapped(page))

    assert result is True
    assert page.gotos
    assert page.gotos[0][0] == "https://example.test/casino/play/luckyjet?x=1"


def test_uses_js_fallback_when_original_click_fails():
    page = Page()

    async def delegate(p):
        return False

    wrapped = wrap_click_demo_mode(delegate)
    result = asyncio.run(wrapped(page))

    assert result is True
    assert page.evals == 1
