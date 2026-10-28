import asyncio

import httpx

from luckyjet_password_gate import TelegramPasswordGate


class FakeRequest:
    def __init__(self):
        self.calls = []
        self.next_message_id = 100
        self.fail_edit_history_once = False

    async def __call__(self, method, payload):
        self.calls.append((method, payload))
        if method == "editMessageReplyMarkup" and self.fail_edit_history_once:
            self.fail_edit_history_once = False
            raise httpx.HTTPError("message missing")
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


def sent_messages(request):
    return [payload for method, payload in request.calls if method == "sendMessage"]


def deleted_ids(request):
    return [payload["message_id"] for method, payload in request.calls if method == "deleteMessage"]


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
    sent = sent_messages(request)
    assert sent[-1]["text"] == "🎰 Lucky Jet — история"
    buttons = sent[-1]["reply_markup"]["inline_keyboard"]
    assert buttons[-1][0]["callback_data"] == "menu_open"
    deleted = deleted_ids(request)
    assert 1 in deleted
    assert 2 in deleted
    assert 101 in deleted


def test_main_menu_keeps_existing_buttons_and_can_close():
    request = FakeRequest()
    gate = TelegramPasswordGate(request, history_provider=lambda: ["1.10x"])
    gate.authorized.add(42)
    gate.chats[42] = 42

    async def run():
        await gate.ensure_private_history(42)  # id 101
        await gate.handle_callback(callback(42, "menu_open", 101))  # menu id 102
        await gate.handle_callback(callback(42, "menu_close", 102))

    asyncio.run(run())

    menu = sent_messages(request)[1]["reply_markup"]["inline_keyboard"]
    labels = [button["text"] for row in menu for button in row]
    assert "📊 История" in labels
    assert "📡 Статус" in labels
    assert "🔔 Уведомления" in labels
    assert "⚙️ Настройки" in labels
    assert "✖ Закрыть меню" in labels
    assert 101 not in deleted_ids(request)
    assert 102 in deleted_ids(request)


def test_each_menu_choice_replaces_old_transient_message_and_history_stays():
    request = FakeRequest()
    gate = TelegramPasswordGate(request, history_provider=lambda: ["1.10x", "2.20x"])
    gate.authorized.add(42)
    gate.chats[42] = 42

    async def run():
        await gate.ensure_private_history(42)  # 101
        await gate.handle_callback(callback(42, "menu_open", 101))  # 102
        await gate.handle_callback(callback(42, "menu_status", 102))  # 103
        await gate.handle_callback(callback(42, "menu_settings", 103))  # 104
        await gate.handle_callback(callback(42, "menu_close", 104))

    asyncio.run(run())

    deleted = deleted_ids(request)
    assert 101 not in deleted
    assert 102 in deleted
    assert 103 in deleted
    assert 104 in deleted
    sent = sent_messages(request)
    assert sent[1]["text"].startswith("🎰 Lucky Jet Monitor")
    assert "📡 Статус" in sent[2]["text"]
    assert "⚙️ Настройки" in sent[3]["text"]



def test_settings_are_top_to_bottom_and_keep_navigation():
    gate = TelegramPasswordGate(FakeRequest())
    gate.authorized.add(42)
    markup = gate.settings_menu(42)["inline_keyboard"]
    callbacks = [[button["callback_data"] for button in row] for row in markup]

    assert callbacks[0] == ["settings_notifications"]
    assert callbacks[1] == ["threshold:4", "threshold:5", "threshold:6"]
    assert callbacks[2] == ["threshold:7", "threshold:8", "threshold:9", "threshold:10"]
    assert callbacks[3] == ["delete:15", "delete:30", "delete:60", "delete:off"]
    assert callbacks[4] == ["settings_sound"]
    assert callbacks[5] == ["settings_test"]
    assert callbacks[6] == ["settings_reset"]
    assert callbacks[-1] == ["menu_main", "menu_close"]


def test_user_can_change_and_reset_settings():
    request = FakeRequest()
    gate = TelegramPasswordGate(request)
    gate.authorized.add(42)
    gate.chats[42] = 42

    async def run():
        await gate.handle_callback(callback(42, "threshold:6", 70))  # sends 101
        await gate.handle_callback(callback(42, "delete:60", 101))  # sends 102
        await gate.handle_callback(callback(42, "settings_notifications", 102))  # sends 103
        await gate.handle_callback(callback(42, "settings_sound", 103))  # sends 104
        await gate.handle_callback(callback(42, "settings_reset", 104))

    asyncio.run(run())

    assert gate.user_threshold(42) == 4
    assert gate.user_delete_seconds(42) == 30
    assert 42 not in gate.notifications_disabled
    assert 42 not in gate.silent_users
    deleted = deleted_ids(request)
    assert all(message_id in deleted for message_id in [70, 101, 102, 103, 104])


