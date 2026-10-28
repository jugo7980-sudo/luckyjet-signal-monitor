"""Telegram /start password gate using Telegram Bot API long polling."""
import asyncio
import hmac
import logging
import os

import httpx

log = logging.getLogger("luckyjet.auth")


class TelegramPasswordGate:
    def __init__(self, request):
        self.request = request
        self.password = os.getenv("TELEGRAM_ACCESS_PASSWORD", "122011")
        self.redis_url = os.getenv("REDIS_URL")
        self.redis = None
        self.authorized = set()
        self.pending = set()
        self.offset = None

    async def start(self):
        if self.redis_url:
            from redis.asyncio import from_url
            self.redis = from_url(self.redis_url, decode_responses=True)
            await self.redis.ping()
        else:
            log.warning("REDIS_URL missing: authorizations will not survive restart")
        try:
            while True:
                try:
                    result = await self.request("getUpdates", {
                        "timeout": 15,
                        "offset": self.offset,
                        "allowed_updates": ["message"],
                    })
                    for update in result.get("result", []):
                        self.offset = update["update_id"] + 1
                        await self.handle_update(update)
                except asyncio.CancelledError:
                    raise
                except (httpx.HTTPError, RuntimeError, ValueError, KeyError) as exc:
                    log.warning("Password polling failed: %s", type(exc).__name__)
                    await asyncio.sleep(3)
        finally:
            if self.redis:
                await self.redis.aclose()

    async def is_authorized(self, user_id):
        if self.redis:
            return bool(await self.redis.sismember("luckyjet:authorized_users", str(user_id)))
        return user_id in self.authorized

    async def handle_update(self, update):
        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        sender = msg.get("from") or {}
        if chat.get("type") != "private" or sender.get("is_bot"):
            return
        user_id, chat_id = sender.get("id"), chat.get("id")
        if user_id is None or chat_id is None:
            return
        value = (msg.get("text") or "").strip()
        if value.split("@", 1)[0].split(" ", 1)[0] == "/start":
            if await self.is_authorized(user_id):
                answer = "Доступ уже открыт."
            else:
                self.pending.add(user_id)
                answer = "🔐 Введите пароль для доступа к боту."
        elif await self.is_authorized(user_id):
            return
        elif user_id not in self.pending:
            answer = "Для начала нажмите /start."
        elif hmac.compare_digest(value, self.password):
            if self.redis:
                await self.redis.sadd("luckyjet:authorized_users", str(user_id))
            else:
                self.authorized.add(user_id)
            self.pending.discard(user_id)
            answer = "✅ Пароль верный. Доступ открыт."
        else:
            answer = "❌ Неверный пароль. Попробуйте ещё раз."
        await self.request("sendMessage", {"chat_id": chat_id, "text": answer})
