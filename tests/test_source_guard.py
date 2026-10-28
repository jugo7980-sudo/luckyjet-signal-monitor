import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from luckyjet_signal import monitor
from luckyjet_source_guard import install_source_guard


def history_payload(items):
    return {
        "history": [
            {"id": rid, "coefficient": coefficient, "status": "finished"}
            for rid, coefficient in items
        ]
    }


def test_recent_direct_history_is_not_replaced_by_websocket_snapshot():
    install_source_guard(monitor, direct_hold_seconds=15)
    capture = monitor.LiveCapture()

    async def run():
        await capture.ingest(
            history_payload([("d3", 1.30), ("d2", 2.20), ("d1", 1.10)]),
            "direct:https://example.test/history",
        )
        await capture.ingest(
            history_payload([("w9", 9.90), ("w8", 8.80), ("w7", 7.70)]),
            "ws:wss://example.test/socket:packet",
        )
        return await capture.snapshot()

    rounds, source = asyncio.run(run())
    assert source.startswith("direct:")
    assert [r["coefficient"] for r in rounds[:3]] == [1.30, 2.20, 1.10]


def test_direct_targets_are_deterministic_and_history_first():
    install_source_guard(monitor, direct_hold_seconds=15)
    capture = monitor.LiveCapture()
    capture._candidate_urls.update(
        {
            "https://example.test/round/current",
            "https://example.test/state",
            "https://example.test/history",
        }
    )

    urls, _ = asyncio.run(capture.direct_targets())
    assert urls[0].endswith("/history")
    assert urls.index("https://example.test/state") < urls.index("https://example.test/round/current")


def test_same_coefficients_with_different_ids_do_not_emit_fake_round(monkeypatch):
    install_source_guard(monitor, direct_hold_seconds=15)
    state = monitor.State(previous=[1.20, 2.30, 1.50], last_round_id="old-3")
    rounds = [
        {"id": "new-3", "coefficient": 1.20},
        {"id": "new-2", "coefficient": 2.30},
        {"id": "new-1", "coefficient": 1.50},
    ]
    sent = []

    async def fake_send(config, message):
        sent.append(message)
        return True

    async def fake_save(config, state):
        return None

    monkeypatch.setattr(monitor, "send", fake_send)
    monkeypatch.setattr(monitor, "save_state", fake_save)

    config = SimpleNamespace(threshold=2.0, trigger_streak=4)
    asyncio.run(monitor.process_network_history(config, state, rounds))

    assert sent == []
    assert state.last_round_id == "new-3"
    assert state.previous == [1.20, 2.30, 1.50]


def test_id_change_with_overlapping_history_emits_only_real_new_round(monkeypatch):
    install_source_guard(monitor, direct_hold_seconds=15)
    state = monitor.State(previous=[1.20, 2.30, 1.50], last_round_id="old-3")
    rounds = [
        {"id": "new-4", "coefficient": 1.70},
        {"id": "new-3", "coefficient": 1.20},
        {"id": "new-2", "coefficient": 2.30},
    ]
    sent = []

    async def fake_send(config, message):
        sent.append(message)
        return True

    async def fake_save(config, state):
        return None

    monkeypatch.setattr(monitor, "send", fake_send)
    monkeypatch.setattr(monitor, "save_state", fake_save)

    config = SimpleNamespace(threshold=2.0, trigger_streak=4)
    asyncio.run(monitor.process_network_history(config, state, rounds))

    last_messages = [m for m in sent if m.startswith("Последний коэффициент:")]
    assert last_messages == ["Последний коэффициент: 1.70x"]
    assert state.last_round_id == "new-4"
