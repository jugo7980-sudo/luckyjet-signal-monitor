import asyncio

from test_intuitive_ui import setup, click, screen


def _cycle_buttons(payload):
    return [
        button
        for row in payload["reply_markup"]["inline_keyboard"]
        for button in row
        if button["callback_data"].startswith("demo_cycle:")
    ]


def test_cycle_button_shows_round_count_date_and_result():
    gate, request = setup()
    gate._demo_ledger(42).history = [
        {
            "started_at": "2026-11-02T16:00:00+00:00",
            "ended_at": "2026-11-02T16:00:30+00:00",
            "net_cents": 1000,
            "rounds": [{} for _ in range(7)],
            "outcome": "recovered",
        }
    ]

    async def run():
        await click(gate, "demo_history")
        button = _cycle_buttons(screen(request))[0]
        assert button["text"] == "7 цик. · 02.11 23:00 · +$10.00"

    asyncio.run(run())
