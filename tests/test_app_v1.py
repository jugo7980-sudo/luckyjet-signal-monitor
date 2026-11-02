import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import app_v1


class Gate:
    def __init__(self):
        self.current_streak = []


def test_seed_private_streak_keeps_only_current_sub_two_run():
    gate = Gate()
    app_v1.seed_private_streak(
        gate,
        ["1.40x", "1.30x", "1.20x", "2.10x", "1.10x"],
    )
    assert gate.current_streak == ["1.20x", "1.30x", "1.40x"]


def test_seed_private_streak_handles_invalid_values():
    gate = Gate()
    app_v1.seed_private_streak(gate, ["1.50x", "bad", "1.40x"])
    assert gate.current_streak == ["1.40x", "1.50x"]


def test_runtime_configuration_installs_fast_direct_poll(monkeypatch):
    calls = []
    monkeypatch.setattr(app_v1, "install_source_guard", lambda module: calls.append("guard"))
    monkeypatch.setattr(app_v1, "install_fast_poll", lambda module: calls.append("fast"))
    monkeypatch.setattr(app_v1, "low_memory_async_playwright", "playwright-wrapper")
    monkeypatch.setattr(app_v1, "wrap_click_demo_mode", lambda fn: "demo-wrapper")

    app_v1.configure_monitor_runtime()

    assert calls == ["guard", "fast"]
    assert app_v1.monitor.async_playwright == "playwright-wrapper"
    assert app_v1.monitor.click_demo_mode == "demo-wrapper"


def test_unchanged_telegram_edit_is_not_logged_as_error(caplog):
    import asyncio
    import httpx
    import pytest

    async def request(method, payload):
        response = httpx.Response(400, json={'description': 'Bad Request: message is not modified'},
                                  request=httpx.Request('POST', 'https://example.test/edit'))
        response.raise_for_status()

    async def run():
        with pytest.raises(httpx.HTTPStatusError):
            await app_v1.telegram_request_with_diagnostics(request, 'editMessageText', {})
    asyncio.run(run())
    assert not any(record.levelno >= 40 for record in caplog.records)
