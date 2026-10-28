import asyncio
from types import SimpleNamespace

from luckyjet_password_gate import TelegramPasswordGate


class FakeRequest:
    def __init__(self):
        self.calls = []
        self.next_message_id = 100

    async def __call__(self, method, payload):
        self.calls.append((method, payload))
        if method == "sendMessage":
            self.next_message_id += 1
            return {"ok": True, "result": {"message_id": self.next_message_id}}
        return {"ok": True, "result": True}


def private_message(user_id, text, message_id=1):
    return {
        "message": {
            "message_id": message_id,
            "text": text,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False},
        }
    }


def callback(user_id, data):
    return {
        "id": f"cb-{user_id}-{data}",
        "data": data,
        "from": {"id": user_id},
        "message": {"chat": {"id": user_id, "type": "private"}},
    }


def test_password_unlocks_private_menu():
    request = FakeRequest()
    gate = TelegramPasswordGate(request, history_provider=lambda: ["1.20x", "3.40x"])

    async def run():
        await gate.handle_update(private_message(42, "/start"))
        await gate.handle_update(private_message(42, "122011", 2))

    asyncio.run(run())

    assert 42 in gate.authorized
    assert gate.chats[42] == 42
    last = [payload for method, payload in request.calls if method == "sendMessage"][-1]
    assert last["text"] == "✅ Пароль верный. Доступ открыт."
    buttons = last["reply_markup"]["inline_keyboard"]
    assert any(button["callback_data"] == "menu_settings" for row in buttons for button in row)


def test_user_can_set_threshold_and_delete_timer():
    request = FakeRequest()
    gate = TelegramPasswordGate(request)
    gate.authorized.add(42)
    gate.chats[42] = 42

    async def run():
        await gate.handle_callback(callback(42, "threshold:6"))
        await gate.handle_callback(callback(42, "delete:60"))
        await gate.handle_callback(callback(42, "delete:off"))

    asyncio.run(run())

    assert gate.user_threshold(42) == 6
    assert gate.user_delete_seconds(42) == 0


def test_private_streak_uses_user_threshold_updates_and_finishes():
    request = FakeRequest()
    gate = TelegramPasswordGate(request)
    gate.authorized.add(42)
    gate.chats[42] = 42
    gate.thresholds[42] = 3
    gate.delete_seconds[42] = 15
    scheduled = []

    async def fake_schedule(chat_id, message_id, delay):
        scheduled.append((chat_id, message_id, delay))

    gate._schedule_delete = fake_schedule

    async def run():
        for value in ["1.10x", "1.20x", "1.30x", "1.40x", "2.10x"]:
            await gate.on_round(value)

    asyncio.run(run())

    methods = [method for method, _ in request.calls]
    assert methods.count("sendMessage") == 1
    assert methods.count("editMessageReplyMarkup") == 2
    first = next(payload for method, payload in request.calls if method == "sendMessage")
    assert first["reply_markup"]["inline_keyboard"][0][0]["text"].startswith("Серия 3")
    assert scheduled == [(42, 101, 15)]


def test_notifications_toggle_suppresses_private_streak():
    request = FakeRequest()
    gate = TelegramPasswordGate(request)
    gate.authorized.add(42)
    gate.chats[42] = 42
    gate.notifications_disabled.add(42)

    async def run():
        for value in ["1.10x", "1.20x", "1.30x", "1.40x"]:
            await gate.on_round(value)

    asyncio.run(run())

    assert not any(method == "sendMessage" for method, _ in request.calls)


def test_admin_can_list_and_revoke_authorized_user():
    request = FakeRequest()
    gate = TelegramPasswordGate(request, admin_chat_id="99")
    gate.authorized.update({42, 99})
    gate.chats.update({42: 42, 99: 99})

    async def run():
        await gate.handle_callback(callback(99, "admin_users"))
        await gate.handle_callback(callback(99, "admin_revoke:42"))

    asyncio.run(run())

    assert 42 not in gate.authorized
    assert 42 not in gate.chats
    sent = [payload["text"] for method, payload in request.calls if method == "sendMessage"]
    assert any("42" in text for text in sent)
    assert any("отозван" in text.lower() for text in sent)


def test_status_reports_last_round_and_uptime():
    request = FakeRequest()
    gate = TelegramPasswordGate(request)
    gate.authorized.add(42)
    gate.chats[42] = 42

    async def run():
        await gate.on_round("1.23x")
        await gate.handle_callback(callback(42, "menu_status"))

    asyncio.run(run())

    text = [payload["text"] for method, payload in request.calls if method == "sendMessage"][-1]
    assert "1.23x" in text
    assert "Аптайм" in text
