import asyncio
import copy
import re

from luckyjet_reliable_gate import ReliableTelegramPasswordGate


class Telegram:
    def __init__(self):
        self.calls = []
        self.next_id = 100

    async def __call__(self, method, payload):
        self.calls.append((method, payload))
        if method == "sendMessage":
            self.next_id += 1
            return {"result": {"message_id": self.next_id}}
        return {"result": True}


def setup():
    request = Telegram()
    gate = ReliableTelegramPasswordGate(request, admin_chat_id=99)
    gate.authorized.update({42, 99})
    gate.chats.update({42: 42, 99: 99})
    gate.history_messages[42] = 10
    return gate, request


async def click(gate, action, user=42):
    await gate.handle_callback({"id": action, "data": action, "from": {"id": user},
                                "message": {"message_id": gate.menu_messages.get(user, 1), "chat": {"id": user}}})


def screen(request):
    return [p for m, p in request.calls if m in {"sendMessage", "editMessageText"}][-1]


def test_settings_selection_is_separate_from_demo_and_navigation_is_static():
    gate, request = setup()
    async def run():
        await click(gate, "menu_settings")
        menu_id = gate.menu_messages[42]
        await click(gate, "settings_threshold")
        assert "Уведомлять" in screen(request)["text"]
        assert "ниже 2x" in screen(request)["text"]
        await click(gate, "threshold:6")
        assert gate.user_threshold(42) == 6
        assert gate._demo_ledger(42).trigger_streak == 4
        await click(gate, "settings_history")
        await click(gate, "history_size:50")
        await click(gate, "settings_delete")
        assert "после конца серии" in screen(request)["text"]
        await click(gate, "delete:5")
        await click(gate, "demo_trigger_menu")
        await click(gate, "demo_trigger:3")
        assert gate._demo_ledger(42).trigger_streak == 3
        assert gate.user_threshold(42) == 6
        assert gate.menu_messages[42] == menu_id
        assert not any(m == "sendMessage" for m, _ in request.calls)
    asyncio.run(run())


def test_resets_require_fresh_confirmation_and_cancel_preserves_data():
    gate, request = setup()
    gate.thresholds[42] = 6
    ledger = gate._demo_ledger(42)
    ledger.balance_cents = 123_400
    async def run():
        await click(gate, "settings_reset")
        assert gate.user_threshold(42) == 6
        await click(gate, "menu_settings")
        await click(gate, "settings_reset_confirm")
        assert gate.user_threshold(42) == 6
        await click(gate, "settings_reset")
        await click(gate, "settings_reset_confirm")
        assert gate.user_threshold(42) == 4
        assert ledger.balance_cents == 123_400
        await click(gate, "demo_reset")
        await click(gate, "menu_main")
        await click(gate, "demo_reset_confirm")
        assert ledger.balance_cents == 123_400
        await click(gate, "demo_reset")
        await click(gate, "demo_reset_confirm")
        assert ledger.balance_cents == 1_000_000
        ledger.balance_cents = 456_700
        await click(gate, "demo_reset_confirm")
        assert ledger.balance_cents == 456_700
    asyncio.run(run())


def test_admin_actions_need_confirmation_and_cannot_be_replayed_after_navigation():
    gate, request = setup()
    async def run():
        await click(gate, "admin_user:42", 99)
        assert "Пользователь ID 42" in screen(request)["text"]
        await click(gate, "admin_block:42", 99)
        assert 42 in gate.authorized
        await click(gate, "menu_main", 99)
        await click(gate, "admin_confirm", 99)
        assert 42 in gate.authorized and 42 not in gate.blocked
        await click(gate, "admin_block:42", 99)
        await click(gate, "admin_confirm", 42)
        assert 42 not in gate.blocked
        await click(gate, "admin_confirm", 99)
        assert 42 in gate.blocked and 42 not in gate.authorized
    asyncio.run(run())


