"""Execute the release Lua script with an isolated Redis-compatible Lua engine."""
import asyncio
import json

import pytest

fakeredis = pytest.importorskip("fakeredis.aioredis")
pytest.importorskip("lupa")

from release_reset import MARKER_KEY, RELEASE_ID, apply_release_reset
from luckyjet_signal.demo_ledger import DemoLedger
from luckyjet_reliable_gate import ReliableTelegramPasswordGate


def test_reset_covers_all_users_preserves_settings_and_runs_only_once():
    async def run():
        redis = fakeredis.FakeRedis(decode_responses=True)
        await redis.sadd("luckyjet:authorized_users", "42", "99")
        await redis.sadd("luckyjet:history_hidden", "42")
        await redis.set("luckyjet:state", "market history")
        for uid in ("42", "99"):
            ledger = DemoLedger(enabled=True, trigger_streak=6, base_stake_cents=1000)
            ledger.balance_cents = 123
            ledger.active = True
            ledger.history = [{"net_cents": -1000}]
            await redis.hset("luckyjet:demo_sessions", uid, json.dumps(ledger.dump()))
        assert await apply_release_reset(redis) is True
        assert not await redis.smembers("luckyjet:authorized_users")
        assert not await redis.smembers("luckyjet:history_hidden")
        for raw in (await redis.hgetall("luckyjet:demo_sessions")).values():
            ledger = DemoLedger(**json.loads(raw))
            assert ledger.balance_cents == 1_000_000
            assert not ledger.enabled and not ledger.active and not ledger.history
            assert ledger.trigger_streak == 6 and ledger.base_stake_cents == 1000
        assert await redis.get("luckyjet:state") == "market history"
        assert await redis.get(MARKER_KEY) == RELEASE_ID
        await redis.sadd("luckyjet:authorized_users", "42")
        await redis.hset("luckyjet:demo_sessions", "42", "new stats")
        assert await apply_release_reset(redis) is False
        assert await redis.sismember("luckyjet:authorized_users", "42")
        assert await redis.hget("luckyjet:demo_sessions", "42") == "new stats"
        await redis.aclose()
    asyncio.run(run())


def test_malformed_legacy_session_is_reset_without_blocking_release():
    async def run():
        redis = fakeredis.FakeRedis(decode_responses=True)
        await redis.hset("luckyjet:demo_sessions", "42", "broken JSON")
        assert await apply_release_reset(redis)
        ledger = DemoLedger(**json.loads(await redis.hget("luckyjet:demo_sessions", "42")))
        assert ledger.balance_cents == 1_000_000
        await redis.aclose()
    asyncio.run(run())


def test_admin_must_reenter_password_after_release_then_restart_keeps_login():
    async def run():
        redis = fakeredis.FakeRedis(decode_responses=True)
        await redis.sadd("luckyjet:authorized_users", "42")
        await redis.hset("luckyjet:chats", "42", "42")
        await apply_release_reset(redis)
        calls = []
        async def telegram(method, payload):
            calls.append((method, payload))
            return {"result": {"message_id": 100 + len(calls)}} if method == "sendMessage" else {"result": True}
        gate = ReliableTelegramPasswordGate(telegram, password="test-secret", admin_chat_id=42)
        gate.redis = redis
        await gate._load_persisted_state()
        assert gate.is_admin(42) and not await gate.is_authorized(42)
        def msg(text):
            return {"message": {"message_id": 1, "text": text, "chat": {"id": 42, "type": "private"}, "from": {"id": 42}}}
        await gate.handle_update(msg("/start"))
        await gate.handle_update(msg("test-secret"))
        assert gate.authorized == {42}
        assert 42 in gate.history_messages and 42 in gate.menu_messages
        assert not await apply_release_reset(redis)
        restored = ReliableTelegramPasswordGate(telegram, password="test-secret", admin_chat_id=42)
        restored.redis = redis
        await restored._load_persisted_state()
        assert restored.authorized == {42}
        assert restored._demo_ledger(42).balance_cents == 1_000_000
        await redis.aclose()
    asyncio.run(run())
