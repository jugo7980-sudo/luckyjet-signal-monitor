import asyncio

import httpx

from luckyjet_reliable_gate import ReliableTelegramPasswordGate


class NotModifiedRequest:
    def __init__(self):
        self.calls = []

    async def __call__(self, method, payload):
        self.calls.append((method, payload))
        if method == "editMessageReplyMarkup":
            request = httpx.Request("POST", "https://api.telegram.org/bot-test/editMessageReplyMarkup")
            response = httpx.Response(
                400,
                request=request,
                json={"ok": False, "description": "Bad Request: message is not modified"},
            )
            raise httpx.HTTPStatusError(
                "400 Bad Request",
                request=request,
                response=response,
            )
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": 999}}
        return {"ok": True, "result": True}


def test_unchanged_history_is_success_not_recreated():
    request = NotModifiedRequest()
    gate = ReliableTelegramPasswordGate(
        request,
        history_provider=lambda: ["1.10x", "2.20x"],
        password="122011",
    )
    gate.authorized.add(42)
    gate.chats[42] = 42
    gate.history_messages[42] = 555

    result = asyncio.run(gate.ensure_private_history(42))

    assert result is True
    assert gate.history_messages[42] == 555
    assert not any(method == "sendMessage" for method, _ in request.calls)
