"""Telegram field menu must show commands; Mini App launcher belongs in pinned post."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from luckyjet_password_gate import TelegramPasswordGate
from mini_app import app_url


def test_global_and_existing_private_menu_overrides_reset_on_boot(monkeypatch):
    from fakeredis.aioredis import FakeRedis
    from release_reset import MARKER_KEY, RELEASE_ID, ACCESS_MARKER_KEY, ACCESS_RESET_ID
    import redis.asyncio

    async def run():
        database = FakeRedis(decode_responses=True)
        await database.set(MARKER_KEY, RELEASE_ID)
        await database.set(ACCESS_MARKER_KEY, ACCESS_RESET_ID)
        await database.hset("luckyjet:chats", mapping={"99": "99", "101": "101"})
        monkeypatch.setattr(redis.asyncio, "from_url", lambda *a, **kw: database)
        calls = []
        async def request(method, payload):
            calls.append((method, payload))
            if method == "getUpdates":
                raise asyncio.CancelledError()
            if method == "sendMessage":
                return {"ok": True, "result": {"message_id": 1000 + len(calls)}}
            if method == "getChatMenuButton":
                return {"ok": True, "result": {"type": "commands"}}
            return {"ok": True, "result": True}
        gate = TelegramPasswordGate(request, password="test", admin_chat_id=42)
        gate.redis_url = "redis://fixture"
        try:
            await gate.start()
        except asyncio.CancelledError:
            pass
        buttons = [p for m,p in calls if m == "setChatMenuButton"]
        assert len(buttons) == 4, buttons  # global + admin + two saved chats
        assert buttons[0] == {"menu_button": {"type": "commands"}}
        assert {p["chat_id"] for p in buttons[1:]} == {42, 99, 101}
        assert all(p["menu_button"] == {"type": "commands"} for p in buttons)
        assert len([x for x in calls if x[0] == "pinChatMessage"]) == 3
        assert all(
            p["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"] == app_url()
            for m,p in calls if m == "sendMessage"
        )
        cmd_list = [p["commands"] for m,p in calls if m == "setMyCommands"][0]
        assert [x["command"] for x in cmd_list] == ["start", "menu", "status", "settings", "restart"]
        assert any(m == "getChatMenuButton" and p == {"chat_id":42} for m,p in calls)
    asyncio.run(run())


def test_pinned_app_message_is_reused_after_restart_not_sent_twice():
    from fakeredis.aioredis import FakeRedis

    async def run():
        database = FakeRedis(decode_responses=True)
        calls = []
        pinned = {"id": None}
        async def request(method, payload):
            calls.append((method, payload))
            if method == "getChat":
                return {"ok": True, "result": {
                    "id": 42, "type": "private",
                    **({"pinned_message": {"message_id": pinned["id"]}}
                       if pinned["id"] else {}),
                }}
            if method == "pinChatMessage":
                pinned["id"] = payload["message_id"]
                return {"ok": True, "result": True}
            if method == "sendMessage":
                return {"ok": True, "result": {"message_id": 456}}
            return {"ok": True, "result": True}
        gate = TelegramPasswordGate(request, password="test")
        gate.redis = database
        await gate._ensure_app_pin(42)
        await gate._ensure_app_pin(42)
        assert len([x for x in calls if x[0] == "sendMessage"]) == 1
        assert await database.hget("luckyjet:pinned_app_messages", "42") == "456"
        assert len([x for x in calls if x[0] == "pinChatMessage"]) == 1
        await gate._register_commands_button(42)
        await gate._register_commands_button(42)
        assert len([x for x in calls if x[0] == "setChatMenuButton"]) == 1
        assert gate.menu(42)["inline_keyboard"][0][0]["callback_data"] == "menu_history"
        await gate.close()
    asyncio.run(run())


def test_start_for_new_unregistered_user_repairs_legacy_menu_and_creates_pin():
    from fakeredis.aioredis import FakeRedis

    async def run():
        calls = []
        async def request(method, payload):
            calls.append((method, payload))
            return {"ok":True,"result":{"message_id": 345}}
        gate = TelegramPasswordGate(request, password="test")
        gate.redis = FakeRedis(decode_responses=True)
        await gate._prepare_start(101, 101)
        assert ("setChatMenuButton", {"chat_id":101, "menu_button":{"type":"commands"}}) in calls
        assert any(m == "pinChatMessage" and p["chat_id"] == 101 for m,p in calls)
        assert await gate.redis.hget("luckyjet:pinned_app_messages", "101") == "345"
        await gate.close()
    asyncio.run(run())
