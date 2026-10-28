import asyncio

import httpx

from luckyjet_reliable_gate import ReliableTelegramPasswordGate


class RacingRequest:
    def __init__(self):
        self.calls = []
        self.next_message_id = 800

    async def __call__(self, method, payload):
        self.calls.append((method, payload))
        if method == "editMessageReplyMarkup" and payload.get("message_id") == 799:
            await asyncio.sleep(0)
            raise httpx.HTTPError("stale history message")
        if method == "sendMessage":
            await asyncio.sleep(0)
            self.next_message_id += 1
            return {"ok": True, "result": {"message_id": self.next_message_id}}
        return {"ok": True, "result": True}


def test_concurrent_history_recovery_creates_only_one_replacement():
    request = RacingRequest()
    gate = ReliableTelegramPasswordGate(
        request,
        history_provider=lambda: ["1.10x", "2.20x"],
        password="122011",
    )
    gate.authorized.add(42)
    gate.chats[42] = 42
    gate.history_messages[42] = 799

    async def run():
        await asyncio.gather(
            gate.ensure_private_history(42),
            gate.ensure_private_history(42),
        )

    asyncio.run(run())

    history_sends = [
        payload
        for method, payload in request.calls
        if method == "sendMessage" and payload.get("text") == "🎰 Lucky Jet — история"
    ]
    assert len(history_sends) == 1
    assert gate.history_messages[42] == 801
