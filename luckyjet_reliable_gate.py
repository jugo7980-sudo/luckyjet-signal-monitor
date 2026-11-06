"""Restart-safe extension for the private Telegram bot UI."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import logging
import time

import httpx

from luckyjet_password_gate import TelegramPasswordGate
from demo_persistence import DemoPersistence


_demo_batch = ContextVar("demo_batch", default=None)

log = logging.getLogger("luckyjet.reliable_gate")


class ReliableTelegramPasswordGate(TelegramPasswordGate):
    """Persist transient message ids so restarts do not leave chat clutter."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pending_deletions = {}
        self._demo_storage = DemoPersistence()
        self._runtime_saved = {}
        self._runtime_save_locks = {}

    @staticmethod
    def _telegram_description(exc):
        try:
            return str(exc.response.json().get("description", ""))
        except Exception:
            return ""

    async def _ensure_private_history(self, user_id):
        # Recreate only a confirmed missing message, never after a network error.
        return await super()._ensure_private_history(user_id)

    async def _load_persisted_state(self):
        await super()._load_persisted_state()
        stats = getattr(self, "coefficient_stats", None)
        if stats is not None:
            stats.redis = self.redis
            await stats.bootstrap()
        if self.redis:
            await self._demo_storage.restore(self.redis, self.demo_ledgers)
            self.menu_messages = {
                int(k): int(v)
                for k, v in (await self.redis.hgetall("luckyjet:menu_messages")).items()
            }
            self.menu_deadlines = {
                int(k): float(v)
                for k, v in (await self.redis.hgetall("luckyjet:menu_deadlines")).items()
            }
            self.active_messages = {
                int(k): int(v)
                for k, v in (await self.redis.hgetall("luckyjet:active_messages")).items()
            }
            deletion_ids = await self.redis.hgetall("luckyjet:pending_delete_message")
            deletion_deadlines = await self.redis.hgetall("luckyjet:pending_delete_deadline")
            self.pending_deletions = {
                int(uid): (int(message_id), float(deletion_deadlines[uid]))
                for uid, message_id in deletion_ids.items()
                if uid in deletion_deadlines
            }

        if self.site_account is not None and hasattr(self.site_account, "cleanup"):
            await self.site_account.cleanup.restore()
        await self.restore_runtime_state()
        notices = getattr(self, "notification_queue", None)
        if notices is not None:
            await notices.restore()

    async def _persist_runtime_user(self, user_id):
        if not self.redis:
            return

        user_id = int(user_id)
        lock = self._runtime_save_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            # Only acknowledge a snapshot after Redis confirms the write. The
            # per-user lock also prevents an older save overwriting a newer one.
            snapshot = (
                self.redis,
                self.menu_messages.get(user_id),
                self.menu_deadlines.get(user_id) if user_id in self.menu_messages else None,
                self.active_messages.get(user_id),
                self.pending_deletions.get(user_id),
            )
            if self._runtime_saved.get(user_id) == snapshot:
                return
            uid = str(user_id)
            pipe = self.redis.pipeline()

            if user_id in self.menu_messages:
                pipe.hset("luckyjet:menu_messages", uid, str(self.menu_messages[user_id]))
            else:
                pipe.hdel("luckyjet:menu_messages", uid)

            if user_id in self.menu_messages and user_id in self.menu_deadlines:
                pipe.hset("luckyjet:menu_deadlines", uid, repr(self.menu_deadlines[user_id]))
            else:
                pipe.hdel("luckyjet:menu_deadlines", uid)

            if user_id in self.active_messages:
                pipe.hset("luckyjet:active_messages", uid, str(self.active_messages[user_id]))
            else:
                pipe.hdel("luckyjet:active_messages", uid)

            pending = self.pending_deletions.get(user_id)
            if pending:
                message_id, deadline = pending
                pipe.hset("luckyjet:pending_delete_message", uid, str(message_id))
                pipe.hset("luckyjet:pending_delete_deadline", uid, repr(float(deadline)))
            else:
                pipe.hdel("luckyjet:pending_delete_message", uid)
                pipe.hdel("luckyjet:pending_delete_deadline", uid)

            await pipe.execute()
            self._runtime_saved[user_id] = snapshot

    async def _persist_demo(self, user_id):
        if self.redis:
            batch = _demo_batch.get()
            if batch is not None and batch[0] is self:
                batch[1][str(int(user_id))] = self._demo_ledger(user_id)
            else:
                await self._demo_storage.save(self.redis, user_id, self._demo_ledger(user_id))

    async def _persist_user(self, user_id):
        await super()._persist_user(user_id)
        await self._persist_runtime_user(user_id)

    async def _send_transient(self, user_id, chat_id, text, reply_markup):
        await super()._send_transient(user_id, chat_id, text, reply_markup)
        await self._persist_runtime_user(user_id)

    async def _delete_transient_source(self, user_id, chat_id, callback):
        await super()._delete_transient_source(user_id, chat_id, callback)
        await self._persist_runtime_user(user_id)

    async def _revoke_user(self, user_id, *, block):
        await self._clear_streak_notice(int(user_id))
        await super()._revoke_user(user_id, block=block)
        await self._persist_runtime_user(user_id)

    async def on_round(self, coefficient):
        batch = {}
        token = _demo_batch.set((self, batch))
        try:
            await super().on_round(coefficient)
        finally:
            _demo_batch.reset(token)
            if self.redis and batch:
                await self._demo_storage.save_many(self.redis, batch)
        users = set(self.chats) | set(self.active_messages) | set(self.pending_deletions)
        for user_id in users:
            await self._persist_runtime_user(user_id)

    async def restore_runtime_state(self, *, now=None):
        """Resume menu timers and known notices without creating new notifications."""
        now = time.time() if now is None else float(now)
        for user_id in list(self.menu_messages):
            if user_id not in self.authorized or user_id not in self.chats:
                await self._close_menu(user_id)
                continue
            if user_id not in self.menu_deadlines:
                self._touch_menu(user_id)
                await self._persist_runtime_user(user_id)
            deadline = self.menu_deadlines[user_id]
            if deadline == 0:
                self.history_open.add(user_id)
                continue
            if deadline <= now:
                await self._close_menu(user_id, expected_deadline=deadline, now=now)
            else:
                self._start_menu_timer(user_id)
        for user_id, (message_id, deadline) in list(self.pending_deletions.items()):
            if user_id not in self.authorized or user_id not in self.chats or user_id in self.notifications_disabled:
                await self._clear_streak_notice(user_id)
            elif deadline and deadline <= now:
                await self._safe_delete(self.chats.get(user_id), message_id)
                self.pending_deletions.pop(user_id, None)
                await self._persist_runtime_user(user_id)
            elif deadline:
                self._start_streak_deletion(user_id, message_id, deadline)
        for user_id in list(self.active_messages):
            if (user_id not in self.authorized or user_id in self.notifications_disabled
                    or len(self.current_streak) < self.user_threshold(user_id)):
                await self._clear_streak_notice(user_id)
            else:
                await self._update_streak_notice(user_id)
