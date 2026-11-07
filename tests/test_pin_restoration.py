"""Recover a pinned Mini App launcher after chat clearing and process restart."""
import asyncio
import sys
from pathlib import Path

import httpx
from fakeredis.aioredis import FakeRedis

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from luckyjet_password_gate import (
    APP_PIN_KEY, APP_PIN_REFRESH_KEY, TelegramPasswordGate,
)


class FakeTelegram:
    def __init__(self, *, current=None, next_id=900):
        self.current = current
        self.calls = []
        self.next_id = next_id
        self.failed_get = False
        self.invalid_old = set()
        self.fail_next_pin = False

    async def __call__(self, method, payload):
        self.calls.append((method, payload))
        if method == "getChat":
            if self.failed_get:
                raise httpx.ConnectError("Temporary Telegram timeout")
            info = {"id": payload["chat_id"], "type": "private"}
            if self.current:
                info["pinned_message"] = {"message_id": self.current}
            return {"ok": True, "result": info}
        if method == "sendMessage":
            self.next_id += 1
            return {"ok": True, "result": {"message_id": self.next_id}}
        if method == "pinChatMessage":
            target = payload["message_id"]
            if target in self.invalid_old:
                req = httpx.Request("POST", "https://api.telegram.org/botxxx/pinChatMessage")
                resp = httpx.Response(400, request=req,
                    json={"ok": False, "description": "Bad Request: message to pin not found"})
                raise httpx.HTTPStatusError("old pin deleted", request=req, response=resp)
            if self.fail_next_pin:
                self.fail_next_pin = False
                raise httpx.ConnectError("Telegram unavailable")
            self.current = target
            return {"ok": True, "result": True}
        if method == "unpinChatMessage":
            return {"ok": True, "result": True}
        if method == "deleteMessage":
            return {"ok": True, "result": True}
        return {"ok": True, "result": True}

    def count(self, method):
        return len([x for x in self.calls if x[0] == method])


def test_deleted_pin_recreated_and_saved_in_redis():
    async def run():
        tg = FakeTelegram(current=None)
        db = FakeRedis(decode_responses=True)
        await db.hset(APP_PIN_KEY, "42", "350")
        gate = TelegramPasswordGate(tg, password="test")
        gate.redis = db
        assert await gate._ensure_app_pin(42)
        assert tg.count("sendMessage") == 1
        assert tg.count("pinChatMessage") == 1
        assert tg.current == tg.next_id == 901
        assert await db.hget(APP_PIN_KEY, "42") == "901"
        assert await gate._ensure_app_pin(42)
        assert tg.count("sendMessage") == 1, "Do not send duplicates after recovery"
        await gate.close()
    asyncio.run(run())


def test_clear_local_history_repaired_by_next_start_even_with_stale_telegram_pin():
    async def run():
        tg = FakeTelegram(current=300)
        db = FakeRedis(decode_responses=True)
        await db.hset(APP_PIN_KEY, "42", "300")
        gate = TelegramPasswordGate(tg, password="test")
        gate.redis = db
        # Telegram might still report old pinned ID even after the client
        # has locally cleared the dialog: a normal audit would do nothing.
        assert await gate._ensure_app_pin(42)
        assert tg.count("sendMessage") == 0
        await gate._prepare_start(42, 42)
        assert tg.count("sendMessage") == 1
        assert tg.current == 901
        assert await db.hget(APP_PIN_KEY, "42") == "901"
        assert await db.sismember(APP_PIN_REFRESH_KEY, "42")
        assert tg.count("unpinChatMessage") == 1
        assert tg.count("deleteMessage") == 1
        assert await gate._ensure_app_pin(42)
        assert tg.count("sendMessage") == 1
        await gate.close()
    asyncio.run(run())


def test_deleted_cached_id_after_getchat_outage_is_replaced():
    async def run():
        tg = FakeTelegram(next_id=500)
        tg.failed_get = True
        tg.invalid_old.add(200)
        db = FakeRedis(decode_responses=True)
        await db.hset(APP_PIN_KEY, "42", "200")
        gate = TelegramPasswordGate(tg, password="test")
        gate.redis = db
        assert await gate._ensure_app_pin(42)
        assert tg.next_id == 501
        assert await db.hget(APP_PIN_KEY, "42") == "501"
        assert tg.count("pinChatMessage") == 2  # invalid old ID, then new ID
        await gate.close()
    asyncio.run(run())


def test_network_error_does_not_create_repeated_messages():
    async def run():
        tg = FakeTelegram(current=222)
        tg.failed_get = True
        db = FakeRedis(decode_responses=True)
        await db.hset(APP_PIN_KEY, "42", "222")
        gate = TelegramPasswordGate(tg, password="test")
        gate.redis = db
        assert await gate._ensure_app_pin(42)
        assert tg.count("sendMessage") == 0
        tg.fail_next_pin = True
        assert not await gate._ensure_app_pin(42)
        assert tg.count("sendMessage") == 0
        assert await db.hget(APP_PIN_KEY, "42") == "222"
        await gate.close()
    asyncio.run(run())


def test_failed_new_pin_keeps_cached_id_and_cleans_up_message():
    async def run():
        tg = FakeTelegram(current=None)
        tg.fail_next_pin = True
        db = FakeRedis(decode_responses=True)
        await db.hset(APP_PIN_KEY, "42", "100")
        gate = TelegramPasswordGate(tg, password="test")
        gate.redis = db
        assert not await gate._ensure_app_pin(42)
        assert await db.hget(APP_PIN_KEY, "42") == "100"
        assert tg.count("deleteMessage") == 1
        await gate.close()
    asyncio.run(run())


def test_concurrent_recovery_sends_only_one_replacement():
    async def run():
        tg = FakeTelegram(current=None)
        db = FakeRedis(decode_responses=True)
        await db.hset(APP_PIN_KEY, "42", "300")
        gate = TelegramPasswordGate(tg, password="test")
        gate.redis = db
        results = await asyncio.gather(*(gate._ensure_app_pin(42) for _ in range(8)))
        assert all(results)
        assert tg.count("sendMessage") == 1
        await gate.close()
    asyncio.run(run())


def test_periodic_audit_recovers_a_deleted_pin_without_user_message(monkeypatch):
    import luckyjet_password_gate as module
    monkeypatch.setattr(module, "APP_PIN_AUDIT_SECONDS", 0.015)

    async def run():
        tg = FakeTelegram(current=None)
        db = FakeRedis(decode_responses=True)
        await db.hset(APP_PIN_KEY, "42", "400")
        # This chat was already upgraded and its pin later disappeared.
        await db.sadd(APP_PIN_REFRESH_KEY, "42")
        gate = TelegramPasswordGate(tg, password="test")
        gate.redis = db
        gate.chats[42] = 42
        task = asyncio.create_task(gate._audit_app_pins())
        try:
            await asyncio.wait_for(_until(lambda: tg.count("sendMessage") == 1), timeout=2)
            assert tg.current == 901
            assert await db.hget(APP_PIN_KEY, "42") == "901"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await gate.close()
    asyncio.run(run())


async def _until(predicate):
    while not predicate():
        await asyncio.sleep(0.01)
