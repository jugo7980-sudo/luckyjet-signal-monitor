import asyncio

from luckyjet_reliable_gate import ReliableTelegramPasswordGate


class FakeRequest:
    def __init__(self):
        self.calls = []
        self.next_message_id = 700

    async def __call__(self, method, payload):
        self.calls.append((method, payload))
        if method == "sendMessage":
            self.next_message_id += 1
            return {"ok": True, "result": {"message_id": self.next_message_id}}
        return {"ok": True, "result": True}


def message(user_id, text, message_id=1):
    return {
        "message": {
            "message_id": message_id,
            "text": text,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False},
        }
    }


def test_authorized_start_always_shows_visible_main_menu():
    request = FakeRequest()
    gate = ReliableTelegramPasswordGate(
        request,
        history_provider=lambda: ["1.10x", "2.20x"],
        password="122011",
    )
    gate.authorized.add(42)
    gate.chats[42] = 42
    gate.history_messages[42] = 699

    asyncio.run(gate.handle_update(message(42, "/start", 10)))

    sends = [payload for method, payload in request.calls if method == "sendMessage"]
    assert sends, "Authorized /start must visibly respond instead of looking dead"
    assert sends[-1]["text"].startswith("🎰 Lucky Jet Monitor")
    callbacks = {
        button["callback_data"]
        for row in sends[-1]["reply_markup"]["inline_keyboard"]
        for button in row
    }
    assert {"menu_history", "menu_status", "menu_notifications", "menu_settings"} <= callbacks
