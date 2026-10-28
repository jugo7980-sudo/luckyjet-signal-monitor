"""Telegram /start password gate using Telegram Bot API long polling."""
import asyncio
import hmac
import logging
import os
import time

import httpx

log = logging.getLogger("luckyjet.auth")


class TelegramPasswordGate:
    def __init__(self, request, history_provider=None):
        self.request = request
        self.history_provider = history_provider
        self.notifications_disabled = set()
        self.password = os.getenv("TELEGRAM_ACCESS_PASSWORD", "122011")
        self.redis_url = os.getenv("REDIS_URL")
        self.redis = None
        self.authorized = set()
        self.pending = set()
        self.offset = None
        self.failures = {}
        self.blocked_until = {}

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
                        "allowed_updates": ["message", "callback_query"],
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

    def menu(self):
        return {"inline_keyboard": [
            [{"text": "📊 История", "callback_data": "menu_history"},
             {"text": "📡 Статус", "callback_data": "menu_status"}],
            [{"text": "🔔 Уведомления", "callback_data": "menu_notifications"},
             {"text": "⚙️ Настройки", "callback_data": "menu_settings"}],
        ]}

    async def handle_update(self, update):
        callback = update.get("callback_query")
        if callback:
            await self.handle_callback(callback)
            return
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
        elif self.blocked_until.get(user_id, 0) > time.monotonic():
            answer = "Повторите попытку через 5 минут."
        elif hmac.compare_digest(value, self.password):
            if self.redis:
                await self.redis.sadd("luckyjet:authorized_users", str(user_id))
            else:
                self.authorized.add(user_id)
            self.pending.discard(user_id)
            self.failures.pop(user_id, None)
            self.blocked_until.pop(user_id, None)
            answer = "✅ Пароль верный. Доступ открыт."
        else:
            count = self.failures.get(user_id, 0) + 1
            self.failures[user_id] = count
            if count >= 5:
                self.blocked_until[user_id] = time.monotonic() + 300
                self.failures[user_id] = 0
                answer = "Слишком много ошибок. Повторите через 5 минут."
            else:
                answer = "❌ Неверный пароль. Попробуйте ещё раз."
        payload = {"chat_id": chat_id, "text": answer}
        if await self.is_authorized(user_id):
            payload["reply_markup"] = self.menu()
        await self.request("sendMessage", payload)

    async def handle_callback(self, callback):
        user_id = (callback.get("from") or {}).get("id")
        chat_id = ((callback.get("message") or {}).get("chat") or {}).get("id")
        data = callback.get("data", "")
        try:
            await self.request("answerCallbackQuery", {"callback_query_id": callback["id"]})
        except (httpx.HTTPError, RuntimeError):
            pass
        if not chat_id or not user_id or not await self.is_authorized(user_id):
            return
        if data == "menu_history":
            values = list(self.history_provider() if self.history_provider else [])[:20]
            answer = "📊 Последние коэффициенты:\n" + ("  ".join(values) if values else "Пока нет данных")
        elif data == "menu_status":
            answer = "📡 Бот запущен. Статус источника данных проверяйте по обновлению истории."
        elif data == "menu_notifications":
            answer = "🔔 Настройка персональных уведомлений появится после подключения рассылки в личные чаты."
        elif data == "menu_settings":
            answer = "⚙️ Настройки: порог серии 4, автоудаление 30 секунд."
        else:
            return
        await self.request("sendMessage", {"chat_id": chat_id, "text": answer, "reply_markup": self.menu()})
