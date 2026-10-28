import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from luckyjet_signal.network_diagnostics import wrap_attach_network_capture


class Request:
    def __init__(self, resource_type="xhr"):
        self.resource_type = resource_type


class Response:
    def __init__(self, url, status=200, resource_type="xhr"):
        self.url = url
        self.status = status
        self.request = Request(resource_type)


class Page:
    def __init__(self):
        self.handlers = {}

    def on(self, name, callback):
        self.handlers[name] = callback


def test_logs_sanitized_xhr_response_and_preserves_delegate(caplog):
    calls = []

    def delegate(page, capture):
        calls.append((page, capture))

    wrapped = wrap_attach_network_capture(delegate, max_events=2)
    page = Page()
    capture = object()
    caplog.set_level(logging.INFO, logger="luckyjet.netdiag")

    wrapped(page, capture)
    page.handlers["response"](
        Response("https://example.test/api/casino/launch?token=secret", 403)
    )

    messages = [r.message for r in caplog.records if "Network response" in r.message]
    assert calls == [(page, capture)]
    assert len(messages) == 1
    assert "403" in messages[0]
    assert "https://example.test/api/casino/launch" in messages[0]
    assert "secret" not in messages[0]
