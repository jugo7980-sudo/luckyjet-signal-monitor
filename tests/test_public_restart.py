"""Personal /restart must never restart the service or disturb other users."""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from luckyjet_password_gate import (
    TelegramPasswordGate, RESTART_COOLDOWN_KEY, RESTART_COOLDOWN_SECONDS,
)


def incoming(uid, message_id=123):
    return {"message": {
        "message_id": message_id, "chat": {"id": uid, "type": "private"},
        "from": {"id": uid}, "text": "/restart",
    }}


def confirmation(uid, callback_data, message_id=124):
    return {
        "id": "cb-" + str(uid), "from": {"id": uid},
        "message": {"message_id": message_id, "chat": {"id": uid, "type": "private"}},
        "data": callback_data,
    }


def test_personal_restart_stops_only_own_demo_and_preserves_balances_histories_and_settings():
    from fakeredis.aioredis import FakeRedis

    async def run():
        calls = []
        next_id = 124

        async def request(method, payload):
            nonlocal next_id
            calls.append((method, payload))
            next_id += 1
            return {"ok": True, "result": {"message_id": next_id}}

        gate = TelegramPasswordGate(request, password="fixture", admin_chat_id=1, history_provider=lambda: [])
        gate.redis = FakeRedis(decode_responses=True)
        gate.authorized.update({22, 44})
        gate.chats.update({22: 22, 44: 44})
        gate.notifications_disabled.add(22)
        gate.history_hidden.add(22)
        gate.thresholds[22] = 8

        first = gate._demo_ledger(22)
        first.enabled = True
        first.active = True
        first.balance_cents = 1_050_000
        first.cycle_start_balance_cents = 1_055_000
        first.cycle_stake_cents = 5_000
        first.cycle_rounds = ["1.25x", "1.50x"]
        first.cycle_started_at = "2026-11-07T00:00:00+00:00"
        first.history = [{"outcome": "recovered", "net_cents": 1000}]
        first.daily_max_steps = {"2026-11-06": 7}
        first.tracker_step = 2

        second = gate._demo_ledger(44)
        second.enabled = True
        second.active = True
        second.balance_cents = 1_200_000
        second.history = [{"outcome": "recovered", "net_cents": 2000}]
        before_other = second.dump().copy()

        await gate._persist_demo(22)
        await gate._persist_demo(44)
        await gate.redis.set("luckyjet:state", "history and coefficients")
        await gate.redis.hset("luckyjet:thresholds", "22", "8")
        await gate.redis.sadd("luckyjet:authorized_users", 22, 44)

        await gate.handle_update(incoming(22))
        prompt = [body for method, body in calls if method == "sendMessage"][-1]
        assert "только твой интерфейс" in prompt["text"]
        assert "Mini App и другие пользователи не затрагиваются" in prompt["text"]
        confirm = prompt["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        cancel = prompt["reply_markup"]["inline_keyboard"][1][0]["callback_data"]
        await gate.handle_callback(confirmation(44, confirm))
        assert await gate.redis.get(f"{RESTART_COOLDOWN_KEY}:22") is None
        await gate.handle_callback(confirmation(22, cancel))
        assert first.active and first.enabled
        await gate.handle_update(incoming(22, 130))
        confirm = [body for method, body in calls if method == "sendMessage"][-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        await gate.handle_callback(confirmation(22, confirm))

        assert first.enabled is False
        assert first.active is False
        assert first.balance_cents == 1_050_000
        assert first.history[0]["outcome"] == "stopped_by_user"
        assert first.history[0]["balance_after_cents"] == 1_050_000
        assert first.history[1]["outcome"] == "recovered"
        assert first.daily_max_steps["2026-11-06"] == 7
        assert first.tracker_step == 2  # A bot UI restart must not reset always-on tracking.
        assert second.dump() == before_other
        assert gate.history_hidden == {22}
        assert 22 in gate.notifications_disabled
        assert gate.authorized == {22, 44}
        assert gate.redis is not None
        assert await gate.redis.get("luckyjet:state") == "history and coefficients"
        assert await gate.redis.hget("luckyjet:thresholds", "22") == "8"
        saved = json.loads(await gate.redis.hget("luckyjet:demo_sessions", "22"))
        assert saved["enabled"] is False and saved["active"] is False
        assert saved["balance_cents"] == 1_050_000
        assert saved["tracker_step"] == 2
        assert saved["history"][0]["outcome"] == "stopped_by_user"
        assert json.loads(await gate.redis.hget("luckyjet:demo_sessions", "44"))["enabled"] is True
        assert 0 < await gate.redis.ttl(f"{RESTART_COOLDOWN_KEY}:22") <= RESTART_COOLDOWN_SECONDS
        assert await gate.redis.get(f"{RESTART_COOLDOWN_KEY}:44") is None

        # A second user can restart immediately: cooldown is not global.
        await gate.handle_update(incoming(44))
        user44_prompt = [p for m, p in calls if m == "sendMessage"][-1]
        assert user44_prompt["reply_markup"]["inline_keyboard"][0][0]["callback_data"].startswith("restart_confirm:")
        # The first user remains stopped when new rounds are received.
        await gate.on_round("1.25x")
        assert first.enabled is False and first.balance_cents == 1_050_000
        await gate.close()

    asyncio.run(run())


def test_personal_restart_expiration_blocked_user_and_no_redis(monkeypatch):
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

        await gate.handle_update(incoming(42))
        token = gate._restart_pending[42][0]
        expired = gate._restart_pending[42][1] + 1
        monkeypatch.setattr(luckyjet_password_gate.time, "monotonic", lambda: expired)
        await gate.handle_callback(confirmation(42, "restart_confirm:" + token))
        assert await gate.redis.get(f"{RESTART_COOLDOWN_KEY}:42") is None

        await gate.close()
        new_gate = TelegramPasswordGate(request, password="fixture")
        await new_gate.handle_update(incoming(42))
        assert "нет подключения к Redis" in [b for m, b in calls if m == "sendMessage"][-1]["text"]

    asyncio.run(run())


def test_personal_restart_does_not_stop_polling(monkeypatch):
    from fakeredis.aioredis import FakeRedis
    from release_reset import MARKER_KEY, RELEASE_ID, ACCESS_MARKER_KEY, ACCESS_RESET_ID
    import redis.asyncio

    async def run():
        fake = FakeRedis(decode_responses=True)
        await fake.set(MARKER_KEY, RELEASE_ID)
        await fake.set(ACCESS_MARKER_KEY, ACCESS_RESET_ID)
        await fake.set("luckyjet:state", "retain")
        monkeypatch.setattr(redis.asyncio, "from_url", lambda *args, **kwargs: fake)

        sent = []
        polls = [0]
        async def request(method, payload):
            sent.append((method, payload))
            if method == "getUpdates":
                polls[0] += 1
                if polls[0] == 1:
                    return {"result": [{"update_id": 1, **incoming(55)}]}
                if polls[0] == 2:
                    buttons = [v["reply_markup"]["inline_keyboard"] for m, v in sent
                               if m == "sendMessage" and "reply_markup" in v]
                    callback = buttons[-1][0][0]["callback_data"]
                    return {"result": [{"update_id": 2, "callback_query": confirmation(55, callback)}]}
                raise asyncio.CancelledError()
            return {"ok": True, "result": {"message_id": 150}}

        gate = TelegramPasswordGate(request, password="fixture")
        gate.redis_url = "redis://fixture"
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(gate.start(), timeout=3)
        commands = [body["commands"] for name, body in sent if name == "setMyCommands"]
        assert any(x["command"] == "restart" for x in commands[0])
        assert polls[0] == 3
        assert await fake.get("luckyjet:state") == "retain"
        assert await fake.get(f"{RESTART_COOLDOWN_KEY}:55") == "1"

    asyncio.run(run())
