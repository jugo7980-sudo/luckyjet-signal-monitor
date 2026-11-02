import asyncio
import time

import pytest

from luckyjet_reliable_gate import ReliableTelegramPasswordGate


class Telegram:
    def __init__(self):
        self.calls = []
        self.next_id = 100

    async def __call__(self, method, payload):
        self.calls.append((method, payload))
        if method == "sendMessage":
            self.next_id += 1
            return {"result": {"message_id": self.next_id}}
        return {"result": True}


def setup():
    request = Telegram()
    gate = ReliableTelegramPasswordGate(request)
    gate.authorized.add(42)
    gate.chats[42] = 42
    gate.history_hidden.add(42)
    return gate, request


@pytest.mark.parametrize("threshold", [2, 3, 4, 5, 6, 10])
def test_one_notice_at_threshold_updates_same_id_and_schedules_five_seconds(threshold):
    gate, request = setup()
    gate.thresholds[42] = threshold
    async def run():
        for _ in range(threshold - 1):
            await gate.on_round("1.20x")
        assert not any(m == "sendMessage" for m, _ in request.calls)
        await gate.on_round("1.30x")
        notice_id = gate.active_messages[42]
        for value in ("1.40x", "1.50x"):
            await gate.on_round(value)
            assert gate.active_messages[42] == notice_id
        before = time.time()
        await gate.on_round("2.00x")
        assert 42 not in gate.active_messages
        mid, deadline = gate.pending_deletions[42]
        assert mid == notice_id and before + 5 <= deadline <= time.time() + 5
        await gate.close()
        # Cancelled timers keep their persisted deadline for a restart.
        assert gate.pending_deletions[42] == (mid, deadline)
    asyncio.run(run())
    assert len([1 for m, _ in request.calls if m == "sendMessage"]) == 1
    edits = [p for m, p in request.calls if m == "editMessageReplyMarkup"]
    assert len(edits) == 3 and {p["message_id"] for p in edits} == {101}


def test_expired_notice_is_deleted_and_new_series_cannot_be_deleted_by_old_timer(monkeypatch):
    gate, request = setup()
    gate.thresholds[42] = 3
    async def run():
        for _ in range(3):
            await gate.on_round("1.20x")
        await gate.on_round("2.00x")
        first_id, first_deadline = gate.pending_deletions[42]
        for _ in range(3):
            await gate.on_round("1.30x")
        second_id = gate.active_messages[42]
        assert second_id != first_id
        monkeypatch.setattr(time, "time", lambda: first_deadline + 1)
        await gate._resume_streak_delete(42, first_id, first_deadline)
        assert gate.active_messages[42] == second_id
        assert not any(m == "deleteMessage" and p["message_id"] == second_id for m, p in request.calls)
        await gate.on_round("2.10x")
        mid, deadline = gate.pending_deletions[42]
        monkeypatch.setattr(time, "time", lambda: deadline + 1)
        await gate._resume_streak_delete(42, mid, deadline)
        assert 42 not in gate.pending_deletions
        assert any(m == "deleteMessage" and p["message_id"] == mid for m, p in request.calls)
        await gate.close()
    asyncio.run(run())


def test_sound_and_notification_toggle_are_applied_without_demo_spam():
    gate, request = setup()
    gate.silent_users.add(42)
    async def run():
        for _ in range(4):
            await gate.on_round("1.20x")
        notice_id = gate.active_messages[42]
        notice = [p for m, p in request.calls if m == "sendMessage"][0]
        assert notice["disable_notification"] is True
        await gate.handle_callback({"id": "off", "data": "settings_notifications", "from": {"id": 42},
                                    "message": {"message_id": 999, "chat": {"id": 42}}})
        assert 42 in gate.notifications_disabled and 42 not in gate.active_messages
        for _ in range(10):
            await gate.on_round("1.10x")
        assert len([1 for m, _ in request.calls if m == "sendMessage"]) == 1
        assert any(m == "deleteMessage" and p["message_id"] == notice_id for m, p in request.calls)
        await gate.close()
    asyncio.run(run())


def test_legacy_durations_and_buttons_use_five_seconds():
    gate, request = setup()
    async def run():
        for value in (10, 15):
            gate.delete_seconds[42] = value
            assert gate.user_delete_seconds(42) == 5
            await gate.handle_callback({"id": str(value), "data": f"delete:{value}", "from": {"id": 42},
                                       "message": {"message_id": 999, "chat": {"id": 42}}})
            assert gate.delete_seconds[42] == 5
    asyncio.run(run())
    text = str(gate.settings_menu(42))
    assert "5 сек." in text and "10 сек." not in text
    _, choices = gate._choice_screen(42, "settings_delete")
    buttons = [b for row in choices["inline_keyboard"] for b in row]
    assert any(b["callback_data"] == "delete:5" for b in buttons)
    assert not any(b["callback_data"] == "delete:10" for b in buttons)


def test_redis_migration_and_restart_resume_notice_without_duplicate():
    fakeredis = pytest.importorskip("fakeredis.aioredis")
    async def run():
        gate, request = setup()
        redis = fakeredis.FakeRedis(decode_responses=True)
        gate.redis = redis
        await redis.hset("luckyjet:delete_seconds", "42", "10")
        gate.current_streak = ["1.20x"] * 4
        await gate._persist_user(42)
        # Seed legacy value after persist to test loading/migration itself.
        await redis.hset("luckyjet:delete_seconds", "42", "10")
        await gate._update_streak_notice(42)
        old_id = gate.active_messages[42]
        restored = ReliableTelegramPasswordGate(request)
        restored.redis = redis
        restored.current_streak = ["1.20x"] * 4
        await restored._load_persisted_state()
        assert restored.active_messages[42] == old_id
        assert await redis.hget("luckyjet:delete_seconds", "42") == "5"
        assert len([1 for m, _ in request.calls if m == "sendMessage"]) == 1
        await restored.on_round("2.00x")
        saved = await redis.hget("luckyjet:pending_delete_message", "42")
        assert saved == str(old_id)
        # Cancel timer as a real shutdown would; deadline stays in Redis.
        for task in list(restored._deletion_tasks):
            task.cancel()
        await asyncio.gather(*restored._deletion_tasks, return_exceptions=True)
        assert await redis.hget("luckyjet:pending_delete_message", "42") == str(old_id)
        await restored.restore_runtime_state(now=time.time() + 6)
        assert 42 not in restored.pending_deletions
        assert await redis.hget("luckyjet:pending_delete_message", "42") is None
        await redis.aclose()
    asyncio.run(run())
