import asyncio

from luckyjet_signal.demo_ledger import MINIMUM_STAKE_CENTS
from test_intuitive_ui import setup, click, screen


def normal_entry(index=0, *, outcome="recovered", net_cents=1000):
    return {
        "started_at": "2026-11-03T00:00:00+00:00",
        "ended_at": "2026-11-03T00:01:30+00:00",
        "rounds": [
            {"stake_cents": 1000, "coefficient": "1.50", "payout_cents": 0, "balance_after_cents": 999000},
            {"stake_cents": 2000, "coefficient": "2.75", "payout_cents": 4000, "balance_after_cents": 1001000},
        ],
        "base_stake_cents": 1000,
        "balance_before_cents": 1_000_000,
        "balance_after_cents": 1_001_000,
        "total_stake_cents": 3000,
        "payout_cents": 4000,
        "net_cents": net_cents,
        "outcome": outcome,
        "coefficient": "2.75",
        "marker": index,
    }


def incomplete_entry():
    entry = normal_entry(outcome="incomplete_data", net_cents=None)
    entry["payout_cents"] = None
    entry["balance_after_cents"] = 999000
    return entry


def all_buttons(markup, prefix):
    return [button for row in markup["inline_keyboard"] for button in row
            if button.get("callback_data", "").startswith(prefix)]


def test_one_hundred_sessions_render_ten_pages_and_callback_index_99_is_valid():
    gate, request = setup()
    ledger = gate._demo_ledger(42)
    ledger.history = [normal_entry(i) for i in range(100)]

    text, markup = gate._demo_history_list(42)
    token = all_buttons(markup, "demo_cycle:")[0]["callback_data"].split(":")[1]

    assert "100 циклов · 1/10" in text
    assert len(all_buttons(markup, "demo_cycle:")) == 10
    assert gate._supported_callback(f"demo_cycle:{token}:99", 42)
    last_text, last_markup = gate._demo_history_list(42, 9, token)
    assert "10/10" in last_text
    assert len(all_buttons(last_markup, "demo_cycle:")) == 10


def test_incomplete_session_never_fabricates_money_and_detail_shows_reason_and_duration():
    gate, _ = setup()
    gate._demo_ledger(42).history = [incomplete_entry()]

    text, markup = gate._demo_history_list(42)
    button = all_buttons(markup, "demo_cycle:")[0]
    token = button["callback_data"].split(":")[1]
    detail, _ = gate._demo_cycle_screen(42, token, 0)

    assert "⚠️ данные" in button["text"]
    assert "$0.00" not in button["text"]
    assert "причина завершения: неполные данные" in detail.lower()
    assert "Длительность: 1 мин 30 сек" in detail
    assert "Чистый результат:" not in detail
    assert "2.75x" in detail


def test_active_demo_cannot_be_disabled_or_have_trigger_changed():
    gate, request = setup()
    ledger = gate._demo_ledger(42)
    ledger.enabled = True
    ledger.active = True
    ledger.trigger_streak = 4

    async def run():
        await click(gate, "demo_toggle")
        assert ledger.enabled is True and ledger.active is True
        assert "заверш" in screen(request)["text"].lower()
        await click(gate, "demo_trigger:3")
        assert ledger.trigger_streak == 4
        assert "заверш" in screen(request)["text"].lower()
    asyncio.run(run())


