"""Blue Mini App access in the existing compact history/prompt, no chat clutter."""
import asyncio

from history_panels import HISTORY_PANEL_TITLE
from luckyjet_password_gate import TelegramPasswordGate
from mini_app import app_url


class TelegramRecorder:
    def __init__(self):
        self.calls = []
        self.next_id = 100

    async def __call__(self, method, data):
        self.calls.append((method, data))
        if method == "sendMessage":
            self.next_id += 1
            return {"ok": True, "result": {"message_id": self.next_id}}
        return {"ok": True, "result": True}

    def by_method(self, method):
        return [data for name, data in self.calls if name == method]


def message(uid, text, mid):
    return {"message": {
        "message_id": mid,
        "chat": {"type": "private", "id": uid},
        "from": {"id": uid, "is_bot": False},
        "text": text,
    }}


def assert_blue_app_row(markup):
    first = markup["inline_keyboard"][0]
    assert len(first) == 1, "App entry should fill an entire inline-keyboard row"
    button = first[0]
    assert button == {
        "text": "Открыть приложение",
        "web_app": {"url": app_url()},
        "style": "primary",
    }
    assert "callback_data" not in button


def test_compact_history_header_and_blue_primary_button_above_coefficients():
    recorder = TelegramRecorder()
    gate = TelegramPasswordGate(recorder, history_provider=lambda: [
        "1.10x", "2.50x", "5.20x", "10.25x", "100.00x",
        "1.80x", "2.30x",
    ])
    gate.authorized.add(42)
    gate.chats[42] = 42

    async def run():
        assert await gate.ensure_private_history(42)
        initial = [p for p in recorder.by_method("sendMessage")
                   if p["text"] == HISTORY_PANEL_TITLE]
        assert len(initial) == 1
        assert_blue_app_row(initial[0]["reply_markup"])
        rows = initial[0]["reply_markup"]["inline_keyboard"]
        assert len(rows[1]) == 5 and len(rows[2]) == 2
        assert all(btn["callback_data"] == "history_refresh"
                   for row in rows[1:3] for btn in row)
        assert rows[-1] == [{"text": "☰ Меню", "callback_data": "menu_open"}]
        assert not recorder.by_method("pinChatMessage")

        old_id = gate.history_messages[42]
        assert await gate.ensure_private_history(42)
        assert gate.history_messages[42] == old_id
        assert len(recorder.by_method("sendMessage")) == 1
        edit = recorder.by_method("editMessageText")[-1]
        assert edit["text"] == HISTORY_PANEL_TITLE
        assert_blue_app_row(edit["reply_markup"])
        await gate.close()

    asyncio.run(run())


def test_authorized_start_and_repeat_start_keep_one_history_message_at_a_time():
    recorder = TelegramRecorder()
    gate = TelegramPasswordGate(recorder, history_provider=lambda: ["1.10x"] * 20,
                                password="fixture")
    gate.authorized.add(42)

    async def run():
        await gate.handle_update(message(42, "/start", 10))
        first_id = gate.history_messages[42]
        initial = [p for p in recorder.by_method("sendMessage")
                   if p["text"] == HISTORY_PANEL_TITLE]
        assert len(initial) == 1
        assert_blue_app_row(initial[0]["reply_markup"])

        # A restart or reopened chat goes through the same existing panel.
        await gate.handle_update(message(42, "/start", 11))
        second_id = gate.history_messages[42]
        assert second_id != first_id
        assert first_id in [p["message_id"] for p in recorder.by_method("deleteMessage")]
        newest = [p for p in recorder.by_method("sendMessage")
                  if p["text"] == HISTORY_PANEL_TITLE][-1]
        assert_blue_app_row(newest["reply_markup"])
        assert not recorder.by_method("pinChatMessage")
        await gate.close()

    asyncio.run(run())


def test_guest_gets_same_blue_entry_in_existing_password_prompt():
    recorder = TelegramRecorder()
    gate = TelegramPasswordGate(recorder, history_provider=lambda: ["1.40x"],
                                password="fixture")

    async def run():
        await gate.handle_update(message(77, "/start", 10))
        assert not gate.history_messages
        prompts = [p for p in recorder.by_method("sendMessage")
                   if "Введите пароль" in p["text"]]
        assert len(prompts) == 1
        assert_blue_app_row(prompts[0]["reply_markup"])

        # Incorrect password edits the existing prompt, not a second post.
        await gate.handle_update(message(77, "wrong", 11))
        updated = [p for p in recorder.by_method("editMessageText")
                   if p["chat_id"] == 77]
        assert updated
        assert_blue_app_row(updated[-1]["reply_markup"])
        assert not recorder.by_method("pinChatMessage")
        await gate.close()

    asyncio.run(run())


def test_command_app_has_blue_web_button_without_pin_and_others_preserved():
    recorder = TelegramRecorder()
    gate = TelegramPasswordGate(recorder, password="fixture")

    async def run():
        await gate.handle_update(message(77, "/app", 20))
        sent = recorder.by_method("sendMessage")
        assert len(sent) == 1
        assert sent[0]["text"] == "Открыть Lucky Jet:"
        assert_blue_app_row(sent[0]["reply_markup"])
        assert not recorder.by_method("pinChatMessage")
        assert recorder.by_method("setChatMenuButton")[-1]["menu_button"]["type"] == "commands"
        await gate.close()

    asyncio.run(run())
