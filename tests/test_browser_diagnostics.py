import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from luckyjet_signal.browser_diagnostics import summarize_targets, wrap_diagnose_page


class Target:
    def __init__(self, url):
        self.url = url


class Context:
    def __init__(self, pages):
        self.pages = pages


class Page:
    def __init__(self):
        self.context = Context([
            Target("https://example.com/a?token=secret#frag"),
            Target("https://provider.test/game?session=abc"),
        ])
        self.frames = [
            Target("https://example.com/a?x=1"),
            Target("about:blank"),
        ]


def test_summarize_targets_strips_query_and_fragment():
    summary = summarize_targets(
        ["https://example.com/a?token=secret#frag", "about:blank"],
        ["https://provider.test/game?session=abc"],
    )

    assert summary["pages"] == ["https://example.com/a", "about:blank"]
    assert summary["frames"] == ["https://provider.test/game"]
    assert "secret" not in repr(summary)
    assert "abc" not in repr(summary)


def test_wrap_diagnose_page_logs_targets_and_delegates(caplog):
    calls = []

    async def delegate(page, capture):
        calls.append((page, capture))
        return "ok"

    wrapped = wrap_diagnose_page(delegate)
    caplog.set_level(logging.INFO, logger="luckyjet.browserdiag")
    page = Page()
    capture = object()
    result = asyncio.run(wrapped(page, capture))

    assert result == "ok"
    assert calls == [(page, capture)]
    messages = [r.message for r in caplog.records if "Browser targets" in r.message]
    assert len(messages) == 1
    assert "provider.test/game" in messages[0]
    assert "secret" not in messages[0]
