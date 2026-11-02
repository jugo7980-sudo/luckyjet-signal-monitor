"""Restart-safe extension for the private Telegram bot UI."""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from luckyjet_password_gate import TelegramPasswordGate


log = logging.getLogger("luckyjet.reliable_gate")


class ReliableTelegramPasswordGate(TelegramPasswordGate):
    """Persist transient message ids so restarts do not leave chat clutter."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pending_deletions = {}

    async def handle_update(self, update):
        """Keep normal handling, but make /start visibly open the bot for authorized users."""
        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        sender = msg.get("from") or {}
        user_id = self._to_int(sender.get("id"))
        chat_id = self._to_int(chat.get("id"))
        value = (msg.get("text") or "").strip()
        command = value.split("@", 1)[0].split(" ", 1)[0].lower()
        authorized_start = (
            chat.get("type") == "private"
            and user_id is not None
            and chat_id is not None
            and user_id in self.authorized
            and command == "/start"
        )

        await super().handle_update(update)

        if authorized_start and user_id in self.authorized:
            await self._send_transient(
                user_id,
                chat_id,
                "🎰 Lucky Jet Monitor\nВыберите раздел.",
                self.menu(user_id),
            )

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
        if self.redis:
            self.menu_messages = {
                int(k): int(v)
                for k, v in (await self.redis.hgetall("luckyjet:menu_messages")).items()
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

        await self.restore_runtime_state()

    async def _persist_runtime_user(self, user_id):
        if not self.redis:
            return

        user_id = int(user_id)
        uid = str(user_id)
        pipe = self.redis.pipeline()

        if user_id in self.menu_messages:
            pipe.hset("luckyjet:menu_messages", uid, str(self.menu_messages[user_id]))
        else:
            pipe.hdel("luckyjet:menu_messages", uid)

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
        self.pending_deletions.pop(int(user_id), None)
        await super()._revoke_user(user_id, block=block)
        await self._persist_runtime_user(user_id)

    async def on_round(self, coefficient):
        await super().on_round(coefficient)
        users = set(self.chats) | set(self.active_messages) | set(self.pending_deletions)
        for user_id in users:
            await self._persist_runtime_user(user_id)

    async def _schedule_delete(self, chat_id, message_id, delay):
        user_id = next(
            (
                uid
                for uid, active_message_id in self.active_messages.items()
                if active_message_id == message_id and self.chats.get(uid) == chat_id
            ),
            None,
        )

        if user_id is not None:
            self.pending_deletions[user_id] = (
                int(message_id),
                time.time() + float(delay),
            )
            await self._persist_runtime_user(user_id)

        try:
            await asyncio.sleep(delay)
            await self.request("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, RuntimeError):
            log.warning("Restart-safe streak deletion failed: chat_id=%s message_id=%s", chat_id, message_id)
        finally:
            if user_id is not None:
                pending = self.pending_deletions.get(user_id)
                if pending and pending[0] == message_id:
                    self.pending_deletions.pop(user_id, None)
                    await self._persist_runtime_user(user_id)

    async def _resume_pending_delete(self, user_id, chat_id, message_id, delay):
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            await self.request("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, RuntimeError):
            log.warning("Resumed streak deletion failed: user_id=%s", user_id)
        finally:
            pending = self.pending_deletions.get(user_id)
            if pending and pending[0] == message_id:
                self.pending_deletions.pop(user_id, None)
                await self._persist_runtime_user(user_id)

    async def restore_runtime_state(self, *, now=None):
        """Keep menu/history panels and remove tracked legacy notifications."""
        tracked = {(uid, mid) for uid, mid in self.active_messages.items()}
        tracked.update((uid, item[0]) for uid, item in self.pending_deletions.items())
        for user_id, message_id in tracked:
            if message_id not in {self.menu_messages.get(user_id), self.history_messages.get(user_id)}:
                await self._safe_delete(self.chats.get(user_id), message_id)
        users = set(self.active_messages) | set(self.pending_deletions)
        self.active_messages.clear()
        self.pending_deletions.clear()
        for user_id in users:
            await self._persist_runtime_user(user_id)
