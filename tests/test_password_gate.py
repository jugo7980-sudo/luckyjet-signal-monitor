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
        if method == "editMessageText" and payload.get("text") == "📊" and self.fail_edit_history_once:
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
    assert sent[-2]["text"] == "📊"
    assert sent[-1]["text"].startswith("☰")
    buttons = sent[-2]["reply_markup"]["inline_keyboard"]
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
    assert "📊 Скрыть историю" in labels
    assert "📡 Статус" not in labels
    assert "📈 Сводка" not in labels
    assert "🧪 Демо: проверка" not in labels
    assert "❓ Справка" not in labels
    assert "⚙️ Настройки" in labels
    assert "✖ Закрыть меню" in labels
    assert 101 not in deleted_ids(request)
    assert 102 in deleted_ids(request)


def test_each_menu_choice_edits_same_message_and_history_stays():
    request = FakeRequest()
    gate = TelegramPasswordGate(request, history_provider=lambda: ["1.10x", "2.20x"])
    gate.authorized.add(42)
    gate.chats[42] = 42
    async def run():
        await gate.ensure_private_history(42)
        await gate.handle_callback(callback(42, "menu_open", 101))
        panel_id = gate.menu_messages[42]
        for action in ("menu_settings", "demo_menu", "demo_history", "menu_main"):
            await gate.handle_callback(callback(42, action, panel_id))
            assert gate.menu_messages[42] == panel_id
        await gate.handle_callback(callback(42, "menu_close", panel_id))
    asyncio.run(run())
    assert len(sent_messages(request)) == 2
    assert 101 not in deleted_ids(request)
    edits = [p for m, p in request.calls if m == "editMessageText" and p["message_id"] == 102]
    assert len(edits) == 4
    assert {p["message_id"] for p in edits} == {102}



def test_settings_are_top_to_bottom_and_keep_navigation():
    gate = TelegramPasswordGate(FakeRequest())
    gate.authorized.add(42)
    markup = gate.settings_menu(42)["inline_keyboard"]
    callbacks = [[button["callback_data"] for button in row] for row in markup]

    assert callbacks[0] == ["settings_notifications", "settings_notifications"]
    assert callbacks[1] == ["settings_threshold", "settings_threshold"]
    assert callbacks[2] == ["settings_history", "settings_history"]
    assert callbacks[3] == ["settings_delete", "settings_delete"]
    assert callbacks[4] == ["settings_sound", "settings_sound"]
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
        await gate.handle_callback(callback(42, "settings_reset_confirm", gate.menu_messages[42]))

    asyncio.run(run())

    assert gate.user_threshold(42) == 4
    assert gate.user_delete_seconds(42) == 5
    assert 42 not in gate.notifications_disabled
    assert 42 not in gate.silent_users
    deleted = deleted_ids(request)
    assert not deleted
    assert gate.menu_messages[42] == 70


def test_private_streak_uses_user_threshold_updates_and_finishes():
    request = FakeRequest()
    gate = TelegramPasswordGate(request)
    gate.authorized.add(42)
    gate.chats[42] = 42
    gate.silent_users.add(42)
    gate.notifications_disabled.add(42)
    async def run():
        await gate.ensure_private_history(42)
        before = len(sent_messages(request))
        for value in ["1.10x", "1.20x", "1.30x", "1.40x"]:
            await gate.on_round(value)
        assert len(gate.current_streak) == 4
        gate.notifications_disabled.add(42)
        await gate.on_round("2.10x")
        assert gate.current_streak == []
        assert len(sent_messages(request)) == before
    asyncio.run(run())
    assert not gate.active_messages


def test_disabling_notifications_does_not_kill_existing_streak_message():
    request = FakeRequest()
    gate = TelegramPasswordGate(request)
    gate.authorized.add(42)
    gate.chats[42] = 42
    gate.silent_users.add(42)
    gate.notifications_disabled.add(42)
    async def run():
        await gate.ensure_private_history(42)
        before = len(sent_messages(request))
        for value in ["1.10x", "1.20x", "1.30x", "1.40x"]:
            await gate.on_round(value)
        assert len(gate.current_streak) == 4
        gate.notifications_disabled.add(42)
        await gate.on_round("2.10x")
        assert gate.current_streak == []
        assert len(sent_messages(request)) == before
    asyncio.run(run())
    assert not gate.active_messages


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

    assert any(p["text"] == "📊" for p in sent_messages(request))
    assert not any(p["text"] == "🎰 Lucky Jet | Серия <2x" for p in sent_messages(request))


def test_silent_setting_marks_new_streak_notification_silent():
    request = FakeRequest()
    gate = TelegramPasswordGate(request)
    gate.authorized.add(42)
    gate.chats[42] = 42
    gate.silent_users.add(42)
    gate.notifications_disabled.add(42)
    async def run():
        await gate.ensure_private_history(42)
        before = len(sent_messages(request))
        for value in ["1.10x", "1.20x", "1.30x", "1.40x"]:
            await gate.on_round(value)
        assert len(gate.current_streak) == 4
        gate.notifications_disabled.add(42)
        await gate.on_round("2.10x")
        assert gate.current_streak == []
        assert len(sent_messages(request)) == before
    asyncio.run(run())
    assert not gate.active_messages


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
    history_sends = [p for p in sent_messages(request) if p["text"] == "📊"]
    assert len(history_sends) == 2


def test_admin_can_list_and_revoke_authorized_user():
    request = FakeRequest()
    gate = TelegramPasswordGate(request, admin_chat_id="99")
    gate.authorized.update({42, 99})
    gate.chats.update({42: 42, 99: 99})

    async def run():
        await gate.handle_callback(callback(99, "admin_users", 80))  # 101
        await gate.handle_callback(callback(99, "admin_revoke:42", 101))
        assert 42 in gate.authorized
        await gate.handle_callback(callback(99, "admin_confirm", gate.menu_messages[99]))

    asyncio.run(run())

    assert 42 not in gate.authorized
    assert 42 not in gate.chats
    sent = [p["text"] for m, p in request.calls if m in {"sendMessage", "editMessageText"}]
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

    text = [p["text"] for m, p in request.calls if m in {"sendMessage", "editMessageText"}][-1]
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
    assert methods == ["sendMessage", "editMessageText"]
    sent = request.calls[0][1]
    assert sent["text"] == "📊"
    rows = sent["reply_markup"]["inline_keyboard"]
    assert rows[0][0]["text"] == "🟦1.10x"
    assert rows[-1][0]["callback_data"] == "menu_open"
    updated = request.calls[1][1]["reply_markup"]["inline_keyboard"]
    assert updated[0][0]["text"] == "🟪3.30x"
    assert updated[-1][0]["callback_data"] == "menu_open"