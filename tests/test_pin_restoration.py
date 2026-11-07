"""Migration: remove old pinned Mini App launchers and keep command launch transient."""
import asyncio
import sys
from pathlib import Path

import httpx
from fakeredis.aioredis import FakeRedis

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from luckyjet_password_gate import APP_PIN_KEY, APP_PIN_REFRESH_KEY, TelegramPasswordGate


class FakeTelegram:
    def __init__(self):
        self.calls = []
        self.messages = {}
        self.pinned = {}
        self.last_id = 900
        self.error_on_unpin = False

    async def __call__(self, method, payload):
        self.calls.append((method, payload))
        chat_id = payload.get("chat_id")
        if method == "getChat":
            message_id = self.pinned.get(chat_id)
            pinned = self.messages.get((chat_id, message_id))
            return {"ok": True, "result": {
                "id": chat_id, "type": "private",
                **({"pinned_message": {"message_id": message_id, "text": pinned}}
                   if pinned is not None else {}),
            }}
        if method == "sendMessage":
            self.last_id += 1
            self.messages[(chat_id, self.last_id)] = payload["text"]
            return {"ok": True, "result": {"message_id": self.last_id}}
        if method == "unpinChatMessage":
            if self.error_on_unpin:
                raise httpx.ConnectError("Network unavailable")
            if self.pinned.get(chat_id) == payload["message_id"]:
                self.pinned.pop(chat_id, None)
            return {"ok": True, "result": True}
        if method == "deleteMessage":
            self.messages.pop((chat_id, payload["message_id"]), None)
            return {"ok": True, "result": True}
        return {"ok": True, "result": True}

    def count(self, method):
        return sum(method == name for name, _ in self.calls)


def test_migration_removes_old_bot_pin_but_not_unrelated_pins_or_history():
    async def run():
        redis = FakeRedis(decode_responses=True)
        telegram = FakeTelegram()
        telegram.messages[(42, 300)] = "Lucky Jet · приложение"
        telegram.pinned[42] = 300
        telegram.messages[(43, 333)] = "User's pinned personal note"
        telegram.pinned[43] = 333
        telegram.messages[(43, 301)] = "Lucky Jet · приложение"
        await redis.hset(APP_PIN_KEY, mapping={"42": "300", "43": "301"})
        await redis.sadd(APP_PIN_REFRESH_KEY, "42", "43")
        await redis.set("luckyjet:state", "history preserved")
        await redis.hset("luckyjet:demo_sessions", "42", '{"balance_cents":1000000}')
        gate = TelegramPasswordGate(telegram, password="test")
        gate.redis = redis
        await gate._clean_legacy_pinned_launchers({42, 43})
        assert telegram.pinned.get(42) is None
        assert telegram.pinned[43] == 333
        assert telegram.messages[(43, 333)] == "User's pinned personal note"
        assert (42, 300) not in telegram.messages
        assert (43, 301) not in telegram.messages
        assert not await redis.hgetall(APP_PIN_KEY)
        assert not await redis.exists(APP_PIN_REFRESH_KEY)
        assert await redis.get("luckyjet:state") == "history preserved"
        assert await redis.hget("luckyjet:demo_sessions", "42") == '{"balance_cents":1000000}'
        original_count = len(telegram.calls)
        await gate._clean_legacy_pinned_launchers({42, 43})
        assert telegram.count("sendMessage") == 0
        assert telegram.count("pinChatMessage") == 0
        assert len(telegram.calls) > original_count  # only checks, no new posts
        await gate.close()
    asyncio.run(run())


def test_unrecorded_bot_pin_is_removed_when_chat_is_known():
    async def run():
        redis = FakeRedis(decode_responses=True)
        telegram = FakeTelegram()
        telegram.pinned[77] = 888
        telegram.messages[(77, 888)] = "Lucky Jet · приложение"
        gate = TelegramPasswordGate(telegram, password="test")
        gate.redis = redis
        await gate._clean_legacy_pinned_launchers({77})
        assert not telegram.pinned
        assert (77, 888) not in telegram.messages
        assert telegram.count("sendMessage") == 0
        await gate.close()
    asyncio.run(run())


def test_temporary_app_button_disappears_without_pin(monkeypatch):
    import luckyjet_password_gate as module
    monkeypatch.setattr(module, "APP_LAUNCH_TTL_SECONDS", 0.015)

    async def run():
        telegram = FakeTelegram()
        gate = TelegramPasswordGate(telegram, password="test")
        await gate._show_app_launcher(55)
        assert telegram.count("sendMessage") == 1
        assert telegram.count("pinChatMessage") == 0
        assert telegram.messages[(55, 901)] == "Открыть Lucky Jet:"
        await asyncio.wait_for(_until(lambda: (55, 901) not in telegram.messages), timeout=2)
        await gate.close()
    asyncio.run(run())


def test_legacy_pin_service_notice_is_deleted_not_recreated():
    async def run():
        telegram = FakeTelegram()
        gate = TelegramPasswordGate(telegram, password="test")
        await gate.handle_update({"message": {
            "message_id": 399, "chat": {"type": "private", "id": 55},
            "from": {"id": 55},
            "pinned_message": {"text": "Lucky Jet · приложение", "message_id": 300},
        }})
        assert ("deleteMessage", {"chat_id": 55, "message_id": 399}) in telegram.calls
        assert telegram.count("sendMessage") == 0
        assert telegram.count("pinChatMessage") == 0
        await gate.close()
    asyncio.run(run())


def test_transient_telegram_error_keeps_legacy_record_for_next_start():
    async def run():
        redis = FakeRedis(decode_responses=True)
        telegram = FakeTelegram()
        telegram.error_on_unpin = True
        await redis.hset(APP_PIN_KEY, "55", "42")
        gate = TelegramPasswordGate(telegram, password="test")
        gate.redis = redis
        await gate._clean_legacy_pinned_launchers({55})
        assert await redis.hget(APP_PIN_KEY, "55") == "42"
        telegram.error_on_unpin = False
        await gate._clean_legacy_pinned_launchers({55})
        assert not await redis.hget(APP_PIN_KEY, "55")
        await gate.close()
    asyncio.run(run())


async def _until(predicate):
    while not predicate():
        await asyncio.sleep(0.005)