def test_private_streak_uses_user_threshold_updates_and_finishes():
    request = FakeRequest()
    gate = TelegramPasswordGate(request)
    gate.authorized.add(42)
    gate.chats[42] = 42
    gate.thresholds[42] = 4
    gate.delete_seconds[42] = 15
    scheduled = []

    async def fake_schedule(chat_id, message_id, delay):
        scheduled.append((chat_id, message_id, delay))

    gate._schedule_delete = fake_schedule

    async def run():
        for value in ["1.10x", "1.20x", "1.30x", "1.40x", "2.10x"]:
            await gate.on_round(value)

    asyncio.run(run())

    streak_sends = [p for p in sent_messages(request) if p["text"] == "🎰 Lucky Jet | Серия <2x"]
    assert len(streak_sends) == 1
    assert streak_sends[0]["reply_markup"]["inline_keyboard"][0][0]["text"].startswith("Серия 4")
    assert scheduled == [(42, 102, 15)]


def test_disabling_notifications_does_not_kill_existing_streak_message():
    request = FakeRequest()
    gate = TelegramPasswordGate(request)
    gate.authorized.add(42)
    gate.chats[42] = 42
    gate.thresholds[42] = 4

    async def run():
        for value in ["1.10x", "1.20x", "1.30x", "1.40x"]:
            await gate.on_round(value)
        streak_id = gate.active_messages[42]
        gate.notifications_disabled.add(42)
        await gate.on_round("1.40x")
        await gate.on_round("2.10x")
        return streak_id

    streak_id = asyncio.run(run())
    edits = [p for m, p in request.calls if m == "editMessageReplyMarkup" and p["message_id"] == streak_id]
    assert len(edits) == 2


def test_notifications_disabled_suppresses_new_streak_but_not_history():
    request = FakeRequest()
    gate = TelegramPasswordGate(request)
    gate.authorized.add(42)
    gate.chats[42] = 42
    gate.notifications_disabled.add(42)

    async def run():
        for value in ["1.10x", "1.20x", "1.30x", "1.40x"]:
            await gate.on_round(value)

    asyncio.run(run())

    assert any(p["text"] == "🎰 Lucky Jet — история" for p in sent_messages(request))
    assert not any(p["text"] == "🎰 Lucky Jet | Серия <2x" for p in sent_messages(request))


def test_silent_setting_marks_new_streak_notification_silent():
    request = FakeRequest()
    gate = TelegramPasswordGate(request)
    gate.authorized.add(42)
    gate.chats[42] = 42
    gate.thresholds[42] = 4
    gate.silent_users.add(42)

    async def run():
        for value in ["1.10x", "1.20x", "1.30x", "1.40x"]:
            await gate.on_round(value)

    asyncio.run(run())

    streak = next(p for p in sent_messages(request) if p["text"] == "🎰 Lucky Jet | Серия <2x")
    assert streak["disable_notification"] is True


def test_history_panel_recreates_if_saved_message_was_deleted():
    request = FakeRequest()
    gate = TelegramPasswordGate(request, history_provider=lambda: ["1.10x"])
    gate.authorized.add(42)
    gate.chats[42] = 42

    async def run():
        await gate.ensure_private_history(42)  # 101
        request.fail_edit_history_once = True
        await gate.ensure_private_history(42)  # recreate 102

    asyncio.run(run())

    assert gate.history_messages[42] == 102
    history_sends = [p for p in sent_messages(request) if p["text"] == "🎰 Lucky Jet — история"]
    assert len(history_sends) == 2


def test_admin_can_list_and_revoke_authorized_user():
    request = FakeRequest()
    gate = TelegramPasswordGate(request, admin_chat_id="99")
    gate.authorized.update({42, 99})
    gate.chats.update({42: 42, 99: 99})

    async def run():
        await gate.handle_callback(callback(99, "admin_users", 80))  # 101
        await gate.handle_callback(callback(99, "admin_revoke:42", 101))

    asyncio.run(run())

    assert 42 not in gate.authorized
    assert 42 not in gate.chats
    sent = [payload["text"] for payload in sent_messages(request)]
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

    text = sent_messages(request)[-1]["text"]
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
