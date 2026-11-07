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
        assert not [x for x in calls if x[0] == "pinChatMessage"]
        assert not [x for x in calls if x[0] == "sendMessage"]
        cmd_list = [p["commands"] for m,p in calls if m == "setMyCommands"][0]
        assert [x["command"] for x in cmd_list] == ["app", "start", "menu", "status", "settings", "restart"]
        assert cmd_list[0]["description"] == "🚀 ОТКРЫТЬ ПРИЛОЖЕНИЕ"
        assert any(m == "getChatMenuButton" and p == {"chat_id":42} for m,p in calls)
    asyncio.run(run())


def test_app_command_is_public_and_does_not_create_any_pin():
    from fakeredis.aioredis import FakeRedis
    from luckyjet_password_gate import APP_LAUNCH_TTL_SECONDS

    async def run():
        database = FakeRedis(decode_responses=True)
        calls = []
        async def request(method, payload):
            calls.append((method, payload))
            return {"ok": True, "result": {"message_id": 456}}
        gate = TelegramPasswordGate(request, password="test")
        gate.redis = database
        await gate.handle_update({"message": {
            "message_id": 21, "chat": {"id": 42, "type": "private"},
            "from": {"id": 42}, "text": "/app",
        }})
        assert any(method == "setChatMenuButton" for method, _ in calls)
        assert ("deleteMessage", {"chat_id": 42, "message_id": 21}) in calls
        launch = [payload for method, payload in calls if method == "sendMessage"]
        assert len(launch) == 1
        assert launch[0]["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"] == app_url()
        assert not [method for method, _ in calls if "pinChatMessage" in method]
        assert not await database.hgetall("luckyjet:pinned_app_messages")
        assert APP_LAUNCH_TTL_SECONDS <= 60
        await gate.close()
    asyncio.run(run())


def test_start_for_new_unregistered_user_repairs_menu_without_pin():
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
        assert not any(m in {"pinChatMessage", "sendMessage"} for m,p in calls)
        assert not await gate.redis.hgetall("luckyjet:pinned_app_messages")
        await gate.close()
    asyncio.run(run())
