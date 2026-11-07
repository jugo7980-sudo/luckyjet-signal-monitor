"""Execute the release Lua script with an isolated Redis-compatible Lua engine."""
import asyncio
import json

import pytest

fakeredis = pytest.importorskip("fakeredis.aioredis")
pytest.importorskip("lupa")

from release_reset import MARKER_KEY, RELEASE_ID, apply_release_reset, apply_access_reset, ACCESS_MARKER_KEY, ACCESS_RESET_ID
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


def test_auth_only_reset_revokes_everyone_including_admin_but_preserves_all_data():
    async def run():
        import hashlib
        import secrets
        from mini_app_access import cookie_user, _status, _redis_session_key
        redis = fakeredis.FakeRedis(decode_responses=True)
        await redis.sadd("luckyjet:authorized_users", "42", "99", "101")
        await redis.hset("luckyjet:web_registered", mapping={
            "42": '{"id":42,"username":"owner"}',
            "99": '{"id":99,"username":"guest"}',
        })
        old_cookie = secrets.token_urlsafe(32)
        # Before this release, browser sessions used the unversioned namespace.
        old_redis_key = "luckyjet:web_session:" + hashlib.sha256(old_cookie.encode()).hexdigest()
        await redis.setex(old_redis_key, 2600000, "42")
        protected = {
            "luckyjet:blocked_users": ("set", ["101"]),
            "luckyjet:history_hidden": ("set", ["42"]),
            "luckyjet:settings_marker": ("string", "saved settings"),
            "luckyjet:state": ("string", "preserved coefficients"),
        }
        for key, (kind, value) in protected.items():
            if kind == "set":
                await redis.sadd(key, *value)
            else:
                await redis.set(key, value)
        await redis.hset("luckyjet:demo_sessions", mapping={
            "42": '{"balance_cents":123400,"enabled":true}',
            "99": '{"balance_cents":50400,"enabled":true}',
        })
        await redis.hset("luckyjet:thresholds", mapping={"42": "5", "99": "6"})
        assert await apply_access_reset(redis) is True
        assert await redis.get(ACCESS_MARKER_KEY) == ACCESS_RESET_ID
        assert await redis.scard("luckyjet:authorized_users") == 0
        assert await redis.hlen("luckyjet:web_registered") == 0
        assert await redis.get("luckyjet:state") == "preserved coefficients"
        assert await redis.get("luckyjet:settings_marker") == "saved settings"
        assert await redis.sismember("luckyjet:blocked_users", "101")
        assert await redis.sismember("luckyjet:history_hidden", "42")
        assert await redis.hget("luckyjet:thresholds", "42") == "5"
        assert '"balance_cents":123400' in await redis.hget("luckyjet:demo_sessions", "42")
        assert '"balance_cents":50400' in await redis.hget("luckyjet:demo_sessions", "99")
        # Old 30-day browser cookies are still in Redis until TTL, but not recognized.
        assert await redis.exists(old_redis_key)
        assert old_redis_key != _redis_session_key(old_cookie)
        gate = ReliableTelegramPasswordGate(None, admin_chat_id=42, password="fixture")
        gate.redis = redis
        await gate._load_persisted_state()
        assert not gate.authorized
        assert gate.is_admin(42)
        assert not (await _status(gate, {"id":42,"username":"owner","first_name":"Owner"}))["premium"]
        assert not (await _status(gate, {"id":99,"username":"guest","first_name":"Guest"}))["premium"]
        assert await cookie_user("lj_session="+old_cookie, gate) is None
        # A later restart must keep new Premium grants rather than resetting again.
        await redis.sadd("luckyjet:authorized_users", "99")
        await redis.hset("luckyjet:web_registered", "99", '{"id":99}')
        assert await apply_access_reset(redis) is False
        assert await redis.sismember("luckyjet:authorized_users", "99")
        assert await redis.hget("luckyjet:web_registered", "99") is not None
        await gate.close()
    asyncio.run(run())
