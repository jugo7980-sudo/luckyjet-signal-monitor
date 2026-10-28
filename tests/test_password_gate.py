import asyncio

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


def callback(user_id, data, message_id=50):
    return {
        "id": f"cb-{user_id}-{data}",
        "data": data,
        "from": {"id": user_id},
        "message": {
            "message_id": message_id,
            "chat": {"id": user_id, "type": "private"},
        },
    }


def test_password_unlocks_into_clean_history_panel():
    request = FakeRequest()
    gate = TelegramPasswordGate(
        request,
        history_provider=lambda: ["1.20x", "3.40x"],
        password="test-secret",
    )

    async def run():
        await gate.handle_update(private_message(42, "/start", 1))
        await gate.handle_update(private_message(42, "test-secret", 2))

    asyncio.run(run())

    assert 42 in gate.authorized
    assert gate.chats[42] == 42
    assert gate.history_messages[42] == 102
    sent = [payload for method, payload in request.calls if method == "sendMessage"]
    assert sent[-1]["text"] == "🎰 Lucky Jet — история"
    buttons = sent[-1]["reply_markup"]["inline_keyboard"]
    assert buttons[-1][0]["callback_data"] == "menu_open"
    deleted = [payload["message_id"] for method, payload in request.calls if method == "deleteMessage"]
    assert 1 in deleted
    assert 2 in deleted
    assert 101 in deleted


def test_menu_is_transient_and_close_removes_it_without_touching_history():
    request = FakeRequest()
    gate = TelegramPasswordGate(request, history_provider=lambda: ["1.10x", "2.20x"])
    gate.authorized.add(42)
    gate.chats[42] = 42

    async def run():
        await gate.ensure_private_history(42)
        await gate.handle_callback(callback(42, "menu_open", 101))
        await gate.handle_callback(callback(42, "menu_status", 102))
        await gate.handle_callback(callback(42, "menu_close", 103))

    asyncio.run(run())

    deleted = [payload["message_id"] for method, payload in request.calls if method == "deleteMessage"]
    assert 101 not in deleted
    assert 102 in deleted
    assert 103 in deleted
    sent = [payload for method, payload in request.calls if method == "sendMessage"]
    assert sent[1]["text"].startswith("🎰 Lucky Jet Monitor")
    assert "📡 Статус" in sent[2]["text"]
    assert any(
        button["callback_data"] == "menu_close"
        for row in sent[2]["reply_markup"]["inline_keyboard"]
        for button in row
    )


def test_user_can_set_threshold_and_delete_timer_with_replaced_menu_messages():
    request = FakeRequest()
    gate = TelegramPasswordGate(request)
    gate.authorized.add(42)
    gate.chats[42] = 42

    async def run():
        await gate.handle_callback(callback(42, "threshold:6", 70))
        await gate.handle_callback(callback(42, "delete:60", 71))
        await gate.handle_callback(callback(42, "delete:off", 72))

    asyncio.run(run())

    assert gate.user_threshold(42) == 6
    assert gate.user_delete_seconds(42) == 0
    deleted = [payload["message_id"] for method, payload in request.calls if method == "deleteMessage"]
    assert deleted == [70, 71, 72]


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
        await gate.handle_callback(callback(99, "admin_users", 80))
        await gate.handle_callback(callback(99, "admin_revoke:42", 81))

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
        await gate.handle_callback(callback(42, "menu_status", 90))

    asyncio.run(run())

    text = [payload["text"] for method, payload in request.calls if method == "sendMessage"][-1]
    assert "1.23x" in text
    assert "Аптайм" in text


def test_authorized_user_gets_live_history_panel_and_updates_same_message():
    request = FakeRequest()
    history = ["1.10x", "2.20x"]
    gate = TelegramPasswordGate(request, history_provider=lambda: history)
    gate.authorized.add(42)
    gate.chats[42] = 42

    async def run():
        await gate.ensure_private_history(42)
        history.insert(0, "3.30x")
        await gate.update_private_histories()

    asyncio.run(run())

    assert gate.history_messages[42] == 101
    methods = [method for method, _ in request.calls]
    assert methods == ["sendMessage", "editMessageReplyMarkup"]
    sent = request.calls[0][1]
    assert sent["text"] == "🎰 Lucky Jet — история"
    rows = sent["reply_markup"]["inline_keyboard"]
    assert rows[0][0]["text"] == "🟦1.10x"
    assert rows[-1][0]["callback_data"] == "menu_open"
    updated = request.calls[1][1]["reply_markup"]["inline_keyboard"]
    assert updated[0][0]["text"] == "🟪3.30x"
    assert updated[-1][0]["callback_data"] == "menu_open"
