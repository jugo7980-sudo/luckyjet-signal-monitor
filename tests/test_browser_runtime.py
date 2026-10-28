import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from luckyjet_signal.browser_runtime import make_low_memory_async_playwright, LOW_MEMORY_ARGS
import main as app_main


class FakeChromium:
    def __init__(self):
        self.kwargs = None

    async def launch(self, **kwargs):
        self.kwargs = kwargs
        return "browser"


class FakePlaywright:
    def __init__(self):
        self.chromium = FakeChromium()


class FakeManager:
    def __init__(self):
        self.p = FakePlaywright()
        self.exited = False

    async def __aenter__(self):
        return self.p

    async def __aexit__(self, *args):
        self.exited = True


def config():
    return SimpleNamespace(
        telegram_bot_token="token",
        telegram_chat_id="chat",
    )


def test_low_memory_wrapper_injects_flags_and_preserves_options():
    manager = FakeManager()
    factory = lambda: manager
    wrapped_factory = make_low_memory_async_playwright(factory)

    async def run():
        async with wrapped_factory() as p:
            result = await p.chromium.launch(headless=True, args=["--existing"])
            assert result == "browser"

    asyncio.run(run())

    assert manager.exited is True
    assert manager.p.chromium.kwargs["headless"] is True
    args = manager.p.chromium.kwargs["args"]
    assert "--existing" in args
    for flag in LOW_MEMORY_ARGS:
        assert flag in args


def test_history_keeps_latest_twenty_and_uses_five_by_four_grid():
    history = [f"{1 + value / 100:.2f}x" for value in range(20)]
    updated = app_main.prepend_history(history, "4.45x", limit=20)

    assert updated[0] == "4.45x"
    assert len(updated) == 20

    keyboard = app_main.build_history_keyboard(updated, columns=5)
    rows = keyboard["inline_keyboard"]
    assert [len(row) for row in rows] == [5, 5, 5, 5]
    assert rows[0][0]["text"] == "🟪4.45x"
    assert rows[0][1]["text"] == "🟦1.00x"


def test_history_with_existing_message_id_never_sends_duplicate_message():
    calls = []

    async def fake_request(method, payload):
        calls.append((method, payload))
        return {"ok": True, "result": True}

    panel = app_main.TelegramHistoryPanel(
        config(),
        request=fake_request,
        initial_history=["2.28x", "1.52x"],
        message_id=777,
    )

    async def run():
        assert await panel.ensure_panel() is True
        assert await panel.handle_monitor_message(
            config(),
            "Последний коэффициент: 4.45x",
        ) is True

    asyncio.run(run())

    assert [method for method, _ in calls] == [
        "editMessageReplyMarkup",
        "editMessageReplyMarkup",
    ]
    assert all(payload["message_id"] == 777 for _, payload in calls)


def test_history_edit_failure_does_not_create_replacement_message():
    calls = []

    async def fake_request(method, payload):
        calls.append((method, payload))
        raise RuntimeError("edit failed")

    panel = app_main.TelegramHistoryPanel(
        config(),
        request=fake_request,
        initial_history=["1.11x"] * 20,
        message_id=777,
    )

    assert asyncio.run(panel.ensure_panel()) is False
    assert [method for method, _ in calls] == ["editMessageReplyMarkup"]
    assert panel.message_id == 777


def test_streak_is_silent_before_four_and_uses_one_full_width_button():
    calls = []

    async def request(method, payload):
        calls.append((method, payload))
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": 88}}
        return {"ok": True, "result": True}

    panel = app_main.TelegramStreakPanel(config(), request)

    async def run():
        for coefficient in ["1.11x", "1.22x", "1.33x"]:
            await panel.on_round(coefficient)
        assert calls == []
        await panel.on_round("1.44x")

    asyncio.run(run())

    assert [method for method, _ in calls] == ["sendMessage"]
    assert calls[0][1]["text"] == "Серия"
    rows = calls[0][1]["reply_markup"]["inline_keyboard"]
    assert rows == [[{"text": "🔵4<2x", "callback_data": "noop"}]]


def test_streak_edits_same_button_until_break_then_schedules_delete():
    calls = []

    async def request(method, payload):
        calls.append((method, payload))
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": 77}}
        return {"ok": True, "result": True}

    panel = app_main.TelegramStreakPanel(config(), request)

    async def no_wait_delete(message_id):
        calls.append(("scheduled_delete", {"message_id": message_id}))

    panel._delete_later = no_wait_delete

    async def run():
        for coefficient in ["1.11x", "1.22x", "1.33x", "1.44x", "1.55x", "2.00x"]:
            await panel.on_round(coefficient)
        await asyncio.sleep(0)
        await panel.close()

    asyncio.run(run())

    assert [method for method, _ in calls] == [
        "sendMessage",
        "editMessageReplyMarkup",
        "editMessageReplyMarkup",
        "scheduled_delete",
    ]
    assert calls[1][1]["message_id"] == 77
    assert calls[1][1]["reply_markup"]["inline_keyboard"][0][0]["text"] == "🔵5<2x"
    assert calls[2][1]["reply_markup"]["inline_keyboard"][0][0]["text"] == "🟪5→2.00x"


def test_streak_has_no_maximum_length():
    calls = []

    async def request(method, payload):
        calls.append((method, payload))
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": 99}}
        return {"ok": True, "result": True}

    panel = app_main.TelegramStreakPanel(config(), request)

    async def run():
        for _ in range(21):
            await panel.on_round("1.50x")

    asyncio.run(run())

    assert len(panel.values) == 21
    assert calls[-1][1]["reply_markup"]["inline_keyboard"][0][0]["text"] == "🔵21<2x"
