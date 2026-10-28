import asyncio

import httpx
import pytest

from luckyjet_password_gate import TelegramPasswordGate
from luckyjet_reliable_gate import ReliableTelegramPasswordGate


class Telegram:
    def __init__(self):
        self.calls = []
        self.next_id = 102
        self.fail_method = None

    async def __call__(self, method, payload):
        self.calls.append((method, payload.copy()))
        await asyncio.sleep(0)
        if method == self.fail_method:
            raise httpx.ConnectError("offline")
        if method == "sendMessage":
            self.next_id += 1
            return {"ok": True, "result": {"message_id": self.next_id}}
        return {"ok": True, "result": True}


def setup(gate_class=ReliableTelegramPasswordGate):
    request = Telegram()
    gate = gate_class(request, password="test", history_provider=lambda: ["1.10x"] * 20)
    gate.authorized.add(42)
    gate.chats[42] = 42
    gate.menu_messages[42] = 102
    callback = {
        "id": "history-click", "data": "menu_history", "from": {"id": 42},
        "message": {"message_id": 102, "chat": {"id": 42, "type": "private"}},
    }
    return gate, request, callback


@pytest.mark.parametrize("gate_class", [TelegramPasswordGate, ReliableTelegramPasswordGate])
def test_history_toggle_on_live_update_off_stays_off_then_on(gate_class):
    gate, request, callback = setup(gate_class)

    async def run():
        await gate.handle_callback(callback)
        first_id = gate.history_messages[42]
        await gate.update_private_histories()
        assert gate.history_messages[42] == first_id
        await gate.handle_callback(callback)
        assert 42 not in gate.history_messages
        before = len(request.calls)
        await gate.on_round("2.20x")
        await gate.ensure_private_history(42)  # restart/start restoration must respect off
        assert len(request.calls) == before
        assert 42 not in gate.history_messages
        await gate.handle_callback(callback)
        assert gate.history_messages[42] != first_id

    asyncio.run(run())
    assert gate.menu_messages[42] == 102
    assert not any(m == "deleteMessage" and p["message_id"] == 102 for m, p in request.calls)
    sends = [p for m, p in request.calls if m == "sendMessage"]
    assert len(sends) == 2
    assert [len(row) for row in sends[0]["reply_markup"]["inline_keyboard"][:4]] == [5, 5, 5, 5]


def test_toggle_and_background_update_are_serialized():
    gate, request, callback = setup()
    gate.history_messages[42] = 101

    async def run():
        await asyncio.gather(gate.handle_callback(callback), gate.update_private_histories())

    asyncio.run(run())
    assert 42 not in gate.history_messages
    assert not any(m == "sendMessage" for m, _ in request.calls)


@pytest.mark.parametrize("method,initial_id", [("sendMessage", None), ("deleteMessage", 101)])
def test_failed_toggle_preserves_state_and_shows_alert_without_chat_clutter(method, initial_id):
    gate, request, callback = setup()
    if initial_id is not None:
        gate.history_messages[42] = initial_id
    request.fail_method = method
    asyncio.run(gate.handle_callback(callback))
    assert gate.history_messages.get(42) == initial_id
    assert gate.menu_messages[42] == 102
    answers = [p for m, p in request.calls if m == "answerCallbackQuery"]
    assert len(answers) == 1
    assert answers[0].get("show_alert") is True


def test_unauthorized_click_does_not_show_history():
    gate, request, callback = setup()
    gate.authorized.clear()
    asyncio.run(gate.handle_callback(callback))
    assert [m for m, _ in request.calls] == ["answerCallbackQuery"]


class MemoryRedis:
    def __init__(self):
        self.sets = {}
        self.hashes = {}
    def pipeline(self):
        return self
    def sadd(self, key, value):
        self.sets.setdefault(key, set()).add(value)
    def srem(self, key, value):
        self.sets.setdefault(key, set()).discard(value)
    def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value
    def hdel(self, key, field):
        self.hashes.setdefault(key, {}).pop(field, None)
    async def execute(self):
        pass
    async def smembers(self, key):
        return self.sets.get(key, set()).copy()
    async def hgetall(self, key):
        return self.hashes.get(key, {}).copy()


def test_hidden_history_and_visible_menu_survive_restart():
    gate, request, callback = setup()
    gate.redis = MemoryRedis()
    gate.history_messages[42] = 101

    async def run():
        await gate.handle_callback(callback)
        restored = ReliableTelegramPasswordGate(request, password="test")
        restored.redis = gate.redis
        await restored._load_persisted_state()
        before = len(request.calls)
        await restored.ensure_private_history(42)
        assert len(request.calls) == before
        assert restored.menu_messages[42] == 102
        assert 42 not in restored.history_messages

    asyncio.run(run())


def test_selected_three_round_threshold_triggers_after_three():
    gate, request, _ = setup()
    gate.thresholds[42] = 3

    async def run():
        for _ in range(2):
            await gate.on_round("1.10x")
        assert not gate.active_messages
        await gate.on_round("1.20x")
        assert 42 in gate.active_messages

    asyncio.run(run())


def test_start_removes_command_and_does_not_reenable_hidden_history():
    gate, request, callback = setup()
    gate.history_messages[42] = 101

    async def run():
        await gate.handle_callback(callback)
        await gate.handle_update({"message": {
            "message_id": 200, "text": "/start",
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False},
        }})
        assert 42 not in gate.history_messages
        assert 42 in gate.menu_messages

    asyncio.run(run())
    assert any(m == "deleteMessage" and p["message_id"] == 200 for m, p in request.calls)
    assert not any(m == "sendMessage" and p.get("text") == "🎰 Lucky Jet — история" for m, p in request.calls)


def test_only_consecutive_low_rounds_trigger_one_live_streak_while_history_hidden():
    gate, request, callback = setup()
    gate.history_messages[42] = 101

    async def run():
        await gate.handle_callback(callback)
        for value in ["1.10x", "1.20x", "2.00x", "1.30x", "1.40x", "1.50x"]:
            await gate.on_round(value)
        assert not gate.active_messages
        await gate.on_round("1.60x")
        message_id = gate.active_messages[42]
        await gate.on_round("1.70x")
        assert gate.active_messages[42] == message_id
        await gate.on_round("2.10x")
        assert not gate.active_messages
        assert 42 not in gate.history_messages
        await gate.close()

    asyncio.run(run())
    sends = [p for m, p in request.calls if m == "sendMessage"]
    assert len(sends) == 1
    assert sends[0]["text"] == "🎰 Lucky Jet | Серия <2x"
    assert sends[0]["reply_markup"]["inline_keyboard"][0][0]["text"].startswith("Серия 4")
