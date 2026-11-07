import asyncio

from test_intuitive_ui import setup, click
from test_history_open import MemoryRedis
from luckyjet_reliable_gate import ReliableTelegramPasswordGate


def test_inactivity_closes_only_menu_clears_pending_input_and_preserves_demo(monkeypatch):
    gate, request = setup()
    now = [1000.0]
    monkeypatch.setattr('luckyjet_password_gate.time.time', lambda: now[0])
    async def run():
        await click(gate, 'demo_menu')
        ledger = gate._demo_ledger(42)
        ledger.enabled = True
        ledger.balance_cents = 1234500
        menu_id = gate.menu_messages[42]
        await click(gate, 'demo_set_stake')
        deadline = gate.menu_deadlines[42]
        assert deadline == 1060
        assert not await gate._close_menu(42, expected_deadline=deadline, now=1059)
        assert gate.menu_messages[42] == menu_id
        assert await gate._close_menu(42, expected_deadline=deadline, now=1060)
        assert 42 not in gate.menu_messages and 42 not in gate.demo_pending_inputs
        assert gate.history_messages[42] == 10
        assert ledger.enabled and ledger.balance_cents == 1234500
        assert any(b.get('callback_data') == 'menu_open' for row in gate._history_markup(42)['inline_keyboard'] for b in row)
        deleted = [p['message_id'] for m, p in request.calls if m == 'deleteMessage']
        assert deleted == [menu_id]
        await gate.close()
    asyncio.run(run())


def test_click_extends_timer_but_live_updates_do_not_and_old_timeout_cannot_close_new_screen(monkeypatch):
    gate, request = setup()
    now = [1000.0]
    monkeypatch.setattr('luckyjet_password_gate.time.time', lambda: now[0])
    async def run():
        await click(gate, 'demo_menu')
        first = gate.menu_deadlines[42]
        now[0] = 1030
        gate._demo_ledger(42).balance_cents -= 1000
        await gate._refresh_demo_panel(42)
        assert gate.menu_deadlines[42] == first
        await click(gate, 'menu_settings')
        assert gate.menu_deadlines[42] == 1090
        assert not await gate._close_menu(42, expected_deadline=first, now=1060)
        await click(gate, 'menu_close')
        now[0] = 1100
        await click(gate, 'menu_open')
        assert not await gate._close_menu(42, expected_deadline=1090, now=1200)
        assert 42 in gate.menu_messages
        await gate.close()
    asyncio.run(run())


def test_menu_deadline_survives_restart_and_expired_menu_is_removed(monkeypatch):
    gate, request = setup()
    gate.redis = MemoryRedis()
    now = [1000.0]
    monkeypatch.setattr('luckyjet_password_gate.time.time', lambda: now[0])
    async def run():
        await click(gate, 'menu_main')
        await gate._persist_user(42)
        restored = ReliableTelegramPasswordGate(request, admin_chat_id=99)
        restored.redis = gate.redis
        await restored._load_persisted_state()
        assert restored.menu_deadlines[42] == 1060
        now[0] = 1061
        await restored.restore_runtime_state()
        assert 42 not in restored.menu_messages
        assert '42' not in gate.redis.hashes['luckyjet:menu_messages']
        assert '42' not in gate.redis.hashes['luckyjet:menu_deadlines']
        gate.redis = None
        restored.redis = None
        await restored.close()
        await gate.close()
    asyncio.run(run())


def test_timer_task_automatically_closes_menu_and_shutdown_cancels_waiting_tasks(monkeypatch):
    gate, request = setup()
    monkeypatch.setattr('luckyjet_password_gate.MENU_IDLE_SECONDS', 0.01)
    async def run():
        await click(gate, 'menu_main')
        await asyncio.sleep(0.05)
        assert 42 not in gate.menu_messages
        assert gate.history_messages[42] == 10
        await click(gate, 'menu_open')
        menu_id = gate.menu_messages[42]
        await gate.close()
        await asyncio.sleep(0.02)
        assert gate.menu_messages[42] == menu_id
        assert not gate._menu_tasks
    asyncio.run(run())


def test_failed_delete_keeps_menu_and_deadline_for_retry(monkeypatch):
    import httpx
    import pytest
    gate, request = setup()
    monkeypatch.setattr('luckyjet_password_gate.time.time', lambda: 1000)
    async def run():
        await click(gate, 'menu_main')
        menu_id = gate.menu_messages[42]
        async def offline(method, payload):
            if method == 'deleteMessage':
                raise httpx.ConnectError('offline')
            return await request(method, payload)
        gate.request = offline
        with pytest.raises(httpx.ConnectError):
            await gate._close_menu(42, expected_deadline=1060, now=1061)
        assert gate.menu_messages[42] == menu_id
        assert gate.menu_deadlines[42] == 1060
        gate.request = request
        assert await gate._close_menu(42, expected_deadline=1060, now=1061)
        await gate.close()
    asyncio.run(run())
