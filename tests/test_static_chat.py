import asyncio

import httpx
import pytest

from luckyjet_reliable_gate import ReliableTelegramPasswordGate
from luckyjet_signal.demo_ledger import DemoLedger


class Telegram:
    def __init__(self):
        self.calls = []
        self.next_id = 100
        self.failure = None

    async def __call__(self, method, payload):
        self.calls.append((method, payload))
        await asyncio.sleep(0)
        if method.startswith("editMessage") and self.failure:
            raise self.failure
        if method == "sendMessage":
            self.next_id += 1
            return {"result": {"message_id": self.next_id}}
        return {"result": True}


def callback(data, message_id):
    return {"id": data, "data": data, "from": {"id": 42},
            "message": {"message_id": message_id, "chat": {"id": 42}}}


def test_navigation_and_thirty_rounds_keep_exactly_two_messages():
    request = Telegram()
    gate = ReliableTelegramPasswordGate(request, history_provider=lambda: ["1.20x"])
    gate.authorized.add(42)
    gate.chats[42] = 42
    gate.notifications_disabled.add(42)

    async def run():
        await gate.ensure_private_history(42)
        await gate.handle_callback(callback("menu_open", gate.history_messages[42]))
        menu_id = gate.menu_messages[42]
        for action in ("demo_menu", "demo_toggle", "demo_trigger:3"):
            await gate.handle_callback(callback(action, menu_id))
        for _ in range(5):
            for value in ["1.20x"] * 5 + ["3.00x"]:
                await gate.on_round(value)
        for action in ("demo_refresh_balance", "demo_history", "menu_main", "menu_settings", "demo_menu"):
            await gate.handle_callback(callback(action, menu_id))
            assert gate.menu_messages[42] == menu_id
        assert len(gate.demo_ledgers[42].history) == 5
        assert "UTC" in gate._demo_history_text(42)
        await gate.close()

    asyncio.run(run())
    assert len([1 for method, _ in request.calls if method == "sendMessage"]) == 2
    assert not any(method == "deleteMessage" for method, _ in request.calls)


def test_menu_and_history_network_errors_never_create_replacements():
    request = Telegram()
    gate = ReliableTelegramPasswordGate(request)
    gate.chats[42] = 42
    gate.menu_messages[42] = 55
    gate.history_messages[42] = 66
    request.failure = httpx.ConnectError("offline")

    async def run():
        with pytest.raises(httpx.ConnectError):
            await gate._send_transient(42, 42, "menu", gate.menu(42))
        assert await gate.ensure_private_history(42) is False

    asyncio.run(run())
    assert gate.menu_messages[42] == 55
    assert gate.history_messages[42] == 66
    assert not any(method == "sendMessage" for method, _ in request.calls)


def test_cycle_dates_persist_across_restart_and_legacy_dates_remain_unknown():
    ledger = DemoLedger(enabled=True, trigger_streak=3)
    for _ in range(3):
        ledger.process_round("1.20")
    restored = DemoLedger(**ledger.dump())
    assert restored.cycle_started_at == ledger.cycle_started_at
    restored.process_round("2.00")
    entry = restored.history[0]
    assert entry["started_at"] <= entry["ended_at"]
    assert ReliableTelegramPasswordGate._cycle_time("2026-11-02T15:33:00+00:00") == "02.11.2026 15:33:00 UTC"
    assert ReliableTelegramPasswordGate._cycle_time(None) == "время не сохранено"