def test_all_fifty_cycles_can_be_read_without_new_messages_or_mutating_ledger():
    gate, request = setup()
    ledger = gate._demo_ledger(42)
    ledger.history = [{"started_at": "2026-11-02T16:00:00+00:00", "ended_at": "2026-11-02T16:00:30+00:00",
                       "rounds": [{"stake_cents": 1000 * 2**step, "coefficient": "1.50", "payout_cents": 0} for step in range(8)],
                       "outcome": "insufficient_balance", "net_cents": -255_000} for _ in range(50)]
    original = copy.deepcopy(ledger.dump())
    async def run():
        await click(gate, "demo_history")
        message_id = gate.menu_messages[42]
        for page in range(50):
            await click(gate, f"demo_history:{page}")
            rendered = screen(request)
            assert f"Демо-цикл {page + 1} из 50" in rendered["text"]
            assert "23:00:00" in rendered["text"]
            assert len(rendered["text"]) < 4096
            assert gate.menu_messages[42] == message_id
        await click(gate, "demo_history:9999")
        assert "Демо-цикл 50 из 50" in screen(request)["text"]
    asyncio.run(run())
    assert ledger.dump() == original
    assert not any(m == "sendMessage" for m, _ in request.calls)


def test_stake_input_is_guarded_during_cycle_and_cancelled_by_main_menu():
    gate, request = setup()
    ledger = gate._demo_ledger(42)
    ledger.active = True
    async def run():
        await click(gate, "demo_set_stake")
        assert 42 not in gate.demo_pending_inputs
        assert "цикл ещё не завершён" in screen(request)["text"]
        ledger.active = False
        await click(gate, "demo_set_stake")
        assert gate.demo_pending_inputs[42] == "stake"
        await gate.handle_update({"message": {"text": "1e99999999", "message_id": 4,
                                             "chat": {"id": 42, "type": "private"}, "from": {"id": 42}}})
        assert ledger.base_stake_cents == 15_000
        await click(gate, "menu_main")
        assert 42 not in gate.demo_pending_inputs
        await click(gate, "demo_set_stake")
        ledger.active = True  # A new live round starts the cycle while the user types.
        await gate.handle_update({"message": {"text": "10", "message_id": 5,
                                             "chat": {"id": 42, "type": "private"}, "from": {"id": 42}}})
        assert ledger.base_stake_cents == 15_000
        assert 42 not in gate.demo_pending_inputs
    asyncio.run(run())


def test_buttons_are_named_and_fit_callback_limits():
    gate, request = setup()
    menus = [gate.menu(42), gate.settings_menu(42), gate.demo_menu(42), gate._demo_history_menu(42)]
    for kind in ("settings_threshold", "settings_history", "settings_delete", "demo_trigger_menu"):
        text, markup = gate._choice_screen(42, kind)
        assert 10 < len(text) < 120
        menus.append(markup)
    for markup in menus:
        for row in markup["inline_keyboard"]:
            for button in row:
                assert not re.fullmatch(r"[✓\s]*\d+", button["text"])
                assert 1 <= len(button["callback_data"].encode()) <= 64
                assert gate._supported_callback(button["callback_data"], 42) or button["callback_data"] == "menu_history"


def test_compact_tables_and_two_round_selection_in_both_modes():
    gate, request = setup()
    async def run():
        for kind, action in (("settings_threshold", "threshold:2"), ("demo_trigger_menu", "demo_trigger:2")):
            await click(gate, kind)
            rows = screen(request)['reply_markup']['inline_keyboard']
            assert any(b['callback_data'] == action for row in rows for b in row)
            assert len(rows[0]) == 3
            await click(gate, action)
        assert gate.user_threshold(42) == 2
        assert gate._demo_ledger(42).trigger_streak == 2
        for kind in ("settings_threshold", "demo_trigger_menu"):
            _, markup = gate._choice_screen(42, kind)
            assert markup['inline_keyboard'][0][0]['text'] == '✓ 2 подряд'
        for markup, count in ((gate.settings_menu(42), 5), (gate.demo_menu(42), 4)):
            for row in markup['inline_keyboard'][:count]:
                assert len(row) == 2
                assert row[0]['callback_data'] == row[1]['callback_data']
        assert len(gate._settings_text(42).splitlines()) == 1
    asyncio.run(run())
