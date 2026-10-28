import asyncio
from types import SimpleNamespace

from luckyjet_history_panel import MenuHistoryPanel
from luckyjet_reliable_gate import ReliableTelegramPasswordGate


class FakeRequest:
    def __init__(self):
        self.calls = []

    async def __call__(self, method, payload):
        self.calls.append((method, payload))
        return {"ok": True, "result": {"message_id": 999}}


def config():
    return SimpleNamespace(
        telegram_bot_token="token",
        telegram_chat_id="42",
    )


def test_shared_history_panel_preserves_five_by_four_grid_and_menu_button():
    request = FakeRequest()
    history = [f"{1 + index / 10:.2f}x" for index in range(20)]
    panel = MenuHistoryPanel(
        config(),
        request=request,
        initial_history=history,
        message_id=149,
    )

    markup = panel._reply_markup()["inline_keyboard"]
    assert [len(row) for row in markup[:-1]] == [5, 5, 5, 5]
    assert markup[-1] == [{"text": "☰ Меню", "callback_data": "menu_open"}]


def test_owner_adopts_existing_shared_history_and_deletes_private_duplicate():
    request = FakeRequest()
    gate = ReliableTelegramPasswordGate(
        request,
        admin_chat_id="42",
        shared_history_message_id=149,
    )
    gate.authorized.add(42)
    gate.chats[42] = 42
    gate.history_messages[42] = 777

    asyncio.run(gate.adopt_shared_history())

    assert gate.history_messages[42] == 149
    deletes = [payload for method, payload in request.calls if method == "deleteMessage"]
    assert {"chat_id": 42, "message_id": 777} in deletes


def test_live_private_history_refresh_skips_owner_shared_message_to_avoid_double_edit():
    request = FakeRequest()
    gate = ReliableTelegramPasswordGate(
        request,
        history_provider=lambda: ["1.10x"],
        admin_chat_id="42",
        shared_history_message_id=149,
    )
    gate.authorized.update({42, 43})
    gate.chats.update({42: 42, 43: 43})
    gate.history_messages[42] = 149
    gate.history_messages[43] = 150

    asyncio.run(gate.update_private_histories())

    edits = [payload for method, payload in request.calls if method == "editMessageReplyMarkup"]
    assert [payload["message_id"] for payload in edits] == [150]
