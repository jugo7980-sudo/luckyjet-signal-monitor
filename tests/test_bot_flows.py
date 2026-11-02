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


def test_history_size_setting_updates_panel_and_is_shown_in_settings():
    values = [f"{1 + index / 10:.2f}x" for index in range(100)]
    request = FakeRequest()
    gate = TelegramPasswordGate(request, history_provider=lambda: values)
    gate.authorized.add(42)
    gate.chats[42] = 42

    asyncio.run(gate.handle_callback(callback(42, "history_size:50", 500)))

    assert gate.user_history_size(42) == 50
    assert len([button for row in gate._history_markup(42)["inline_keyboard"][:-1] for button in row]) == 50
    settings_text = [
        payload["text"] for method, payload in request.calls
        if method == "sendMessage" and payload.get("text", "").startswith("⚙️ Настройки")
    ]
    assert settings_text and "История: 50 раундов" in settings_text[-1]


def test_summary_and_status_show_recent_data_quality():
    import time
    request = FakeRequest()
    gate = TelegramPasswordGate(request, history_provider=lambda: ["1.50x", "2.00x", "4.00x"])
    gate.last_round = "1.50x"
    gate.last_round_at = time.time()
    gate.current_streak = ["1.50x"]

    summary = gate._summary_text(42)
    status = gate._status_text(42)

    assert "Средний коэффициент: 2.50x" in summary
    assert "Максимальный: 4.00x" in summary
    assert "Ниже 2x: 1" in summary
    assert "Статус мониторинга: раунд получен недавно" in status
    assert "История: 3 раундов" in status


def test_demo_buttons_toggle_select_trigger_and_update_usd_balance():
    request = FakeRequest()
    gate = TelegramPasswordGate(request)
    gate.authorized.add(42)
    gate.chats[42] = 42

    async def run():
        await gate.handle_callback(callback(42, "demo_menu", 500))
        menu_id = gate.menu_messages[42]
        await gate.handle_callback(callback(42, "demo_toggle", menu_id))
        menu_id = gate.menu_messages[42]
        await gate.handle_callback(callback(42, "demo_trigger:3", menu_id))
        menu_id = gate.menu_messages[42]
        await gate.handle_callback(callback(42, "demo_set_balance", menu_id))
        await gate.handle_update(message(42, "9500.50", 77))

    asyncio.run(run())

    ledger = gate.demo_ledgers[42]
    assert ledger.enabled is True
    assert ledger.trigger_streak == 3
    assert ledger.balance_cents == 950_050
    assert 77 in [p["message_id"] for m, p in request.calls if m == "deleteMessage"]
    answers = [p for method, p in request.calls if method == "answerCallbackQuery"]
    assert [p["callback_query_id"] for p in answers] == [
        "cb-demo_menu", "cb-demo_toggle", "cb-demo_trigger:3", "cb-demo_set_balance"
    ]
    latest_screen = [
        p for method, p in request.calls
        if method == "sendMessage" and p.get("reply_markup", {}).get("inline_keyboard")
    ][-1]
    assert "Виртуальный баланс установлен: $9,500.50" in latest_screen["text"]
    labels = [
        button["text"]
        for row in latest_screen["reply_markup"]["inline_keyboard"]
        for button in row
    ]
    assert "💵 Баланс: $9,500.50" in labels


