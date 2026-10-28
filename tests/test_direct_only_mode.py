import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from luckyjet_signal import monitor


class StaleCapture:
    async def snapshot(self):
        return ([{"id": "round-1", "coefficient": 1.23}], "direct:https://example/history")


def test_direct_history_allows_browser_page_to_be_released():
    decision = getattr(monitor, "should_release_browser_page", lambda *_: False)
    assert decision(
        [{"id": "round-1", "coefficient": 1.23}],
        "direct:https://example/history",
    ) is True
    assert decision(
        [{"id": "round-1", "coefficient": 1.23}],
        "response:https://example/history",
    ) is False


def test_refresh_without_browser_requires_fresh_direct_response(monkeypatch):
    async def no_fresh_response(_capture):
        return 0

    async def stale_history_would_look_working(_config, _state, _rounds):
        return True

    monkeypatch.setattr(monitor, "poll_direct_gateways", no_fresh_response)
    monkeypatch.setattr(monitor, "process_network_history", stale_history_would_look_working)

    state = SimpleNamespace(last_round_id=None, previous=[])
    outcome = asyncio.run(
        monitor.refresh_live_history(
            SimpleNamespace(),
            state,
            StaleCapture(),
            None,
        )
    )

    assert outcome["ok"] is False
    assert outcome["worked"] is False
