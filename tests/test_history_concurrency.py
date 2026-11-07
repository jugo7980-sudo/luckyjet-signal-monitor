import asyncio

import httpx

from luckyjet_reliable_gate import ReliableTelegramPasswordGate
from history_panels import HISTORY_PANEL_TITLE


class RacingRequest:
    def __init__(self):
        self.calls = []
        self.next_message_id = 800

    async def __call__(self, method, payload):
        self.calls.append((method, payload))
        if method == "editMessageText" and payload.get("text") == HISTORY_PANEL_TITLE and payload.get("message_id") == 799:
            await asyncio.sleep(0)
            raise httpx.HTTPError("message to edit not found")
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
        if method == "sendMessage" and payload.get("text") == HISTORY_PANEL_TITLE
    ]
    assert len(history_sends) == 1
    assert gate.history_messages[42] == 801


def test_slow_history_request_does_not_block_other_users_and_concurrency_is_bounded():
    async def run():
        release = asyncio.Event()
        four_started = asyncio.Event()
        active = 0
        maximum = 0
        started = []

        async def request(method, payload):
            nonlocal active, maximum
            if method == 'editMessageText':
                active += 1
                maximum = max(maximum, active)
                started.append(payload['chat_id'])
                if len(started) >= 4:
                    four_started.set()
                await release.wait()
                active -= 1
            return {'ok': True, 'result': True}

        gate = ReliableTelegramPasswordGate(request, history_provider=lambda: ['1.10x'])
        for uid in range(10):
            gate.authorized.add(uid)
            gate.chats[uid] = uid
            gate.history_messages[uid] = 100 + uid
        task = asyncio.create_task(gate.update_private_histories())
        try:
            await asyncio.wait_for(four_started.wait(), timeout=0.2)
            assert len(started) == 4
        finally:
            release.set()
            await task
        assert set(started) == set(range(10))
        assert maximum == 4

    asyncio.run(run())
