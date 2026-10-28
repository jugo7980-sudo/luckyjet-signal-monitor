import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from luckyjet_reliable_gate import ReliableTelegramPasswordGate


class FakeRequest:
    def __init__(self):
        self.calls = []
        self.next_message_id = 200

    async def __call__(self, method, payload):
        self.calls.append((method, payload))
        if method == "sendMessage":
            self.next_message_id += 1
            return {"ok": True, "result": {"message_id": self.next_message_id}}
        return {"ok": True, "result": True}


def test_restore_preserves_visible_menu_and_active_streak_message():
    request = FakeRequest()
    gate = ReliableTelegramPasswordGate(request)
    gate.authorized.add(42)
    gate.chats[42] = 42
    gate.menu_messages[42] = 55
    gate.active_messages[42] = 77
    gate.current_streak = ["1.10x", "1.20x", "1.30x", "1.40x"]

    asyncio.run(gate.restore_runtime_state())

    deletes = [p for m, p in request.calls if m == "deleteMessage"]
    edits = [p for m, p in request.calls if m == "editMessageReplyMarkup"]
    assert {"chat_id": 42, "message_id": 55} not in deletes
    assert gate.menu_messages[42] == 55
    assert any(p["message_id"] == 77 for p in edits)
    assert gate.active_messages[42] == 77


def test_restore_creates_missing_active_streak_if_threshold_already_met():
    request = FakeRequest()
    gate = ReliableTelegramPasswordGate(request)
    gate.authorized.add(42)
    gate.chats[42] = 42
    gate.current_streak = ["1.10x", "1.20x", "1.30x", "1.40x"]

    asyncio.run(gate.restore_runtime_state())

    streaks = [
        p for m, p in request.calls
        if m == "sendMessage" and p.get("text") == "🎰 Lucky Jet | Серия <2x"
    ]
    assert len(streaks) == 1
    assert gate.active_messages[42] == 201


def test_restore_deletes_stale_active_message_when_current_run_no_longer_qualifies():
    request = FakeRequest()
    gate = ReliableTelegramPasswordGate(request)
    gate.authorized.add(42)
    gate.chats[42] = 42
    gate.active_messages[42] = 77
    gate.current_streak = ["1.10x", "1.20x"]

    asyncio.run(gate.restore_runtime_state())

    deletes = [p for m, p in request.calls if m == "deleteMessage"]
    assert {"chat_id": 42, "message_id": 77} in deletes
    assert 42 not in gate.active_messages


def test_overdue_pending_deletion_is_completed_on_restore():
    request = FakeRequest()
    gate = ReliableTelegramPasswordGate(request)
    gate.authorized.add(42)
    gate.chats[42] = 42
    gate.pending_deletions[42] = (88, 1.0)

    asyncio.run(gate.restore_runtime_state(now=10.0))

    deletes = [p for m, p in request.calls if m == "deleteMessage"]
    assert {"chat_id": 42, "message_id": 88} in deletes
    assert 42 not in gate.pending_deletions
