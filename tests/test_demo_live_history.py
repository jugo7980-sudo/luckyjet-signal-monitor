import asyncio

from test_intuitive_ui import setup, click, screen


def entry(marker, net=1000):
    return {
        "started_at": "2026-11-03T00:00:00+00:00",
        "ended_at": "2026-11-03T00:01:00+00:00",
        "rounds": [{"stake_cents": 1000, "coefficient": "2.00", "payout_cents": 2000}],
        "base_stake_cents": 1000,
        "balance_before_cents": 1_000_000,
        "balance_after_cents": 1_001_000,
        "total_stake_cents": 1000,
        "payout_cents": 2000,
        "net_cents": net,
        "outcome": "recovered",
        "coefficient": "2.00",
        "marker": marker,
    }


def buttons(payload, prefix):
    return [button for row in payload["reply_markup"]["inline_keyboard"] for button in row
            if button.get("callback_data", "").startswith(prefix)]


def test_open_first_history_page_refreshes_when_new_session_finishes():
    gate, request = setup()
    ledger = gate._demo_ledger(42)
    ledger.history = [entry(1)]

    async def run():
        await click(gate, "demo_history")
        before = screen(request)
        assert "1 циклов · 1/1" in before["text"]
        ledger.history.insert(0, entry(2, 2000))
        await gate._refresh_open_demo_history(42)
        after = screen(request)
        assert "2 циклов · 1/1" in after["text"]
        assert "+$20.00" in buttons(after, "demo_cycle:")[0]["text"]
        await gate.close()
    asyncio.run(run())


def test_new_session_does_not_force_user_off_later_history_page():
    gate, request = setup()
    ledger = gate._demo_ledger(42)
    ledger.history = [entry(i) for i in range(25)]

    async def run():
        await click(gate, "demo_history")
        first = screen(request)
        next_page = buttons(first, "demo_list:")[-1]["callback_data"]
        await click(gate, next_page)
        second = screen(request)
        assert "2/3" in second["text"]
        second_text = second["text"]
        ledger.history.insert(0, entry(99, 9900))
        await gate._refresh_open_demo_history(42)
        after = screen(request)
        assert after["text"] == second_text
        assert "2/3" in after["text"]
        await gate.close()
    asyncio.run(run())


def test_demo_warning_close_only_deletes_warning_message():
    gate, request = setup()

    async def run():
        await gate.handle_callback({
            "id": "warning-close",
            "from": {"id": 42},
            "message": {"chat": {"id": 42}, "message_id": 777},
            "data": "demo_warning_close",
        })
    asyncio.run(run())

    deleted = [payload for method, payload in request.calls if method == "deleteMessage"]
    assert deleted[-1] == {"chat_id": 42, "message_id": 777}
    assert gate._demo_ledger(42).history == []
