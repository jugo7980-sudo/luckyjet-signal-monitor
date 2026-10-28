import asyncio

import httpx

from luckyjet_reliable_gate import ReliableTelegramPasswordGate


class Telegram:
    def __init__(self, fail_send=False):
        self.calls = []
        self.fail_send = fail_send

    async def __call__(self, method, payload):
        self.calls.append((method, payload.copy()))
        await asyncio.sleep(0)
        if method == "sendMessage":
            if self.fail_send:
                self.fail_send = False
                raise httpx.ConnectError("offline")
            return {"ok": True, "result": {"message_id": 103}}
        return {"ok": True, "result": True}


def setup(fail_send=False):
    request = Telegram(fail_send)
    gate = ReliableTelegramPasswordGate(
        request, password="test", history_provider=lambda: ["1.10x"] * 20
    )
    gate.authorized.add(42)
    gate.chats[42] = 42
    gate.history_messages[42] = 101
    gate.menu_messages[42] = 102
    callback = {
        "id": "history-click", "data": "menu_history", "from": {"id": 42},
        "message": {"message_id": 102, "chat": {"id": 42, "type": "private"}},
    }
    return gate, request, callback


def test_history_click_shows_panel_after_menu_and_keeps_menu():
    gate, request, callback = setup()

    async def run():
        await gate.handle_callback(callback)
        await gate.update_private_histories()

    asyncio.run(run())
    assert gate.history_messages[42] == 103
    assert gate.menu_messages[42] == 102
    sent = [(i, p) for i, (m, p) in enumerate(request.calls) if m == "sendMessage"]
    assert len(sent) == 1
    rows = sent[0][1]["reply_markup"]["inline_keyboard"]
    assert [len(row) for row in rows[:4]] == [5, 5, 5, 5]
    deleted = [(i, p["message_id"]) for i, (m, p) in enumerate(request.calls) if m == "deleteMessage"]
    assert [mid for _, mid in deleted] == [101]
    assert deleted[0][0] > sent[0][0]
    edits = [p for m, p in request.calls if m == "editMessageReplyMarkup"]
    assert edits[-1]["message_id"] == 103


def test_repeated_history_clicks_and_background_update_do_not_duplicate_panel():
    gate, request, callback = setup()

    async def run():
        await asyncio.gather(
            gate.handle_callback(callback), gate.handle_callback(callback),
            gate.update_private_histories(),
        )

    asyncio.run(run())
    assert gate.history_messages[42] == 103
    assert len([1 for m, _ in request.calls if m == "sendMessage"]) == 1


def test_failed_open_preserves_previous_history_and_menu():
    gate, request, callback = setup(fail_send=True)
    asyncio.run(gate.handle_callback(callback))
    assert gate.history_messages[42] == 101
    assert gate.menu_messages[42] == 102
    assert not any(m == "deleteMessage" for m, _ in request.calls)
    assert any(m == "sendMessage" and "не удалось" in p.get("text", "") for m, p in request.calls)


def test_unauthorized_click_does_not_show_history():
    gate, request, callback = setup()
    gate.authorized.clear()
    asyncio.run(gate.handle_callback(callback))
    assert [m for m, _ in request.calls] == ["answerCallbackQuery"]