def test_insufficient_balance_emits_one_closable_ten_minute_warning_and_resets_start_stake():
    gate, request = setup()
    ledger = gate._demo_ledger(42)
    ledger.enabled = True
    ledger.balance_cents = 3_000
    ledger.base_stake_cents = 1_000
    ledger.next_stake_cents = 1_000
    ledger.trigger_streak = 2
    scheduled = []

    async def fake_schedule(chat_id, message_id, delay):
        scheduled.append((chat_id, message_id, delay))
    gate._schedule_delete = fake_schedule

    async def run():
        await gate._handle_demo_round(42, "1.10")
        await gate._handle_demo_round(42, "1.20")  # trigger only
        await gate._handle_demo_round(42, "1.30")  # $10 loss
        await gate._handle_demo_round(42, "1.40")  # $20 loss -> cannot fund $40
        await asyncio.sleep(0)
    asyncio.run(run())

    warnings = [payload for method, payload in request.calls
                if method == "sendMessage" and "недостаточно баланса" in payload.get("text", "").lower()]
    assert len(warnings) == 1
    assert "Шагов: 2" in warnings[0]["text"]
    assert "Новая ставка: $10.00" in warnings[0]["text"]
    assert warnings[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "demo_warning_close"
    assert scheduled and scheduled[-1][2] == 600
    assert ledger.base_stake_cents == MINIMUM_STAKE_CENTS


def test_history_gap_closes_only_active_demo_as_incomplete_and_warns():
    gate, request = setup()
    ledger = gate._demo_ledger(42)
    ledger.enabled = True
    ledger.trigger_streak = 2
    ledger.process_round("1.10")
    ledger.process_round("1.20")
    ledger.process_round("1.30")
    confirmed_balance = ledger.balance_cents

    async def no_delete(chat_id, message_id, delay):
        return None
    gate._schedule_delete = no_delete

    asyncio.run(gate.on_history_gap())

    assert ledger.active is False
    assert ledger.history[0]["outcome"] == "incomplete_data"
    assert ledger.history[0]["net_cents"] is None
    assert ledger.balance_cents == confirmed_balance
    assert ledger.base_stake_cents == MINIMUM_STAKE_CENTS
    assert any(method == "sendMessage" and "не удалось восстановить" in payload.get("text", "").lower()
               for method, payload in request.calls)


def test_demo_reset_prompt_preserves_history_and_active_reset_records_user_stop():
    gate, request = setup()
    ledger = gate._demo_ledger(42)
    ledger.history = [normal_entry(1)]
    ledger.enabled = True
    ledger.trigger_streak = 2
    ledger.process_round("1.10")
    ledger.process_round("1.20")
    ledger.process_round("1.30")

    async def run():
        await click(gate, "demo_reset")
        prompt = screen(request)["text"].lower()
        assert "история" in prompt and "не удал" in prompt
        assert "$10.00" in prompt
        await click(gate, "demo_reset_confirm")
    asyncio.run(run())

    assert ledger.balance_cents == 1_000_000
    assert ledger.base_stake_cents == 1_000
    assert len(ledger.history) == 2
    assert ledger.history[0]["outcome"] == "stopped_by_user"
    assert ledger.history[1]["marker"] == 1


def test_demo_statistics_derive_from_history_and_exclude_technical_sessions_from_money():
    gate, _ = setup()
    recovered = normal_entry(outcome="recovered", net_cents=1000)
    loss = normal_entry(outcome="insufficient_balance", net_cents=-3000)
    incomplete = incomplete_entry()
    stopped = normal_entry(outcome="stopped_by_user", net_cents=-1000)
    gate._demo_ledger(42).history = [recovered, loss, incomplete, stopped]

    text = gate._demo_stats_text(42)

    assert "Всего сессий: 4" in text
    assert "Успешных: 1" in text
    assert "Проигрышных: 1" in text
    assert "Неполных: 1" in text
    assert "Остановлено вручную: 1" in text
    assert "Общий результат: -$20.00" in text
    assert "Среднее шагов: 2.0" in text


def test_demo_history_is_persistent_but_leaving_it_restores_sixty_second_menu_timeout(monkeypatch):
    gate, _ = setup()
    now = [1000.0]
    monkeypatch.setattr("luckyjet_password_gate.time.time", lambda: now[0])
    gate._demo_ledger(42).history = [normal_entry()]

    async def run():
        await click(gate, "demo_history")
        assert 42 not in gate.menu_deadlines
        await click(gate, "demo_menu")
        assert gate.menu_deadlines[42] == 1060
        await gate.close()
    asyncio.run(run())


def test_main_balance_button_puts_refresh_before_money_icon():
    gate, _ = setup()
    labels = [button["text"] for row in gate.menu(42)["inline_keyboard"] for button in row]
    balance = next(label for label in labels if "Баланс:" in label)
    assert balance.startswith("🔄 💵 Баланс:")