def test_demo_stake_input_minimum_and_reset_confirmation_buttons():
    request = FakeRequest()
    gate = TelegramPasswordGate(request)
    gate.authorized.add(42)
    gate.chats[42] = 42

    async def run():
        await gate.handle_callback(callback(42, "demo_menu", 600))
        await gate.handle_callback(callback(42, "demo_set_stake", gate.menu_messages[42]))
        await gate.handle_update(message(42, "9", 78))
        assert gate.demo_ledgers[42].base_stake_cents == 15_000
        invalid_prompt = [
            p for method, p in request.calls if method == "sendMessage"
        ][-1]
        assert invalid_prompt["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "demo_input_cancel"
        await gate.handle_update(message(42, "10", 79))
        assert gate.demo_ledgers[42].base_stake_cents == 1_000
        await gate.handle_callback(callback(42, "demo_toggle", gate.menu_messages[42]))
        await gate.handle_callback(callback(42, "demo_reset", gate.menu_messages[42]))
        confirm_id = gate.menu_messages[42]
        await gate.handle_callback(callback(42, "demo_reset_confirm", confirm_id))

    asyncio.run(run())

    ledger = gate.demo_ledgers[42]
    assert ledger.balance_cents == 1_000_000
    assert ledger.base_stake_cents == 1_000
    assert ledger.enabled is True
    assert ledger.history == []
    assert all(
        message_id in [p["message_id"] for m, p in request.calls if m == "deleteMessage"]
        for message_id in (78, 79)
    )


def test_demo_history_button_renders_recovered_initial_stake_and_cycle_result():
    request = FakeRequest()
    gate = TelegramPasswordGate(request)
    gate.authorized.add(42)
    gate.chats[42] = 42
    ledger = gate._demo_ledger(42)
    ledger.enabled = True
    for _ in range(4):
        ledger.process_round("1.50")
    ledger.process_round("1.50")
    ledger.process_round("2.00")

    asyncio.run(gate.handle_callback(callback(42, "demo_history", 700)))

    screen = [
        p for method, p in request.calls
        if method == "sendMessage" and p.get("text", "").startswith("📜")
    ][-1]
    assert "Возвращено изначальных $150.00" in screen["text"]
    assert "итог +$150.00" in screen["text"]
    assert "Баланс: $10,000.00 → $10,150.00" in screen["text"]


def test_demo_toggle_arms_when_existing_history_already_meets_selected_trigger():
    request = FakeRequest()
    gate = TelegramPasswordGate(
        request,
        history_provider=lambda: ["1.50x", "1.30x", "1.10x", "2.00x"],
    )
    gate.authorized.add(42)
    gate.chats[42] = 42
    ledger = gate._demo_ledger(42)
    ledger.set_trigger(3)

    asyncio.run(gate.handle_callback(callback(42, "demo_toggle", 800)))

    assert ledger.enabled is True
    assert ledger.active is True
    assert ledger.next_stake_cents == 15_000


def test_invalid_stale_demo_button_is_acknowledged_without_deleting_menu():
    request = FakeRequest()
    gate = TelegramPasswordGate(request)
    gate.authorized.add(42)
    gate.chats[42] = 42
    gate.menu_messages[42] = 801

    asyncio.run(gate.handle_callback(callback(42, "demo_trigger:7", 801)))

    assert gate.menu_messages[42] == 801
    assert 801 not in [p["message_id"] for m, p in request.calls if m == "deleteMessage"]
    assert any(m == "answerCallbackQuery" for m, p in request.calls)


def test_demo_ledger_is_connected_to_live_round_handler_and_reports_recovery():
    request = FakeRequest()
    gate = TelegramPasswordGate(request, history_provider=lambda: [])
    gate.authorized.add(42)
    gate.chats[42] = 42
    ledger = gate._demo_ledger(42)
    ledger.enabled = True

    async def run():
        for value in ("1.10x", "1.20x", "1.30x", "1.40x", "1.50x", "1.20x", "2.00x"):
            await gate.on_round(value)
        await gate.close()

    asyncio.run(run())

    texts = [p["text"] for method, p in request.calls if method == "sendMessage"]
    assert any("условие выполнено" in text for text in texts)
    assert any("виртуальная ставка $150.00 потеряна" in text for text in texts)
    assert any("Исходная ставка $150.00 возвращена" in text for text in texts)
    assert any("итог цикла: +$150.00" in text for text in texts)
    assert ledger.balance_cents == 1_015_000
    assert ledger.history[0]["balance_before_cents"] == 1_000_000


def test_six_round_trigger_and_live_balance_update_stop_after_leaving_demo():
    request = FakeRequest()
    gate = TelegramPasswordGate(request, history_provider=lambda: [])
    gate.authorized.add(42)
    gate.chats[42] = 42

    async def run():
        await gate.handle_callback(callback(42, "demo_trigger:6", 900))
        await gate.handle_callback(callback(42, "demo_toggle", gate.menu_messages[42]))
        panel_id = gate.menu_messages[42]
        for _ in range(6):
            await gate.on_round("1.20x")
        assert gate.demo_ledgers[42].active
        await gate.on_round("1.50x")
        edits = [p for m, p in request.calls if m == "editMessageText" and p["message_id"] == panel_id]
        assert "$9,850.00" in edits[-1]["text"]
        assert "$300.00" in edits[-1]["text"]
        await gate.handle_callback(callback(42, "menu_main", panel_id))
        count = len([1 for m, p in request.calls if m == "editMessageText" and p["message_id"] == panel_id])
        await gate.on_round("2.00x")
        assert count == len([1 for m, p in request.calls if m == "editMessageText" and p["message_id"] == panel_id])
        await gate.close()

    asyncio.run(run())
    assert gate.demo_ledgers[42].trigger_streak == 6
