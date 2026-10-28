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


def test_live_history_helpers_keep_duplicates_trim_and_build_three_columns():
    history = [f"{value:.2f}x" for value in range(1, 16)]
    updated = app_main.prepend_history(history, "4.45x", limit=15)

    assert updated[0] == "4.45x"
    assert len(updated) == 15

    duplicated = app_main.prepend_history(["1.00x"], "1.00x", limit=15)
    assert duplicated == ["1.00x", "1.00x"]

    keyboard = app_main.build_history_keyboard(
        ["4.45x", "1.00x", "1.52x", "2.28x"],
        columns=3,
    )
    rows = keyboard["inline_keyboard"]
    assert [len(row) for row in rows] == [3, 1]
    assert rows[0][0]["text"] == "🟪 4.45x"
    assert rows[1][0]["text"] == "🟪 2.28x"


def test_live_history_panel_creates_once_then_edits_same_message():
    calls = []

    async def fake_request(method, payload):
        calls.append((method, payload))
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": 777}}
        return {"ok": True, "result": True}

    config = SimpleNamespace(
        telegram_bot_token="token",
        telegram_chat_id="chat",
    )
    panel = app_main.TelegramHistoryPanel(
        config,
        request=fake_request,
        initial_history=["2.28x", "1.52x"],
    )

    async def run():
        assert await panel.ensure_panel() is True
        assert await panel.handle_monitor_message(
            config,
            "Последний коэффициент: 4.45x",
        ) is True

    asyncio.run(run())

    assert [method for method, _ in calls] == [
        "sendMessage",
        "editMessageReplyMarkup",
    ]
    assert calls[0][1]["text"] == "🎰 Lucky Jet — история"
    assert calls[1][1]["message_id"] == 777
    first_button = calls[1][1]["reply_markup"]["inline_keyboard"][0][0]
    assert first_button["text"] == "🟪 4.45x"
