"""Telegram /restart is public, confirmed, globally rate-limited, and data-safe."""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from luckyjet_password_gate import (
    TelegramPasswordGate, RestartRequested,
    RESTART_COOLDOWN_KEY, RESTART_COOLDOWN_SECONDS,
)


def incoming(uid, message_id=123):
    return {"message": {
        "message_id": message_id, "chat": {"id": uid, "type": "private"},
        "from": {"id": uid}, "text": "/restart"
    }}


def confirmation(uid, callback_data, message_id=124):
    return {"id": "cb-" + str(uid), "from": {"id": uid},
            "message": {"message_id": message_id,
                        "chat": {"id": uid, "type": "private"}},
            "data": callback_data}


def test_public_restart_confirmation_cooldown_preserves_history_demo_settings():
    from fakeredis.aioredis import FakeRedis

    async def run():
        calls = []
        async def request(method, payload):
            calls.append((method, payload))
            return {"ok": True, "result": {"message_id": 124}}

        gate = TelegramPasswordGate(request, password="fixture", admin_chat_id=1)
        gate.redis = FakeRedis(decode_responses=True)
        # Non-admin unregistered users are authorized to request a restart.
        await gate.redis.set("luckyjet:state", "history and coefficients")
        await gate.redis.hset("luckyjet:demo_sessions", "22",
                              '{"balance_cents":1050000,"enabled":true,"history":[{"win":1}]}')
        await gate.redis.hset("luckyjet:thresholds", "22", "8")
        await gate.redis.sadd("luckyjet:authorized_users", "1")
        await gate.handle_update(incoming(22))
        messages = [body for method, body in calls if method == "sendMessage"]
        prompt = messages[-1]
        nonce_button = prompt["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        cancel_button = prompt["reply_markup"]["inline_keyboard"][1][0]["callback_data"]
        assert nonce_button.startswith("restart_confirm:")
        assert cancel_button.startswith("restart_cancel:")
        assert not gate._restart_requested.is_set()
        assert await gate.redis.get(RESTART_COOLDOWN_KEY) is None

        # Wrong user cannot replay another person's confirmation.
        await gate.handle_callback(confirmation(99, nonce_button))
        assert not gate._restart_requested.is_set()
        assert await gate.redis.get(RESTART_COOLDOWN_KEY) is None

        # Cancellation does not spend the global cooldown.
        await gate.handle_callback(confirmation(22, cancel_button))
        assert not gate._restart_requested.is_set()
        assert await gate.redis.get(RESTART_COOLDOWN_KEY) is None

        await gate.handle_update(incoming(22, 130))
        fresh = [body for method, body in calls if method == "sendMessage"][-1]
        confirm = fresh["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        await gate.handle_callback(confirmation(22, confirm))
        assert gate._restart_requested.is_set()
        assert await gate.redis.get(RESTART_COOLDOWN_KEY) == "22"
        assert 0 < await gate.redis.ttl(RESTART_COOLDOWN_KEY) <= RESTART_COOLDOWN_SECONDS
        assert await gate.redis.get("luckyjet:state") == "history and coefficients"
        assert await gate.redis.hget("luckyjet:thresholds", "22") == "8"
        assert json.loads(await gate.redis.hget("luckyjet:demo_sessions", "22"))["balance_cents"] == 1050000
        assert await gate.redis.sismember("luckyjet:authorized_users", "1")
        # A distinct user is also blocked by the global cooldown.
        other = TelegramPasswordGate(request, password="fixture", admin_chat_id=1)
        other.redis = gate.redis
        await other.handle_update(incoming(44))
        after = [body for method, body in calls if method == "sendMessage"][-1]["text"]
        assert "через" in after and "мин." in after
        assert not other._restart_pending and not other._restart_requested.is_set()
        await gate.close()
        other.redis = None

    asyncio.run(run())


def test_public_restart_expiration_blocked_user_and_no_redis(monkeypatch):
    from fakeredis.aioredis import FakeRedis
    import luckyjet_password_gate

    async def run():
        calls = []
        async def request(method, payload):
            calls.append((method, payload))
            return {"ok": True, "result": {"message_id": 123}}

        gate = TelegramPasswordGate(request, password="fixture")
        gate.redis = FakeRedis(decode_responses=True)
        gate.blocked.add(33)
        await gate.handle_update(incoming(33))
        assert 33 not in gate._restart_pending
        assert not gate._restart_requested.is_set()

        await gate.handle_update(incoming(42))
        token = gate._restart_pending[42][0]
        monkeypatch.setattr(luckyjet_password_gate.time, "monotonic",
                            lambda: gate._restart_pending[42][1] + 1)
        await gate.handle_callback(confirmation(42, "restart_confirm:" + token))
        assert not gate._restart_requested.is_set()
        assert await gate.redis.get(RESTART_COOLDOWN_KEY) is None

        await gate.close()
        gate = TelegramPasswordGate(request, password="fixture")
        await gate.handle_update(incoming(42))
        assert "нет подключения к Redis" in [body for method, body in calls if method == "sendMessage"][-1]["text"]
    asyncio.run(run())


def test_restart_bubbles_out_of_poller_and_registers_command(monkeypatch):
    from fakeredis.aioredis import FakeRedis
    from release_reset import MARKER_KEY, RELEASE_ID, ACCESS_MARKER_KEY, ACCESS_RESET_ID
    import redis.asyncio

    async def run():
        fake = FakeRedis(decode_responses=True)
        # This is a routine restart; earlier release migrations must not fire again.
        await fake.set(MARKER_KEY, RELEASE_ID)
        await fake.set(ACCESS_MARKER_KEY, ACCESS_RESET_ID)
        await fake.set("luckyjet:state", "retain")
        await fake.hset("luckyjet:demo_sessions", "55", '{"balance_cents":204000}')
        monkeypatch.setattr(redis.asyncio, "from_url", lambda *args, **kwargs: fake)

        sent = []
        polls = [0]
        async def request(method, payload):
            sent.append((method, payload))
            if method == "getUpdates":
                polls[0] += 1
                if polls[0] == 1:
                    return {"result": [{"update_id": 1, **incoming(55)}]}
                buttons = [v["reply_markup"]["inline_keyboard"] for m, v in sent
                           if m == "sendMessage" and "reply_markup" in v]
                callback = buttons[-1][0][0]["callback_data"]
                return {"result": [{"update_id": 2,
                                    "callback_query": confirmation(55, callback)}]}
            return {"ok": True, "result": {"message_id": 150}}

        gate = TelegramPasswordGate(request, password="fixture")
        gate.redis_url = "redis://fixture"
        with pytest.raises(RestartRequested):
            await asyncio.wait_for(gate.start(), timeout=3)
        commands = [body["commands"] for name, body in sent if name == "setMyCommands"]
        assert any(x["command"] == "restart" for x in commands[0])
        assert polls[0] == 2
        assert gate._restart_requested.is_set()
        assert await fake.get("luckyjet:state") == "retain"
        assert await fake.hget("luckyjet:demo_sessions", "55") == '{"balance_cents":204000}'

    asyncio.run(run())
