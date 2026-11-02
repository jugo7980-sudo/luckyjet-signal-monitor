"""Private history rendering and bounded concurrent updates."""
import asyncio
import logging
import httpx

log = logging.getLogger("luckyjet.auth")


class HistoryPanels:
    async def ensure_private_history(self, user_id):
        user_id = int(user_id)
        lock = self._history_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            if user_id in self.history_hidden:
                return False
            return await self._ensure_private_history(user_id)

    async def _ensure_private_history(self, user_id):
        chat_id = self.chats.get(user_id)
        if chat_id is None:
            return False
        markup = self._history_markup(user_id)
        message_id = self.history_messages.get(user_id)
        if message_id is not None:
            try:
                await self.request(
                    "editMessageText",
                    {"chat_id": chat_id, "message_id": message_id, "text": "📊", "reply_markup": markup},
                )
                return True
            except httpx.HTTPStatusError as exc:
                description = ""
                try:
                    description = str(exc.response.json().get("description", "")).lower()
                except (ValueError, AttributeError):
                    pass
                if exc.response.status_code == 400 and "message is not modified" in description:
                    return True
                if exc.response.status_code == 400 and ("message to edit not found" in description or "message_id_invalid" in description):
                    log.info("Private history message deleted; recreating: user_id=%s", user_id)
                    self.history_messages.pop(user_id, None)
                    await self._persist_user(user_id)
                else:
                    log.warning("Private history edit rejected: user_id=%s status=%s", user_id, exc.response.status_code)
                    return False
            except httpx.HTTPError as exc:
                # A deleted message cannot be edited; recreate it. Network errors
                # should not cause duplicates, so only recover known missing-message errors.
                if "message missing" not in str(exc).lower() and "message to edit not found" not in str(exc).lower():
                    log.warning("Private history edit failed: user_id=%s error=%s", user_id, type(exc).__name__)
                    return False
                log.info("Private history message missing; recreating: user_id=%s", user_id)
                self.history_messages.pop(user_id, None)
                await self._persist_user(user_id)
            except RuntimeError:
                log.warning("Private history edit failed: user_id=%s", user_id)
                return False
        try:
            result = await self.request(
                "sendMessage",
                {"chat_id": chat_id, "text": "📊", "reply_markup": markup},
            )
            message_id = result.get("result", {}).get("message_id")
            if message_id is None:
                return False
            self.history_messages[user_id] = int(message_id)
            await self._persist_user(user_id)
            return True
        except (httpx.HTTPError, RuntimeError):
            log.warning("Private history create failed: user_id=%s", user_id)
            return False

    async def update_private_histories(self):
        # A slow Telegram response must not hold up every other user's panel.
        # Keep a small shared limit across overlapping refresh calls.
        if not hasattr(self, "_history_refresh_slots"):
            self._history_refresh_slots = asyncio.Semaphore(4)

        async def update(user_id):
            async with self._history_refresh_slots:
                if user_id in self.authorized and user_id in self.chats:
                    await self.ensure_private_history(user_id)

        async with asyncio.TaskGroup() as group:
            for user_id in list(self.authorized):
                if user_id in self.chats and user_id not in self.history_hidden:
                    group.create_task(update(user_id))

