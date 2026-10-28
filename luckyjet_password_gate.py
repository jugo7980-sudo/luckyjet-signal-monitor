"""Private Telegram bot access, settings, alerts, and admin controls."""
import asyncio
import hmac
import logging
import os
import time

import httpx

log = logging.getLogger("luckyjet.auth")


class TelegramPasswordGate:
    def __init__(self, request, history_provider=None, admin_chat_id=None, password=None):
        self.request = request
        self.history_provider = history_provider
        self.password = password if password is not None else os.getenv("TELEGRAM_ACCESS_PASSWORD", "").strip()
        self.redis_url = os.getenv("REDIS_URL", "").strip()
        self.redis = None
        self.admin_chat_id = self._to_int(admin_chat_id)

        self.authorized = set()
        self.blocked = set()
        self.pending = set()
        self.chats = {}
        self.history_messages = {}
        self.prompt_messages = {}
        self.menu_messages = {}
        self.notifications_disabled = set()
        self.silent_users = set()
        self.thresholds = {}
        self.delete_seconds = {}
        self.failures = {}
        self.blocked_until = {}
        self.offset = None

        self.current_streak = []
        self.active_messages = {}
        self.last_round = None
        self.last_round_at = None
        self.started_at = time.monotonic()
        self._deletion_tasks = set()

    @staticmethod
    def _to_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    async def start(self):
        if not self.password:
            raise RuntimeError("TELEGRAM_ACCESS_PASSWORD missing")

        if self.redis_url:
            from redis.asyncio import from_url

            self.redis = from_url(self.redis_url, decode_responses=True)
            try:
                await self.redis.ping()
                await self._load_persisted_state()
                log.info("Telegram private state loaded from Redis")
            except Exception:
                log.exception("Redis initialization failed; using in-memory state")
                await self.redis.aclose()
                self.redis = None
        else:
            log.warning("REDIS_URL missing: private bot state is in-memory only")

        try:
            await self.request(
                "setMyCommands",
                {
                    "commands": [
                        {"command": "start", "description": "Открыть бота"},
                        {"command": "menu", "description": "Открыть меню"},
                        {"command": "status", "description": "Статус мониторинга"},
                        {"command": "settings", "description": "Настройки"},
                        {"command": "help", "description": "Справка"},
                    ]
                },
            )
        except (httpx.HTTPError, RuntimeError):
            log.warning("Telegram command registration failed")

        for user_id in list(self.authorized):
            if user_id in self.chats:
                await self.ensure_private_history(user_id)

        try:
            while True:
                try:
                    result = await self.request(
                        "getUpdates",
                        {
                            "timeout": 8,
                            "offset": self.offset,
                            "allowed_updates": ["message", "callback_query"],
                        },
                    )
                    for update in result.get("result", []):
                        self.offset = update["update_id"] + 1
                        await self.handle_update(update)
                except asyncio.CancelledError:
                    raise
                except (httpx.HTTPError, RuntimeError, ValueError, KeyError) as exc:
                    log.warning("Telegram polling failed: %s", type(exc).__name__)
                    await asyncio.sleep(2)
        finally:
            await self.close()

    async def close(self):
        for task in list(self._deletion_tasks):
            task.cancel()
        if self._deletion_tasks:
            await asyncio.gather(*self._deletion_tasks, return_exceptions=True)
            self._deletion_tasks.clear()
        if self.redis:
            await self.redis.aclose()
            self.redis = None

    async def _load_persisted_state(self):
        if not self.redis:
            return
        self.authorized = {int(x) for x in await self.redis.smembers("luckyjet:authorized_users")}
        self.blocked = {int(x) for x in await self.redis.smembers("luckyjet:blocked_users")}
        self.notifications_disabled = {
            int(x) for x in await self.redis.smembers("luckyjet:notifications_disabled")
        }
        self.silent_users = {int(x) for x in await self.redis.smembers("luckyjet:silent_users")}
        self.chats = {
            int(k): int(v)
            for k, v in (await self.redis.hgetall("luckyjet:chats")).items()
        }
        self.history_messages = {
            int(k): int(v)
            for k, v in (await self.redis.hgetall("luckyjet:history_messages")).items()
        }
        self.thresholds = {
            int(k): int(v)
            for k, v in (await self.redis.hgetall("luckyjet:thresholds")).items()
        }
        self.delete_seconds = {
            int(k): int(v)
            for k, v in (await self.redis.hgetall("luckyjet:delete_seconds")).items()
        }

    async def _persist_user(self, user_id):
        if not self.redis:
            return
        uid = str(user_id)
        pipe = self.redis.pipeline()
        if user_id in self.authorized:
            pipe.sadd("luckyjet:authorized_users", uid)
        else:
            pipe.srem("luckyjet:authorized_users", uid)
        if user_id in self.blocked:
            pipe.sadd("luckyjet:blocked_users", uid)
        else:
            pipe.srem("luckyjet:blocked_users", uid)
        if user_id in self.notifications_disabled:
            pipe.sadd("luckyjet:notifications_disabled", uid)
        else:
            pipe.srem("luckyjet:notifications_disabled", uid)
        if user_id in self.silent_users:
            pipe.sadd("luckyjet:silent_users", uid)
        else:
            pipe.srem("luckyjet:silent_users", uid)
        if user_id in self.chats:
            pipe.hset("luckyjet:chats", uid, str(self.chats[user_id]))
        else:
            pipe.hdel("luckyjet:chats", uid)
        if user_id in self.history_messages:
            pipe.hset("luckyjet:history_messages", uid, str(self.history_messages[user_id]))
        else:
            pipe.hdel("luckyjet:history_messages", uid)
        pipe.hset("luckyjet:thresholds", uid, str(self.user_threshold(user_id)))
        pipe.hset("luckyjet:delete_seconds", uid, str(self.user_delete_seconds(user_id)))
        await pipe.execute()

    async def is_authorized(self, user_id):
        return int(user_id) in self.authorized

    def is_admin(self, user_id):
        return self.admin_chat_id is not None and int(user_id) == self.admin_chat_id

    def user_threshold(self, user_id):
        value = int(self.thresholds.get(int(user_id), 4))
        return min(10, max(3, value))

    def user_delete_seconds(self, user_id):
        value = int(self.delete_seconds.get(int(user_id), 30))
        return value if value in {0, 15, 30, 60} else 30

    def _navigation_rows(self):
        return [[
            {"text": "⬅️ Меню", "callback_data": "menu_main"},
            {"text": "✖ Закрыть меню", "callback_data": "menu_close"},
        ]]

    def menu(self, user_id=None):
        rows = [
            [
                {"text": "📊 История", "callback_data": "menu_history"},
                {"text": "📡 Статус", "callback_data": "menu_status"},
            ],
            [
                {"text": "🔔 Уведомления", "callback_data": "menu_notifications"},
                {"text": "⚙️ Настройки", "callback_data": "menu_settings"},
            ],
        ]
        if user_id is not None and self.is_admin(user_id):
            rows.append([{"text": "🛡 Админ", "callback_data": "admin_users"}])
        rows.append([{"text": "✖ Закрыть меню", "callback_data": "menu_close"}])
        return {"inline_keyboard": rows}

    def _settings_text(self, user_id):
        notifications = "включены" if user_id not in self.notifications_disabled else "выключены"
        sound = "без звука" if user_id in self.silent_users else "со звуком"
        delete = self.user_delete_seconds(user_id)
        delete_label = "не удалять" if delete == 0 else f"{delete} сек."
        return (
            "⚙️ Настройки\n"
            f"🔔 Уведомления: {notifications}\n"
            f"🎯 Серия: {self.user_threshold(user_id)} подряд ниже 2x\n"
            f"⏱ Автоудаление: {delete_label}\n"
            f"🔊 Звук: {sound}"
        )

    def settings_menu(self, user_id):
        threshold = self.user_threshold(user_id)
        delete = self.user_delete_seconds(user_id)
        notifications_enabled = user_id not in self.notifications_disabled
        sound_enabled = user_id not in self.silent_users

        threshold_rows = []
        for start in (3, 7):
            row = []
            stop = 7 if start == 3 else 11
            for value in range(start, stop):
                prefix = "✓ " if value == threshold else ""
                row.append({"text": f"{prefix}{value}", "callback_data": f"threshold:{value}"})
            threshold_rows.append(row)

        delete_row = []
        for value, label in ((15, "15с"), (30, "30с"), (60, "60с"), (0, "Без удаления")):
            prefix = "✓ " if value == delete else ""
            callback_value = "off" if value == 0 else str(value)
            delete_row.append({"text": f"{prefix}{label}", "callback_data": f"delete:{callback_value}"})

        return {
            "inline_keyboard": [
                [{
                    "text": f"🔔 Уведомления: {'ВКЛ' if notifications_enabled else 'ВЫКЛ'}",
                    "callback_data": "settings_notifications",
                }],
                *threshold_rows,
                delete_row,
                [{
                    "text": f"🔊 Звук: {'ВКЛ' if sound_enabled else 'ВЫКЛ'}",
                    "callback_data": "settings_sound",
                }],
                [{"text": "🧪 Тест уведомления", "callback_data": "settings_test"}],
                [{"text": "♻️ Сбросить настройки", "callback_data": "settings_reset"}],
                *self._navigation_rows(),
            ]
        }

    def _admin_menu(self):
        rows = []
        for user_id in sorted(self.authorized):
            if self.is_admin(user_id):
                continue
            rows.append(
                [
                    {"text": f"Отозвать {user_id}", "callback_data": f"admin_revoke:{user_id}"},
                    {"text": f"Блок {user_id}", "callback_data": f"admin_block:{user_id}"},
                ]
            )
        for user_id in sorted(self.blocked):
            rows.append(
                [{"text": f"Разблокировать {user_id}", "callback_data": f"admin_unblock:{user_id}"}]
            )
        rows.extend(self._navigation_rows())
        return {"inline_keyboard": rows}

    async def _safe_delete(self, chat_id, message_id):
        if chat_id is None or message_id is None:
            return
        try:
            await self.request("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
        except (httpx.HTTPError, RuntimeError, KeyError, TypeError):
            pass

    async def _replace_prompt(self, user_id, chat_id, text):
        message_id = self.prompt_messages.get(user_id)
        if message_id is not None:
            try:
                await self.request(
                    "editMessageText",
                    {"chat_id": chat_id, "message_id": message_id, "text": text},
                )
                return
            except (httpx.HTTPError, RuntimeError):
                self.prompt_messages.pop(user_id, None)
        result = await self.request("sendMessage", {"chat_id": chat_id, "text": text})
        message_id = result.get("result", {}).get("message_id")
        if message_id is not None:
            self.prompt_messages[user_id] = int(message_id)

    async def _delete_prompt(self, user_id, chat_id):
        message_id = self.prompt_messages.pop(user_id, None)
        await self._safe_delete(chat_id, message_id)

    async def _delete_transient_source(self, user_id, chat_id, callback):
        message_id = self._to_int((callback.get("message") or {}).get("message_id"))
        if message_id is None:
            return
        if message_id == self.history_messages.get(user_id):
            return
        if message_id == self.active_messages.get(user_id):
            return
        if self.menu_messages.get(user_id) == message_id:
            self.menu_messages.pop(user_id, None)
        await self._safe_delete(chat_id, message_id)

    async def _send_transient(self, user_id, chat_id, text, reply_markup):
        old_message_id = self.menu_messages.pop(user_id, None)
        await self._safe_delete(chat_id, old_message_id)
        result = await self.request(
            "sendMessage",
            {"chat_id": chat_id, "text": text, "reply_markup": reply_markup},
        )
        message_id = result.get("result", {}).get("message_id")
        if message_id is not None:
            self.menu_messages[user_id] = int(message_id)

    async def _show_status(self, user_id, chat_id):
        uptime = max(0, int(time.monotonic() - self.started_at))
        hours, rem = divmod(uptime, 3600)
        minutes, seconds = divmod(rem, 60)
        latest = self.last_round or "нет данных"
        answer = (
            "📡 Статус: работает\n"
            f"Последний коэффициент: {latest}\n"
            f"Текущая серия <2x: {len(self.current_streak)}\n"
            f"Аптайм: {hours:02d}:{minutes:02d}:{seconds:02d}"
        )
        await self._send_transient(
            user_id,
            chat_id,
            answer,
            {"inline_keyboard": self._navigation_rows()},
        )

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

        user_id = self._to_int(sender.get("id"))
        chat_id = self._to_int(chat.get("id"))
        message_id = self._to_int(msg.get("message_id"))
        if user_id is None or chat_id is None:
            return

        value = (msg.get("text") or "").strip()
        command = value.split("@", 1)[0].split(" ", 1)[0].lower()

        if user_id in self.blocked and not self.is_admin(user_id):
            await self._safe_delete(chat_id, message_id)
            await self._replace_prompt(user_id, chat_id, "⛔ Доступ заблокирован.")
            return

        if await self.is_authorized(user_id):
            self.chats[user_id] = chat_id
            # Keep user commands visible; never erase the only chat content.
            if command == "/start":
                await self.ensure_private_history(user_id)
            elif command == "/menu":
                await self._send_transient(
                    user_id,
                    chat_id,
                    "🎰 Lucky Jet Monitor\nВыберите раздел.",
                    self.menu(user_id),
                )
            elif command == "/status":
                await self._show_status(user_id, chat_id)
            elif command == "/settings":
                await self._send_transient(
                    user_id,
                    chat_id,
                    self._settings_text(user_id),
                    self.settings_menu(user_id),
                )
            elif command == "/help":
                await self._send_transient(
                    user_id,
                    chat_id,
                    "ℹ️ Lucky Jet Monitor\n"
                    "История обновляется автоматически. Меню открывается кнопкой под историей. "
                    "Сообщение серии появляется при достижении выбранного порога и обновляется до завершения серии.",
                    {"inline_keyboard": self._navigation_rows()},
                )
            return

        if command == "/start":
            await self._safe_delete(chat_id, message_id)
            self.pending.add(user_id)
            await self._replace_prompt(user_id, chat_id, "🔐 Введите пароль для доступа к боту.")
            return

        await self._safe_delete(chat_id, message_id)
        if user_id not in self.pending:
            await self._replace_prompt(user_id, chat_id, "Для начала нажмите /start.")
            return

        if self.blocked_until.get(user_id, 0) > time.monotonic():
            await self._replace_prompt(user_id, chat_id, "⏳ Повторите попытку через 5 минут.")
            return

        if hmac.compare_digest(value, self.password):
            self.authorized.add(user_id)
            self.chats[user_id] = chat_id
            self.pending.discard(user_id)
            self.failures.pop(user_id, None)
            self.blocked_until.pop(user_id, None)
            await self._persist_user(user_id)
            await self._delete_prompt(user_id, chat_id)
            await self.ensure_private_history(user_id)
            return

        count = self.failures.get(user_id, 0) + 1
        self.failures[user_id] = count
        if count >= 5:
            self.blocked_until[user_id] = time.monotonic() + 300
            self.failures[user_id] = 0
            answer = "⏳ 5 неверных попыток. Повторите через 5 минут."
        else:
            answer = "❌ Неверный пароль. Попробуйте ещё раз."
        await self._replace_prompt(user_id, chat_id, answer)

    async def handle_callback(self, callback):
        user_id = self._to_int((callback.get("from") or {}).get("id"))
        chat_id = self._to_int(((callback.get("message") or {}).get("chat") or {}).get("id"))
        data = callback.get("data", "")
        try:
            await self.request("answerCallbackQuery", {"callback_query_id": callback["id"]})
        except (httpx.HTTPError, RuntimeError, KeyError):
            pass

        if user_id is None or chat_id is None or not await self.is_authorized(user_id):
            return
        if user_id in self.blocked and not self.is_admin(user_id):
            return

        self.chats[user_id] = chat_id

        if data == "noop":
            return

        if data == "menu_open":
            await self._send_transient(
                user_id,
                chat_id,
                "🎰 Lucky Jet Monitor\nВыберите раздел.",
                self.menu(user_id),
            )
            return

        if data == "menu_history":
            source_id = self._to_int((callback.get("message") or {}).get("message_id"))
            if not await self.open_private_history(user_id, source_id):
                await self.request(
                    "sendMessage",
                    {"chat_id": chat_id,
                     "text": "⚠️ Историю пока не удалось открыть. Повторите попытку."},
                )
            return

        await self._delete_transient_source(user_id, chat_id, callback)

        if data == "menu_close":
            return
        if data == "menu_main":
            answer = "🎰 Lucky Jet Monitor\nВыберите раздел."
            markup = self.menu(user_id)
        elif data == "menu_status":
            uptime = max(0, int(time.monotonic() - self.started_at))
            hours, rem = divmod(uptime, 3600)
            minutes, seconds = divmod(rem, 60)
            latest = self.last_round or "нет данных"
            answer = (
                "📡 Статус: работает\n"
                f"Последний коэффициент: {latest}\n"
                f"Текущая серия <2x: {len(self.current_streak)}\n"
                f"Аптайм: {hours:02d}:{minutes:02d}:{seconds:02d}"
            )
            markup = {"inline_keyboard": self._navigation_rows()}
        elif data == "menu_notifications":
            if user_id in self.notifications_disabled:
                self.notifications_disabled.remove(user_id)
                answer = "🔔 Уведомления включены."
            else:
                self.notifications_disabled.add(user_id)
                answer = "🔕 Уведомления выключены."
            await self._persist_user(user_id)
            markup = self.menu(user_id)
        elif data == "menu_settings":
            answer = self._settings_text(user_id)
            markup = self.settings_menu(user_id)
        elif data == "settings_notifications":
            if user_id in self.notifications_disabled:
                self.notifications_disabled.remove(user_id)
            else:
                self.notifications_disabled.add(user_id)
            await self._persist_user(user_id)
            answer = self._settings_text(user_id)
            markup = self.settings_menu(user_id)
        elif data.startswith("threshold:"):
            try:
                value = int(data.split(":", 1)[1])
            except ValueError:
                return
            if value not in range(3, 11):
                return
            self.thresholds[user_id] = value
            await self._persist_user(user_id)
            answer = self._settings_text(user_id)
            markup = self.settings_menu(user_id)
        elif data.startswith("delete:"):
            raw = data.split(":", 1)[1]
            value = 0 if raw == "off" else self._to_int(raw)
            if value not in {0, 15, 30, 60}:
                return
            self.delete_seconds[user_id] = value
            await self._persist_user(user_id)
            answer = self._settings_text(user_id)
            markup = self.settings_menu(user_id)
        elif data == "settings_sound":
            if user_id in self.silent_users:
                self.silent_users.remove(user_id)
            else:
                self.silent_users.add(user_id)
            await self._persist_user(user_id)
            answer = self._settings_text(user_id)
            markup = self.settings_menu(user_id)
        elif data == "settings_test":
            result = await self.request(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": "🧪 Тест уведомления Lucky Jet",
                    "disable_notification": user_id in self.silent_users,
                },
            )
            test_message_id = result.get("result", {}).get("message_id")
            if test_message_id is not None:
                task = asyncio.create_task(self._schedule_delete(chat_id, int(test_message_id), 5))
                self._deletion_tasks.add(task)
                task.add_done_callback(self._deletion_tasks.discard)
            answer = self._settings_text(user_id)
            markup = self.settings_menu(user_id)
        elif data == "settings_reset":
            self.thresholds.pop(user_id, None)
            self.delete_seconds.pop(user_id, None)
            self.notifications_disabled.discard(user_id)
            self.silent_users.discard(user_id)
            await self._persist_user(user_id)
            answer = self._settings_text(user_id)
            markup = self.settings_menu(user_id)
        elif data == "admin_users" and self.is_admin(user_id):
            users = ", ".join(str(x) for x in sorted(self.authorized)) or "нет"
            blocked = ", ".join(str(x) for x in sorted(self.blocked)) or "нет"
            answer = f"🛡 Пользователи с доступом: {users}\nЗаблокированы: {blocked}"
            markup = self._admin_menu()
        elif data.startswith("admin_revoke:") and self.is_admin(user_id):
            target = self._to_int(data.split(":", 1)[1])
            if target is None or self.is_admin(target):
                return
            await self._revoke_user(target, block=False)
            answer = f"✅ Доступ пользователя {target} отозван."
            markup = self._admin_menu()
        elif data.startswith("admin_block:") and self.is_admin(user_id):
            target = self._to_int(data.split(":", 1)[1])
            if target is None or self.is_admin(target):
                return
            await self._revoke_user(target, block=True)
            answer = f"⛔ Пользователь {target} заблокирован."
            markup = self._admin_menu()
        elif data.startswith("admin_unblock:") and self.is_admin(user_id):
            target = self._to_int(data.split(":", 1)[1])
            if target is None:
                return
            self.blocked.discard(target)
            await self._persist_user(target)
            answer = f"✅ Пользователь {target} разблокирован."
            markup = self._admin_menu()
        else:
            return

        await self._send_transient(user_id, chat_id, answer, markup)

    async def _revoke_user(self, user_id, *, block):
        chat_id = self.chats.get(user_id)
        history_message_id = self.history_messages.pop(user_id, None)
        menu_message_id = self.menu_messages.pop(user_id, None)
        streak_message_id = self.active_messages.pop(user_id, None)
        prompt_message_id = self.prompt_messages.pop(user_id, None)
        self.authorized.discard(user_id)
        self.pending.discard(user_id)
        self.notifications_disabled.discard(user_id)
        self.silent_users.discard(user_id)
        self.thresholds.pop(user_id, None)
        self.delete_seconds.pop(user_id, None)
        self.chats.pop(user_id, None)
        if block:
            self.blocked.add(user_id)
        else:
            self.blocked.discard(user_id)
        await self._persist_user(user_id)
        if chat_id is not None:
            await self._safe_delete(chat_id, menu_message_id)
            await self._safe_delete(chat_id, prompt_message_id)
            await self._safe_delete(chat_id, streak_message_id)
            await self._safe_delete(chat_id, history_message_id)

    @staticmethod
    def _format_coefficient(value):
        try:
            number = float(str(value).strip().rstrip("xX").replace(",", "."))
        except (TypeError, ValueError):
            return None
        if number < 1:
            return None
        return f"{number:.2f}x"

    @classmethod
    def _colored_label(cls, value):
        formatted = cls._format_coefficient(value)
        if formatted is None:
            return str(value)
        number = float(formatted[:-1])
        marker = "🟦" if number < 2 else "🟪" if number < 10 else "🟧"
        return f"{marker}{formatted}"

    def _history_markup(self):
        values = list(self.history_provider() if self.history_provider else [])[:20]
        rows = []
        for start in range(0, len(values), 5):
            rows.append(
                [
                    {"text": self._colored_label(value), "callback_data": "noop"}
                    for value in values[start : start + 5]
                ]
            )
        rows.append([{"text": "☰ Меню", "callback_data": "menu_open"}])
        return {"inline_keyboard": rows}

    async def open_private_history(self, user_id, source_message_id):
        return await self.ensure_private_history(user_id)

    async def ensure_private_history(self, user_id):
        user_id = int(user_id)
        chat_id = self.chats.get(user_id)
        if chat_id is None:
            return False
        markup = self._history_markup()
        message_id = self.history_messages.get(user_id)
        if message_id is not None:
            try:
                await self.request(
                    "editMessageReplyMarkup",
                    {"chat_id": chat_id, "message_id": message_id, "reply_markup": markup},
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
                {"chat_id": chat_id, "text": "🎰 Lucky Jet — история", "reply_markup": markup},
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
        for user_id in list(self.authorized):
            if user_id in self.chats:
                await self.ensure_private_history(user_id)

    def _streak_markup(self, ending=None):
        count = len(self.current_streak)
        latest = ending if ending is not None else self.current_streak[-1]
        recent = [*self.current_streak, *([ending] if ending is not None else [])][-5:]
        return {
            "inline_keyboard": [
                [{"text": f"Серия {count} · {self._colored_label(latest)}", "callback_data": "noop"}],
                [
                    {"text": self._colored_label(item), "callback_data": "noop"}
                    for item in recent
                ],
            ]
        }

    async def on_round(self, coefficient):
        value = self._format_coefficient(coefficient)
        if value is None:
            return
        self.last_round = value
        self.last_round_at = time.time()
        await self.update_private_histories()
        number = float(value[:-1])

        if number < 2:
            self.current_streak.append(value)
            for user_id, chat_id in list(self.chats.items()):
                if user_id not in self.authorized:
                    continue

                if user_id in self.active_messages:
                    try:
                        await self.request(
                            "editMessageReplyMarkup",
                            {
                                "chat_id": chat_id,
                                "message_id": self.active_messages[user_id],
                                "reply_markup": self._streak_markup(),
                            },
                        )
                    except (httpx.HTTPError, RuntimeError):
                        log.warning("Private streak update failed: user_id=%s", user_id)
                        self.active_messages.pop(user_id, None)
                    continue

                if user_id in self.notifications_disabled:
                    continue

                threshold = self.user_threshold(user_id)
                if len(self.current_streak) >= threshold:
                    try:
                        result = await self.request(
                            "sendMessage",
                            {
                                "chat_id": chat_id,
                                "text": "🎰 Lucky Jet | Серия <2x",
                                "reply_markup": self._streak_markup(),
                                "disable_notification": user_id in self.silent_users,
                            },
                        )
                        message_id = result.get("result", {}).get("message_id")
                        if message_id is not None:
                            self.active_messages[user_id] = int(message_id)
                    except (httpx.HTTPError, RuntimeError):
                        log.warning("Private streak create failed: user_id=%s", user_id)
            return

        for user_id, message_id in list(self.active_messages.items()):
            chat_id = self.chats.get(user_id)
            if chat_id is None:
                continue
            try:
                await self.request(
                    "editMessageReplyMarkup",
                    {
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "reply_markup": self._streak_markup(ending=value),
                    },
                )
                delay = self.user_delete_seconds(user_id)
                if delay > 0:
                    task = asyncio.create_task(self._schedule_delete(chat_id, message_id, delay))
                    self._deletion_tasks.add(task)
                    task.add_done_callback(self._deletion_tasks.discard)
                    await asyncio.sleep(0)
            except (httpx.HTTPError, RuntimeError):
                log.warning("Private streak completion failed: user_id=%s", user_id)
        self.current_streak = []
        self.active_messages = {}

    async def _schedule_delete(self, chat_id, message_id, delay):
        try:
            await asyncio.sleep(delay)
            await self.request("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, RuntimeError):
            log.warning("Private streak deletion failed: chat_id=%s message_id=%s", chat_id, message_id)
