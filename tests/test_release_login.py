import asyncio

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
        # Telegram may accept edits to a panel the user removed locally.
        return {"result": True}


def message(text, mid=1):
    return {"message": {"message_id": mid, "text": text,
                        "chat": {"id": 42, "type": "private"},
                        "from": {"id": 42, "is_bot": False}}}


def test_start_after_clearing_chat_creates_visible_password_prompt():
    request = Telegram()
    gate = ReliableTelegramPasswordGate(request, password="test-secret")
    gate.prompt_messages[42] = 5
    asyncio.run(gate.handle_update(message("/start")))
    assert gate.prompt_messages[42] != 5
    assert any(m == "sendMessage" and "Введите пароль" in p["text"] for m, p in request.calls)


def test_authorized_start_recovers_old_hidden_panels():
    request = Telegram()
    gate = ReliableTelegramPasswordGate(request, password="test-secret")
    gate.authorized.add(42)
    gate.chats[42] = 42
    gate.history_hidden.add(42)
    gate.history_messages[42] = 5
    gate.menu_messages[42] = 6
    asyncio.run(gate.handle_update(message("/start")))
    assert 42 not in gate.history_hidden
    assert gate.history_messages[42] not in {5, 6}
    assert gate.menu_messages[42] not in {5, 6}


def test_unicode_wrong_password_does_not_crash_and_success_opens_both_panels():
    request = Telegram()
    gate = ReliableTelegramPasswordGate(request, password="test-secret")
    gate.history_hidden.add(42)
    async def run():
        await gate.handle_update(message("/start"))
        await gate.handle_update(message("неверный пароль", 2))
        assert 42 not in gate.authorized
        await gate.handle_update(message("test-secret", 3))
    asyncio.run(run())
    assert 42 in gate.authorized
    assert 42 not in gate.history_hidden
    assert 42 in gate.history_messages
    assert 42 in gate.menu_messages
