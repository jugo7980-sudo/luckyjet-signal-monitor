import asyncio
from types import SimpleNamespace

import app_v1
import main as base


class FakeRequest:
    def __init__(self):
        self.calls = []

    async def __call__(self, method, payload):
        self.calls.append((method, payload))
        return {"ok": True, "result": True}


def config():
    return SimpleNamespace(
        telegram_bot_token="token",
        telegram_chat_id="42",
    )


def test_record_history_updates_memory_without_sending_telegram_message():
    request = FakeRequest()
    panel = base.TelegramHistoryPanel(
        config(),
        request=request,
        initial_history=["1.10x", "2.20x"],
        message_id=149,
    )

    formatted = app_v1.record_history_coefficient(panel, "3.30x")

    assert formatted == "3.30x"
    assert panel.history[:3] == ["3.30x", "1.10x", "2.20x"]
    assert request.calls == []


def test_retire_legacy_panel_deletes_old_shared_message_once():
    request = FakeRequest()
    panel = base.TelegramHistoryPanel(
        config(),
        request=request,
        initial_history=["1.10x"],
        message_id=149,
    )

    async def run():
        await app_v1.retire_legacy_history_panel(panel)
        await app_v1.retire_legacy_history_panel(panel)

    asyncio.run(run())

    deletes = [payload for method, payload in request.calls if method == "deleteMessage"]
    assert deletes == [{"chat_id": "42", "message_id": 149}]
    assert panel.message_id is None
