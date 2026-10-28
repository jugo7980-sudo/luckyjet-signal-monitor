import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from luckyjet_signal.browser_diagnostics import collect_dom_hints


class Page:
    async def evaluate(self, script):
        return {
            "iframes": [
                {
                    "src": "https://provider.test/game?token=secret",
                    "dataSrc": "https://cdn.test/frame?session=abc",
                    "id": "game",
                    "name": "",
                    "title": "Lucky Jet",
                }
            ],
            "demo": [
                {
                    "tag": "A",
                    "text": "Демо-режим",
                    "href": "https://example.test/?auth=secret",
                    "role": "button",
                }
            ],
        }


def test_collect_dom_hints_sanitizes_urls():
    hints = asyncio.run(collect_dom_hints(Page()))

    assert hints["iframes"][0]["src"] == "https://provider.test/game"
    assert hints["iframes"][0]["dataSrc"] == "https://cdn.test/frame"
    assert hints["demo"][0]["href"] == "https://example.test/"
    assert "secret" not in repr(hints)
    assert "abc" not in repr(hints)
