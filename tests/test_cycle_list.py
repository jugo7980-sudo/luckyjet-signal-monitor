import asyncio
import copy

from test_intuitive_ui import setup, click, screen


def entries(count):
    return [{"started_at": f"2026-11-02T16:{i:02d}:00+00:00",
             "ended_at": f"2026-11-02T16:{i:02d}:30+00:00",
             "net_cents": 1000 + i, "rounds": [], "outcome": "recovered"}
            for i in range(count)]


def buttons(payload, prefix):
    return [b for row in payload['reply_markup']['inline_keyboard'] for b in row
            if b['callback_data'].startswith(prefix)]


def test_cycle_list_pages_details_and_return_are_static_and_stable():
    gate, request = setup()
    ledger = gate._demo_ledger(42)
    ledger.history = entries(21)
    original = copy.deepcopy(ledger.dump())
    async def run():
        await click(gate, 'demo_history')
        menu_id = gate.menu_messages[42]
        first = screen(request)
        assert '21 циклов · 1/3' in first['text']
        assert len(buttons(first, 'demo_cycle:')) == 10
        assert '02.11 23:00' in buttons(first, 'demo_cycle:')[0]['text']
        assert '+$10.00' in buttons(first, 'demo_cycle:')[0]['text']
        next_page = buttons(first, 'demo_list:')[0]['callback_data']
        await click(gate, next_page)
        second = screen(request)
        assert len(buttons(second, 'demo_cycle:')) == 10
        selected = buttons(second, 'demo_cycle:')[3]['callback_data']
        # A completed live cycle must not change which cycle an existing button opens.
        ledger.history.insert(0, entries(1)[0])
        await click(gate, selected)
        detail = screen(request)
        assert 'Начало: 02.11.2026 23:13:00' in detail['text']
        assert 'Чистый результат: +$10.13' in detail['text']
        await click(gate, buttons(detail, 'demo_list:')[0]['callback_data'])
        assert buttons(screen(request), 'demo_cycle:') == buttons(second, 'demo_cycle:')
        await click(gate, buttons(screen(request), 'demo_list:')[-1]['callback_data'])
        assert len(buttons(screen(request), 'demo_cycle:')) == 1
        assert gate.menu_messages[42] == menu_id
        ledger.history.pop(0)
        assert ledger.dump() == original
        assert not any(m == 'sendMessage' for m, _ in request.calls)
    asyncio.run(run())


def test_expired_or_other_users_cycle_button_returns_list_safely():
    gate, request = setup()
    gate._demo_ledger(42).history = entries(1)
    async def run():
        await click(gate, 'demo_history')
        old = buttons(screen(request), 'demo_cycle:')[0]['callback_data']
        gate._demo_history_views.clear()  # restart or reset invalidates the view
        await click(gate, old)
        assert 'История демо-циклов' in screen(request)['text']
        await click(gate, old, 99)
        assert 'Циклов пока нет' in screen(request)['text']
    asyncio.run(run())


def test_history_menu_button_hides_across_sections_and_returns_on_close():
    gate, request = setup()
    gate.history_provider = lambda: ['1.20x'] * 20
    def menu_button():
        return any(b['callback_data'] == 'menu_open' for row in gate._history_markup(42)['inline_keyboard'] for b in row)
    async def run():
        assert menu_button()
        await click(gate, 'menu_open')
        assert not menu_button()
        for action in ('menu_settings', 'demo_menu', 'demo_history', 'menu_main'):
            await click(gate, action)
            assert not menu_button()
        await click(gate, 'menu_close')
        assert menu_button()
        edits = [p for m, p in request.calls if m == 'editMessageText' and p.get('message_id') == 10]
        assert edits[-1]['reply_markup']['inline_keyboard'][-1][0]['callback_data'] == 'menu_open'
        assert 42 not in gate.menu_messages
    asyncio.run(run())


def test_all_fifty_cycles_and_partial_pages_remain_accessible():
    gate, request = setup()
    gate._demo_ledger(42).history = entries(50)
    async def run():
        await click(gate, 'demo_history')
        seen = set()
        for page in range(5):
            listing = screen(request)
            cycles = buttons(listing, 'demo_cycle:')
            assert len(cycles) == 10
            for button in cycles:
                seen.add(button['callback_data'])
                assert len(button['callback_data'].encode()) <= 64
            if page < 4:
                await click(gate, buttons(listing, 'demo_list:')[-1]['callback_data'])
        assert len(seen) == 50
        assert len(buttons(screen(request), 'demo_list:')) == 1
        for count in (0, 1, 10, 11):
            gate._demo_ledger(42).history = entries(count)
            await click(gate, 'demo_history')
            assert len(buttons(screen(request), 'demo_cycle:')) == min(count, 10)
            assert len(buttons(screen(request), 'demo_list:')) == (1 if count > 10 else 0)
        assert all(len(row) == 1 for row in gate.menu(42)['inline_keyboard'])
        assert all(len(row) == 1 for row in gate.settings_menu(42)['inline_keyboard'][5:-1])
    asyncio.run(run())
