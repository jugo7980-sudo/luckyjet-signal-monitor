import asyncio

from luckyjet_password_gate import TelegramPasswordGate


class FakeRequest:
    def __init__(self):
        self.calls = []
        self.next_message_id = 300

    async def __call__(self, method, payload):
        self.calls.append((method, payload))
        if method == "sendMessage":
            self.next_message_id += 1
            return {"ok": True, "result": {"message_id": self.next_message_id}}
        return {"ok": True, "result": True}


def message(user_id, text, message_id):
    return {
        "message": {
            "message_id": message_id,
            "text": text,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False},
        }
    }


def callback(user_id, data, message_id=400):
    return {
        "id": f"cb-{data}",
        "data": data,
        "from": {"id": user_id},
        "message": {
            "message_id": message_id,
            "chat": {"id": user_id, "type": "private"},
        },
    }


def test_authorized_commands_remove_user_message_and_render_expected_screen():
    request = FakeRequest()
    gate = TelegramPasswordGate(request, history_provider=lambda: ["1.10x"])
    gate.authorized.add(42)
    gate.chats[42] = 42

    async def run():
        await gate.handle_update(message(42, "/menu", 10))
        menu_id = gate.menu_messages[42]
        await gate.handle_update(message(42, "/status", 11))
        status_id = gate.menu_messages[42]
        await gate.handle_update(message(42, "/settings", 12))
        settings_id = gate.menu_messages[42]
        await gate.handle_update(message(42, "/help", 13))
        return menu_id, status_id, settings_id, gate.menu_messages[42]

    menu_id, status_id, settings_id, help_id = asyncio.run(run())

    deleted = [p["message_id"] for m, p in request.calls if m == "deleteMessage"]
    for command_id in (10, 11, 12, 13):
        assert command_id in deleted
    for transient_id in (menu_id, status_id, settings_id):
        assert transient_id in deleted

    texts = [p["text"] for m, p in request.calls if m == "sendMessage"]
    assert any(text.startswith("🎰 Lucky Jet Monitor") for text in texts)
    assert any("📡 Статус" in text for text in texts)
    assert any("⚙️ Настройки" in text for text in texts)
    assert any("ℹ️ Lucky Jet Monitor" in text for text in texts)
    assert help_id > settings_id


def test_test_notification_respects_sound_setting_and_is_scheduled_for_cleanup():
    request = FakeRequest()
    gate = TelegramPasswordGate(request)
    gate.authorized.add(42)
    gate.chats[42] = 42
    gate.silent_users.add(42)
    scheduled = []

    async def fake_delete(chat_id, message_id, delay):
        scheduled.append((chat_id, message_id, delay))

    gate._schedule_delete = fake_delete

    async def run():
        await gate.handle_callback(callback(42, "settings_test", 400))
        await asyncio.sleep(0)

    asyncio.run(run())

    tests = [
        p for m, p in request.calls
        if m == "sendMessage" and p.get("text") == "🧪 Тест уведомления Lucky Jet"
    ]
    assert len(tests) == 1
    assert tests[0]["disable_notification"] is True
    assert scheduled == [(42, 301, 5)]


def test_five_wrong_passwords_lock_input_and_password_messages_are_removed():
    request = FakeRequest()
    gate = TelegramPasswordGate(request, password="122011")

    async def run():
        await gate.handle_update(message(42, "/start", 1))
        for index in range(5):
            await gate.handle_update(message(42, "wrong", 2 + index))

    asyncio.run(run())

    assert gate.blocked_until[42] > 0
    deleted = [p["message_id"] for m, p in request.calls if m == "deleteMessage"]
    for message_id in range(1, 7):
        assert message_id in deleted

    prompt_updates = [p["text"] for m, p in request.calls if m == "editMessageText"]
    assert any("5 неверных попыток" in text for text in prompt_updates)


def test_unauthorized_callback_cannot_open_menu():
    request = FakeRequest()
    gate = TelegramPasswordGate(request)

    asyncio.run(gate.handle_callback(callback(42, "menu_open", 400)))

    sends = [p for m, p in request.calls if m == "sendMessage"]
    assert sends == []


def test_admin_block_and_unblock_flow_changes_access_state():
    request = FakeRequest()
    gate = TelegramPasswordGate(request, admin_chat_id="99")
    gate.authorized.update({42, 99})
    gate.chats.update({42: 42, 99: 99})

    async def run():
        await gate.handle_callback(callback(99, "admin_block:42", 500))
        blocked = 42 in gate.blocked and 42 not in gate.authorized
        menu_id = gate.menu_messages[99]
        await gate.handle_callback(callback(99, "admin_unblock:42", menu_id))
        return blocked

    blocked = asyncio.run(run())

    assert blocked is True
    assert 42 not in gate.blocked
