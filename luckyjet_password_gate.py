"""Private Telegram bot access, settings, alerts, and admin controls."""
import asyncio
import hmac
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import httpx
from release_reset import apply_release_reset
from luckyjet_signal.demo_ledger import (
    MINIMUM_STAKE_CENTS,
    TRIGGER_OPTIONS,
    DemoLedger,
    format_dollars,
    parse_dollars,
)

log = logging.getLogger("luckyjet.auth")
THAILAND_TIME = timezone(timedelta(hours=7), name="Asia/Bangkok")


class TelegramPasswordGate:
    def __init__(self, request, history_provider=None, admin_chat_id=None, password=None, history_refresher=None):
        self.request = request
        self.history_provider = history_provider
        self.demo_probe = None
        self.history_refresher = history_refresher
        self._refresh_tasks = set()
        self.password = password if password is not None else os.getenv("TELEGRAM_ACCESS_PASSWORD", "").strip()
        self.redis_url = os.getenv("REDIS_URL", "").strip()
        self.redis = None
        self.admin_chat_id = self._to_int(admin_chat_id)

        self.authorized = set()
        self.blocked = set()
        self.pending = set()
        self.chats = {}
        self.history_messages = {}
        self.history_hidden = set()
        self._history_locks = {}
        self._menu_locks = {}
        self.prompt_messages = {}
        self.menu_messages = {}
        self.notifications_disabled = set()
        self.silent_users = set()
        self.thresholds = {}
        self.delete_seconds = {}
        self.history_sizes = {}
        self.demo_ledgers = {}
        self.demo_pending_inputs = {}
        self.admin_pending_actions = {}
        self.pending_confirmations = {}
        self.demo_panel_messages = {}
        self._demo_panel_content = {}
        self.failures = {}
        self.blocked_until = {}
        self.offset = None

        self.current_streak = []
        self.active_messages = {}
        self.pending_deletions = {}
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
                if await apply_release_reset(self.redis):
                    self.current_streak = []
                await self._load_persisted_state()
                log.info("Telegram private state loaded from Redis")
            except Exception:
                log.exception("Redis initialization failed; persistent login cannot start")
                await self.redis.aclose()
                self.redis = None
                raise
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
                except (httpx.HTTPError, RuntimeError, ValueError, KeyError, TypeError) as exc:
                    log.warning("Telegram polling failed: %s", type(exc).__name__)
                    await asyncio.sleep(2)
        finally:
            await self.close()

    async def close(self):
        for task in list(self._refresh_tasks):
            task.cancel()
        if self._refresh_tasks:
            await asyncio.gather(*self._refresh_tasks, return_exceptions=True)
            self._refresh_tasks.clear()
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
        self.history_hidden = {int(x) for x in await self.redis.smembers("luckyjet:history_hidden")}
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
        legacy_durations = [uid for uid, value in self.delete_seconds.items() if value in {10, 15}]
        if legacy_durations:
            pipe = self.redis.pipeline()
            for uid in legacy_durations:
                self.delete_seconds[uid] = 5
                pipe.hset("luckyjet:delete_seconds", str(uid), "5")
            await pipe.execute()
        self.history_sizes = {
            int(k): int(v)
            for k, v in (await self.redis.hgetall("luckyjet:history_sizes")).items()
        }
        self.demo_ledgers = {}
        for key, raw in (await self.redis.hgetall("luckyjet:demo_sessions")).items():
            try:
                self.demo_ledgers[int(key)] = DemoLedger(**json.loads(raw))
            except (TypeError, ValueError, json.JSONDecodeError):
                log.warning("Ignoring invalid persisted demo state for user_id=%s", key)

    def _demo_ledger(self, user_id):
        user_id = int(user_id)
        if user_id not in self.demo_ledgers:
            self.demo_ledgers[user_id] = DemoLedger()
        return self.demo_ledgers[user_id]

    async def _persist_demo(self, user_id):
        if self.redis:
            await self.redis.hset(
                "luckyjet:demo_sessions",
                str(int(user_id)),
                json.dumps(self._demo_ledger(user_id).dump(), separators=(",", ":")),
            )

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
        if user_id in self.history_hidden:
            pipe.sadd("luckyjet:history_hidden", uid)
        else:
            pipe.srem("luckyjet:history_hidden", uid)
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
        pipe.hset("luckyjet:history_sizes", uid, str(self.user_history_size(user_id)))
        await pipe.execute()

    async def is_authorized(self, user_id):
        return int(user_id) in self.authorized

    def is_admin(self, user_id):
        return self.admin_chat_id is not None and int(user_id) == self.admin_chat_id

    def user_threshold(self, user_id):
        value = int(self.thresholds.get(int(user_id), 4))
        return min(10, max(3, value))

    def user_delete_seconds(self, user_id):
        value = int(self.delete_seconds.get(int(user_id), 5))
        if value in {10, 15}:
            return 5
        return value if value in {0, 5, 30, 60} else 5

    def user_history_size(self, user_id):
        value = int(self.history_sizes.get(int(user_id), 20))
        return value if value in {20, 50, 100} else 20

    def _navigation_rows(self, back=None):
        rows = [[{"text": "⬅️ Назад", "callback_data": back}]] if back else []
        rows.append([
            {"text": "🏠 Главное меню", "callback_data": "menu_main"},
            {"text": "✖ Закрыть", "callback_data": "menu_close"},
        ])
        return rows

    @staticmethod
    def _main_text():
        return (
            "🎰 Lucky Jet Monitor\n"
            "История коэффициентов — завершённые раунды, новые слева направо.\n"
            "Демо — проверка схемы на виртуальном балансе.\n"
            "Настройки — уведомления, звук и размер истории."
        )

    @staticmethod
    def _help_text():
        return (
            "ℹ️ Lucky Jet Monitor — как пользоваться\n\n"
            "1. История коэффициентов обновляется автоматически. Нажмите любой коэффициент для обновления вручную.\n"
            "🟦 Ниже 2x · 🟪 От 2x до 9.99x · 🟧 От 10x.\n\n"
            "2. В настройках выберите, после скольких результатов подряд ниже 2x получать уведомление. "
            "Одно сообщение обновляется до конца серии, затем удаляется по вашему таймеру.\n\n"
            "3. В демо-режиме отдельно задайте условие старта и ставку, затем нажмите «Включить демо». "
            "После проигрыша ставка удваивается, выход рассчитывается на 2x. Реальные ставки не выполняются.\n\n"
            "Баланс — виртуальный расчёт бота. Результаты доступны в истории демо-циклов; время — Таиланд, UTC+7."
        )

    def _supported_callback(self, data, user_id):
        if data in {
            "menu_open", "menu_close", "menu_main", "menu_status", "menu_stats",
            "menu_notifications", "menu_settings", "settings_notifications",
            "settings_sound", "settings_test", "settings_reset", "demo_menu",
            "demo_toggle", "demo_refresh_balance", "demo_set_balance", "demo_set_stake", "demo_input_cancel",
            "demo_history", "demo_reset", "demo_reset_confirm", "demo_trigger_menu",
            "settings_threshold", "settings_history", "settings_delete", "settings_reset_confirm", "menu_help",
        }:
            return True
        if data.startswith("demo_history:"):
            page = self._to_int(data.split(":", 1)[1])
            return page is not None and page >= 0
        if self.is_admin(user_id) and data.startswith("admin_user:"):
            return self._to_int(data.split(":", 1)[1]) is not None
        if self.is_admin(user_id) and data == "admin_confirm":
            return True
        if data.startswith("threshold:"):
            return self._to_int(data.split(":", 1)[1]) in range(3, 11)
        if data.startswith("history_size:"):
            return self._to_int(data.split(":", 1)[1]) in {20, 50, 100}
        if data.startswith("delete:"):
            raw = data.split(":", 1)[1]
            return raw == "off" or self._to_int(raw) in {5, 10, 15, 30, 60}
        if data.startswith("demo_trigger:"):
            return self._to_int(data.split(":", 1)[1]) in TRIGGER_OPTIONS
        if self.is_admin(user_id) and data == "admin_users":
            return True
        if self.is_admin(user_id) and data.startswith(("admin_revoke:", "admin_block:", "admin_unblock:")):
            target = self._to_int(data.split(":", 1)[1])
            return target is not None and (data.startswith("admin_unblock:") or not self.is_admin(target))
        return False

    def menu(self, user_id=None):
        hidden = user_id in self.history_hidden
        rows = [
            [{"text": "📊 Показать историю коэффициентов" if hidden else "📊 Скрыть историю коэффициентов", "callback_data": "menu_history"}],
            [{"text": "🧪 Демо-режим", "callback_data": "demo_menu"}],
            [{"text": "⚙️ Настройки уведомлений и истории", "callback_data": "menu_settings"}],
        ]
        if user_id is not None:
            balance = format_dollars(self._demo_ledger(user_id).balance_cents)
            rows.append([{"text": f"💵 Демо-баланс: {balance} · обновить", "callback_data": "demo_refresh_balance"}])
        rows.append([{"text": "❓ Как пользоваться ботом", "callback_data": "menu_help"}])
        if user_id is not None and self.is_admin(user_id):
            rows.append([{"text": "🛡 Управление доступом", "callback_data": "admin_users"}])
        rows.append([{"text": "✖ Закрыть меню", "callback_data": "menu_close"}])
        return {"inline_keyboard": rows}

    def _demo_text(self, user_id):
        ledger = self._demo_ledger(user_id)
        if not ledger.enabled:
            state = "приостановлен, текущий цикл сохранён" if ledger.active else "выключен"
        else:
            state = "цикл в работе" if ledger.active else "ожидание условия запуска"
        if ledger.active:
            progress = (
                f"Следующая виртуальная ставка: {format_dollars(ledger.next_stake_cents)}\n"
                f"Уже поставлено в цикле: {format_dollars(ledger.cycle_stake_cents)}"
            )
        else:
            progress = f"Сейчас ниже 2x подряд: {ledger.low_streak} из {ledger.trigger_streak}"
        return (
            "🧪 Демо-режим — виртуальные ставки\n"
            f"Состояние: {state}\n"
            f"Виртуальный баланс: {format_dollars(ledger.balance_cents)}\n"
            f"Начинать цикл после {ledger.trigger_streak} раундов подряд ниже 2x.\n"
            f"Стартовая ставка: {format_dollars(ledger.base_stake_cents)} (минимум $10).\n"
            f"{progress}\n\n"
            "После условия ставка учитывается со следующего раунда. После проигрыша — удвоение. "
            "Выход на 2x; после выигрыша — ожидание новой серии.\n"
            "Это расчёт бота: баланс сайта не считывается, реальные ставки не выполняются."
        )

    def demo_menu(self, user_id):
        ledger = self._demo_ledger(user_id)
        return {"inline_keyboard": [
            [{"text": "⏸ Приостановить демо" if ledger.enabled else "▶️ Продолжить демо" if ledger.active else "▶️ Включить демо", "callback_data": "demo_toggle"}],
            [{"text": f"🎯 Начать цикл после: {ledger.trigger_streak} раундов", "callback_data": "demo_trigger_menu"}],
            [{"text": f"🔄 Обновить баланс: {format_dollars(ledger.balance_cents)}", "callback_data": "demo_refresh_balance"}],
            [{"text": f"💰 Изменить стартовую ставку: {format_dollars(ledger.base_stake_cents)}", "callback_data": "demo_set_stake"}],
            [{"text": "📜 Посмотреть историю демо-циклов", "callback_data": "demo_history"}],
            [{"text": "♻️ Сбросить демо-баланс и циклы", "callback_data": "demo_reset"}],
            *self._navigation_rows(),
        ]}

    def _choice_screen(self, user_id, kind):
        ledger = self._demo_ledger(user_id)
        if kind == "demo_trigger_menu":
            text = ("🎯 Условие запуска демо-цикла\n"
                    "Сколько результатов подряд должны быть ниже 2x, чтобы начать виртуальный цикл? "
                    "Первая ставка — в следующем раунде. Это не настройка уведомлений.")
            if ledger.active:
                text += " Текущий цикл продолжится; выбранное условие применяется к следующему циклу."
            choices = [(f"После {n} раундов", f"demo_trigger:{n}", n == ledger.trigger_streak) for n in TRIGGER_OPTIONS]
            back = "demo_menu"
        elif kind == "settings_threshold":
            text = ("🔔 Когда присылать уведомление о серии?\n"
                    "Выберите число результатов подряд ниже 2x. Бот пришлёт одно сообщение и будет обновлять его до конца серии. "
                    "Это не меняет условие запуска демо.")
            choices = [(f"После {n} раундов", f"threshold:{n}", n == self.user_threshold(user_id)) for n in range(3, 11)]
            back = "menu_settings"
        elif kind == "settings_history":
            text = "📋 Размер истории коэффициентов\nСколько последних завершённых раундов показывать в панели?"
            choices = [(f"{n} результатов", f"history_size:{n}", n == self.user_history_size(user_id)) for n in (20, 50, 100)]
            back = "menu_settings"
        else:
            text = ("⏱ Когда удалять уведомление о серии?\n"
                    "Таймер начинается после результата 2x или выше, когда серия завершилась. "
                    "Пока серия идёт, сообщение обновляется на месте.")
            choices = [(label, f"delete:{n}" if n else "delete:off", n == self.user_delete_seconds(user_id))
                       for n, label in ((5, "Через 5 секунд"), (30, "Через 30 секунд"), (60, "Через 60 секунд"), (0, "Без таймера удаления"))]
            back = "menu_settings"
        rows = [[{"text": ("✓ " if selected else "") + label, "callback_data": action}] for label, action, selected in choices]
        return text, {"inline_keyboard": [*rows, *self._navigation_rows(back)]}

    @staticmethod
    def _cycle_time(value):
        if not value:
            return "время не сохранено"
        try:
            instant = datetime.fromisoformat(value)
            if instant.tzinfo is None:
                instant = instant.replace(tzinfo=timezone.utc)
            return instant.astimezone(THAILAND_TIME).strftime("%d.%m.%Y %H:%M:%S")
        except (ValueError, TypeError):
            return "время не сохранено"

    def _demo_history_text(self, user_id, page=0):
        history = self._demo_ledger(user_id).history
        if not history:
            return "📜 История демо-циклов\nЦиклов пока нет. Включите демо и дождитесь выбранного условия запуска."
        page = min(max(0, page), len(history) - 1)
        entry = history[page]
        lines = [f"📜 Демо-цикл {page + 1} из {len(history)} · новые сначала",
                 "Время Таиланда, UTC+7",
                 f"Начало: {self._cycle_time(entry.get('started_at'))}",
                 f"Завершение: {self._cycle_time(entry.get('ended_at'))}", ""]
        for step, item in enumerate(entry.get("rounds", []), 1):
            result = "выход на 2x" if item.get("payout_cents", 0) else "проигрыш"
            lines.append(f"Шаг {step}: {format_dollars(item.get('stake_cents', 0))} · раунд {item.get('coefficient', '?')}x · {result}")
        net = int(entry.get("net_cents", 0))
        result = ("Потраченные средства возвращены." if entry.get("outcome") == "recovered"
                  else "Цикл остановлен: не хватило виртуального баланса на следующий шаг.")
        lines += ["", result,
                  f"Всего поставлено: {format_dollars(entry.get('total_stake_cents', 0))}",
                  f"Выплата: {format_dollars(entry.get('payout_cents', 0))}",
                  f"Чистый результат: {'+' if net > 0 else ''}{format_dollars(net)}",
                  f"Баланс: {format_dollars(entry.get('balance_before_cents', 0))} → {format_dollars(entry.get('balance_after_cents', 0))}"]
        return "\n".join(lines)

    def _demo_history_menu(self, user_id, page=0):
        count = len(self._demo_ledger(user_id).history)
        page = min(max(0, page), max(0, count - 1))
        pager = []
        if page:
            pager.append({"text": "⬅️ Более новый цикл", "callback_data": f"demo_history:{page - 1}"})
        if page + 1 < count:
            pager.append({"text": "Более старый цикл ➡️", "callback_data": f"demo_history:{page + 1}"})
        return {"inline_keyboard": [*([pager] if pager else []), *self._navigation_rows("demo_menu")]}

    async def _demo_input(self, user_id, chat_id, raw, message_id=None):
        field = self.demo_pending_inputs.pop(user_id, None)
        cents = parse_dollars(raw)
        if field is None:
            return False
        ledger = self._demo_ledger(user_id)
        if field == "stake" and ledger.active:
            await self._safe_delete(chat_id, message_id)
            await self._send_transient(user_id, chat_id,
                "Цикл уже начался. Изменить стартовую ставку можно после его завершения или сброса.",
                self.demo_menu(user_id))
            return True
        updated = False
        if cents is None:
            answer = "❌ Введите сумму в USD, не более двух знаков после точки."
            self.demo_pending_inputs[user_id] = field
        elif field == "balance" and ledger.set_balance(cents):
            await self._persist_demo(user_id)
            answer = f"✅ Виртуальный баланс установлен: {format_dollars(cents)}"
            updated = True
        elif field == "stake" and ledger.set_base_stake(cents):
            await self._persist_demo(user_id)
            answer = f"✅ Стартовая ставка установлена: {format_dollars(cents)}"
            updated = True
        else:
            answer = (
                f"❌ Введите сумму от $10 до {format_dollars(ledger.balance_cents)}. "
                "Используйте не более двух знаков после точки, например 25.50."
            )
            self.demo_pending_inputs[user_id] = field
        await self._safe_delete(chat_id, message_id)
        markup = self.demo_menu(user_id) if updated else {
            "inline_keyboard": [[{"text": "⬅️ Отмена", "callback_data": "demo_input_cancel"}]]
        }
        await self._send_transient(user_id, chat_id, answer, markup)
        return True

    def _settings_text(self, user_id):
        notifications = "включены" if user_id not in self.notifications_disabled else "выключены"
        sound = "без звука" if user_id in self.silent_users else "со звуком"
        delay = self.user_delete_seconds(user_id)
        timer = f"через {delay} сек. после конца серии" if delay else "без таймера"
        return (
            "⚙️ Настройки уведомлений и истории\n"
            f"Уведомления о серии: {notifications}, {sound}.\n"
            f"Сообщать после {self.user_threshold(user_id)} раундов подряд ниже 2x.\n"
            f"Удалять уведомление: {timer}.\n"
            f"История: {self.user_history_size(user_id)} раундов.\n\n"
            "Нажмите нужную настройку, чтобы изменить её. Условие запуска демо задаётся отдельно в демо-режиме."
        )

    def settings_menu(self, user_id):
        enabled = user_id not in self.notifications_disabled
        sound = user_id not in self.silent_users
        delay = self.user_delete_seconds(user_id)
        return {"inline_keyboard": [
            [{"text": f"🔔 Уведомления о сериях: {'включены' if enabled else 'выключены'}", "callback_data": "settings_notifications"}],
            [{"text": f"🎯 Уведомлять после: {self.user_threshold(user_id)} раундов", "callback_data": "settings_threshold"}],
            [{"text": f"📋 В истории: {self.user_history_size(user_id)} результатов", "callback_data": "settings_history"}],
            [{"text": f"⏱ После конца серии: {str(delay) + ' сек.' if delay else 'без таймера'}", "callback_data": "settings_delete"}],
            [{"text": f"🔊 Звук уведомлений: {'включён' if sound else 'выключен'}", "callback_data": "settings_sound"}],
            [{"text": "🧪 Отправить тестовое уведомление", "callback_data": "settings_test"}],
            [{"text": "♻️ Вернуть настройки по умолчанию", "callback_data": "settings_reset"}],
            *self._navigation_rows(),
        ]}

    def _admin_menu(self):
        rows = []
        for target in sorted(self.authorized | self.blocked):
            if self.is_admin(target):
                continue
            label = "⛔ Заблокирован" if target in self.blocked else "👤 Пользователь"
            rows.append([{"text": f"{label} · ID {target}", "callback_data": f"admin_user:{target}"}])
        return {"inline_keyboard": [*rows, *self._navigation_rows()]}

    def _admin_user_screen(self, target):
        blocked = target in self.blocked
        state = "заблокирован" if blocked else "есть доступ" if target in self.authorized else "нужно войти с паролем"
        rows = []
        if not self.is_admin(target):
            if blocked:
                rows.append([{"text": "🔓 Разблокировать пользователя", "callback_data": f"admin_unblock:{target}"}])
            else:
                rows += [[{"text": "🔑 Отозвать доступ", "callback_data": f"admin_revoke:{target}"}],
                         [{"text": "⛔ Заблокировать пользователя", "callback_data": f"admin_block:{target}"}]]
        return (f"🛡 Пользователь ID {target}\nСтатус: {state}.\nВыберите действие. Перед изменением доступа потребуется подтверждение.",
                {"inline_keyboard": [*rows, *self._navigation_rows("admin_users")]})

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
            except (httpx.HTTPError, RuntimeError) as exc:
                description = str(exc).lower()
                if isinstance(exc, httpx.HTTPStatusError):
                    try:
                        description = str(exc.response.json().get("description", "")).lower()
                    except (ValueError, AttributeError):
                        pass
                if "message is not modified" in description:
                    return
                if "message to edit not found" not in description and "message_id_invalid" not in description:
                    raise
                self.prompt_messages.pop(user_id, None)
        result = await self.request("sendMessage", {"chat_id": chat_id, "text": text})
        message_id = result.get("result", {}).get("message_id")
        if message_id is not None:
            self.prompt_messages[user_id] = int(message_id)

    async def _delete_prompt(self, user_id, chat_id):
        message_id = self.prompt_messages.pop(user_id, None)
        await self._safe_delete(chat_id, message_id)

    async def _delete_transient_source(self, user_id, chat_id, callback):
        # Navigation reuses the existing menu. Adopt an old menu after restart.
        message_id = self._to_int((callback.get("message") or {}).get("message_id"))
        if message_id is None or message_id in {
            self.history_messages.get(user_id), self.active_messages.get(user_id)
        }:
            return
        self.menu_messages.setdefault(user_id, message_id)

    async def _send_transient(self, user_id, chat_id, text, reply_markup):
        lock = self._menu_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            self.demo_panel_messages.pop(user_id, None)
            self._demo_panel_content.pop(user_id, None)
            message_id = self.menu_messages.get(user_id)
            if message_id is not None:
                try:
                    await self.request("editMessageText", {
                        "chat_id": chat_id, "message_id": message_id,
                        "text": text, "reply_markup": reply_markup,
                    })
                except (httpx.HTTPError, RuntimeError) as exc:
                    description = str(exc).lower()
                    if isinstance(exc, httpx.HTTPStatusError):
                        try:
                            description = str(exc.response.json().get("description", "")).lower()
                        except (ValueError, AttributeError):
                            pass
                    if "message is not modified" in description:
                        pass
                    elif "message to edit not found" in description or "message_id_invalid" in description:
                        self.menu_messages.pop(user_id, None)
                        message_id = None
                    else:
                        # Network errors must never create duplicate menus.
                        raise
            if message_id is None:
                result = await self.request("sendMessage", {
                    "chat_id": chat_id, "text": text, "reply_markup": reply_markup,
                })
                message_id = result.get("result", {}).get("message_id")
            if message_id is not None:
                self.menu_messages[user_id] = int(message_id)
                if any(button.get("callback_data") == "demo_toggle"
                       for row in reply_markup.get("inline_keyboard", []) for button in row):
                    self.demo_panel_messages[user_id] = int(message_id)
                    self._demo_panel_content[user_id] = (text, reply_markup)

    async def _refresh_demo_panel(self, user_id):
        lock = self._menu_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            message_id = self.demo_panel_messages.get(user_id)
            chat_id = self.chats.get(user_id)
            if message_id is None or chat_id is None:
                return
            text, markup = self._demo_text(user_id), self.demo_menu(user_id)
            if self._demo_panel_content.get(user_id) == (text, markup):
                return
            try:
                await self.request("editMessageText", {
                    "chat_id": chat_id, "message_id": message_id,
                    "text": text, "reply_markup": markup,
                })
                if self.demo_panel_messages.get(user_id) == message_id:
                    self._demo_panel_content[user_id] = (text, markup)
            except (httpx.HTTPError, RuntimeError):
                log.warning("Demo panel update failed: user_id=%s", user_id)

    def _status_text(self, user_id):
        uptime = max(0, int(time.monotonic() - self.started_at))
        hours, rem = divmod(uptime, 3600)
        minutes, seconds = divmod(rem, 60)
        values = list(self.history_provider() if self.history_provider else [])
        latest = self.last_round or (self._format_coefficient(values[0]) if values else None) or "нет данных"
        if self.last_round_at is None:
            freshness = "ожидаю первый раунд"
            monitor_state = "нет свежих данных"
        else:
            age = max(0, int(time.time() - self.last_round_at))
            freshness = f"обновлено {age} сек. назад"
            monitor_state = "раунд получен недавно" if age <= 90 else "раундов не было более 90 сек."

        probe = self.demo_probe
        demo_state = "не проверен"
        if probe is not None:
            if probe.requested:
                demo_state = "проверяется"
            else:
                demo_state = {
                    "region_blocked": "сайт недоступен из региона сервера",
                    "verification": "страница запросила проверку",
                    "demo_available": "демо-кнопка найдена, баланс не подтверждён",
                    "game_loaded": "интерфейс загружен, баланс не подтверждён",
                    "unconfirmed": "не подтверждён",
                    "timeout": "проверка превысила время ожидания",
                    "error": "ошибка чтения страницы",
                }.get(probe.result, "не проверен")

        return (
            f"📡 Статус мониторинга: {monitor_state}\n"
            f"Последний коэффициент: {latest}\n"
            f"Данные: {freshness}\n"
            f"Деморежим: {demo_state}\n"
            f"Баланс демо-счёта (виртуальный): {format_dollars(self._demo_ledger(user_id).balance_cents)}\n"
            f"История: {min(len(values), self.user_history_size(user_id))} раундов\n"
            f"Серия ниже 2x: {len(self.current_streak)} из {self.user_threshold(user_id)}\n"
            f"Аптайм: {hours:02d}:{minutes:02d}:{seconds:02d}"
        )

    async def _show_status(self, user_id, chat_id):
        await self._send_transient(
            user_id,
            chat_id,
            self._status_text(user_id),
            {"inline_keyboard": self._navigation_rows()},
        )

    async def _prepare_start(self, user_id, chat_id):
        """Explicit /start recovers panels even after a locally cleared chat."""
        history_lock = self._history_locks.setdefault(user_id, asyncio.Lock())
        menu_lock = self._menu_locks.setdefault(user_id, asyncio.Lock())
        async with history_lock, menu_lock:
            self.chats[user_id] = chat_id
            ids = {self.history_messages.pop(user_id, None),
                   self.menu_messages.pop(user_id, None),
                   self.prompt_messages.pop(user_id, None)}
            self.history_hidden.discard(user_id)
            self.demo_pending_inputs.pop(user_id, None)
            self.pending_confirmations.pop(user_id, None)
            self.admin_pending_actions.pop(user_id, None)
            self.demo_panel_messages.pop(user_id, None)
            self._demo_panel_content.pop(user_id, None)
            for message_id in ids:
                await self._safe_delete(chat_id, message_id)
            await self._persist_user(user_id)

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
            if command in {"/start", "/menu", "/settings", "/status", "/help"}:
                self.demo_pending_inputs.pop(user_id, None)
                self.pending_confirmations.pop(user_id, None)
                self.admin_pending_actions.pop(user_id, None)
            self.chats[user_id] = chat_id
            if command == "/start":
                log.info("Telegram start: user_id=%s authorized=True", user_id)
                await self._prepare_start(user_id, chat_id)
                await self.ensure_private_history(user_id)
                await self._send_transient(user_id, chat_id,
                    self._main_text(), self.menu(user_id))
            elif command == "/menu":
                await self._send_transient(
                    user_id,
                    chat_id,
                    self._main_text(),
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
                    self._help_text(),
                    {"inline_keyboard": self._navigation_rows()},
                )
            elif user_id in self.demo_pending_inputs and value == "/cancel":
                self.demo_pending_inputs.pop(user_id, None)
                await self._send_transient(
                    user_id, chat_id, self._demo_text(user_id), self.demo_menu(user_id)
                )
            elif user_id in self.demo_pending_inputs and not command.startswith("/"):
                await self._demo_input(user_id, chat_id, value, message_id)
                return
            await self._safe_delete(chat_id, message_id)
            return

        if command == "/start":
            log.info("Telegram start: user_id=%s authorized=False", user_id)
            await self._prepare_start(user_id, chat_id)
            self.pending.add(user_id)
            await self._replace_prompt(user_id, chat_id, "🔐 Вход в Lucky Jet\nВведите пароль сообщением в этот чат. Сообщение с паролем будет удалено.")
            await self._safe_delete(chat_id, message_id)
            return

        await self._safe_delete(chat_id, message_id)
        if user_id not in self.pending:
            await self._replace_prompt(user_id, chat_id, "Для начала нажмите /start.")
            return

        if self.blocked_until.get(user_id, 0) > time.monotonic():
            await self._replace_prompt(user_id, chat_id, "⏳ Повторите попытку через 5 минут.")
            return

        if hmac.compare_digest(value.encode("utf-8"), self.password.encode("utf-8")):
            self.authorized.add(user_id)
            self.chats[user_id] = chat_id
            self.pending.discard(user_id)
            self.failures.pop(user_id, None)
            self.blocked_until.pop(user_id, None)
            await self._persist_user(user_id)
            self.history_hidden.discard(user_id)
            await self.ensure_private_history(user_id)
            await self._send_transient(user_id, chat_id,
                self._main_text(), self.menu(user_id))
            await self._delete_prompt(user_id, chat_id)
            log.info("Telegram login completed: user_id=%s", user_id)
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
        history_toggle = (
            data == "menu_history" and user_id is not None and chat_id is not None
            and user_id in self.authorized
            and (user_id not in self.blocked or self.is_admin(user_id))
        )
        message_id = self._to_int((callback.get("message") or {}).get("message_id"))
        history_refresh = (
            data in {"history_refresh", "noop"} and user_id is not None
            and chat_id is not None and user_id in self.authorized
            and (user_id not in self.blocked or self.is_admin(user_id))
            and message_id == self.history_messages.get(user_id)
            and user_id not in self.history_hidden
        )
        if not history_toggle and not history_refresh:
            try:
                answer = {"callback_query_id": callback["id"]}
                if user_id not in self.authorized:
                    answer.update(text="Нажмите /start и введите пароль для доступа.", show_alert=True)
                await self.request("answerCallbackQuery", answer)
            except (httpx.HTTPError, RuntimeError, KeyError):
                pass

        if user_id is None or chat_id is None or not await self.is_authorized(user_id):
            return
        if user_id in self.blocked and not self.is_admin(user_id):
            return

        self.chats[user_id] = chat_id
        self.demo_pending_inputs.pop(user_id, None)
        if data not in {"settings_reset_confirm", "demo_reset_confirm"}:
            self.pending_confirmations.pop(user_id, None)
        if data != "admin_confirm" and not data.startswith(("admin_revoke:", "admin_block:", "admin_unblock:")):
            self.admin_pending_actions.pop(user_id, None)

        if history_refresh:
            task = asyncio.create_task(self._refresh_history_callback(user_id, callback))
            self._refresh_tasks.add(task)
            task.add_done_callback(self._refresh_tasks.discard)
            return

        if data in {"noop", "history_refresh"}:
            return

        if data in {"demo_probe", "demo_probe_status"}:
            if not self.is_admin(user_id) or self.demo_probe is None:
                return
            if data == "demo_probe":
                self.demo_probe.start()
            await self._send_transient(user_id, chat_id, self.demo_probe.text(), {
                "inline_keyboard": [
                    [{"text": "Обновить статус", "callback_data": "demo_probe_status"}],
                    [{"text": "Повторить проверку", "callback_data": "demo_probe"}],
                    *self._navigation_rows(),
                ]
            })
            return

        if data == "menu_open":
            await self._send_transient(
                user_id,
                chat_id,
                self._main_text(),
                self.menu(user_id),
            )
            return

        if data == "menu_history":
            success = await self.toggle_private_history(user_id)
            answer = {"callback_query_id": callback["id"]}
            if not success:
                answer.update(text="Не удалось переключить историю. Повторите попытку.", show_alert=True)
            elif user_id in self.menu_messages:
                await self._send_transient(user_id, chat_id, self._main_text(), self.menu(user_id))
            try:
                await self.request("answerCallbackQuery", answer)
            except (httpx.HTTPError, RuntimeError, KeyError):
                pass
            return

        if not self._supported_callback(data, user_id):
            return

        await self._delete_transient_source(user_id, chat_id, callback)

        if data == "menu_close":
            message_id = self.menu_messages.pop(user_id, None)
            self.demo_panel_messages.pop(user_id, None)
            self._demo_panel_content.pop(user_id, None)
            await self._safe_delete(chat_id, message_id)
            return
        if data == "menu_main":
            answer = self._main_text()
            markup = self.menu(user_id)
        elif data == "menu_status":
            answer = self._status_text(user_id)
            markup = {"inline_keyboard": self._navigation_rows()}
        elif data == "menu_stats":
            answer = self._summary_text(user_id)
            markup = {"inline_keyboard": self._navigation_rows()}
        elif data == "menu_notifications":
            if user_id in self.notifications_disabled:
                self.notifications_disabled.remove(user_id)
                answer = "🔔 Уведомления включены."
            else:
                self.notifications_disabled.add(user_id)
                answer = "🔕 Уведомления выключены."
            if user_id in self.notifications_disabled:
                await self._clear_streak_notice(user_id)
            await self._persist_user(user_id)
            markup = self.menu(user_id)
        elif data == "menu_help":
            answer, markup = self._help_text(), {"inline_keyboard": self._navigation_rows()}
        elif data in {"settings_threshold", "settings_history", "settings_delete", "demo_trigger_menu"}:
            answer, markup = self._choice_screen(user_id, data)
        elif data == "menu_settings":
            answer = self._settings_text(user_id)
            markup = self.settings_menu(user_id)
        elif data == "demo_menu":
            answer = self._demo_text(user_id)
            markup = self.demo_menu(user_id)
        elif data == "demo_toggle":
            ledger = self._demo_ledger(user_id)
            was_enabled = ledger.enabled
            ledger.enabled = not ledger.enabled
            if ledger.enabled and not was_enabled:
                trigger_event = ledger.sync_history(
                    self.history_provider() if self.history_provider else []
                )
            else:
                trigger_event = None
            await self._persist_demo(user_id)
            answer = self._demo_text(user_id)
            if trigger_event and trigger_event["type"] == "insufficient_balance":
                answer += (
                    f"\nНедостаточно баланса для стартовой ставки "
                    f"{format_dollars(trigger_event['needed_cents'])}."
                )
            markup = self.demo_menu(user_id)
        elif data.startswith("demo_trigger:"):
            value = self._to_int(data.split(":", 1)[1])
            ledger = self._demo_ledger(user_id)
            if value not in TRIGGER_OPTIONS or not ledger.set_trigger(value):
                return
            if ledger.enabled:
                ledger.sync_history(
                    self.history_provider() if self.history_provider else []
                )
            await self._persist_demo(user_id)
            answer = self._demo_text(user_id)
            markup = self.demo_menu(user_id)
        elif data in {"demo_refresh_balance", "demo_set_balance"}:
            self.demo_pending_inputs.pop(user_id, None)
            answer = self._demo_text(user_id)
            markup = self.demo_menu(user_id)
        elif data == "demo_set_stake":
            if self._demo_ledger(user_id).active:
                answer = "💰 Стартовая ставка\nСейчас цикл ещё не завершён. Изменить ставку можно после его завершения или сброса демо-цикла."
                markup = {"inline_keyboard": self._navigation_rows("demo_menu")}
            else:
                self.demo_pending_inputs[user_id] = "stake"
                answer = "💰 Изменить стартовую ставку\nВведите сумму в долларах, например 10 или 25.50. Минимум $10; сумма не должна превышать виртуальный баланс."
                markup = {"inline_keyboard": [[{"text": "⬅️ Отменить ввод", "callback_data": "demo_input_cancel"}]]}
        elif data == "demo_input_cancel":
            self.demo_pending_inputs.pop(user_id, None)
            answer = self._demo_text(user_id)
            markup = self.demo_menu(user_id)
        elif data == "demo_history" or data.startswith("demo_history:"):
            page = 0 if data == "demo_history" else self._to_int(data.split(":", 1)[1])
            answer, markup = self._demo_history_text(user_id, page), self._demo_history_menu(user_id, page)
        elif data == "demo_reset":
            self.pending_confirmations[user_id] = "demo"
            state = "включённым" if self._demo_ledger(user_id).enabled else "выключенным"
            answer = ("♻️ Сбросить демо-баланс и циклы?\n"
                      "Баланс станет $10,000.00, текущий цикл будет прерван, история циклов удалится. "
                      f"Ставка и условие запуска сохранятся. Демо останется {state}.")
            markup = {"inline_keyboard": [
                [{"text": "✅ Да, сбросить", "callback_data": "demo_reset_confirm"}],
                [{"text": "Отмена", "callback_data": "demo_menu"}],
            ]}
        elif data == "demo_reset_confirm":
            if self.pending_confirmations.pop(user_id, None) != "demo":
                await self._send_transient(user_id, chat_id,
                    "Подтверждение устарело. Для сброса выберите «Сбросить демо-баланс и циклы» заново.",
                    self.demo_menu(user_id))
                return
            ledger = self._demo_ledger(user_id)
            ledger.reset()
            await self._persist_demo(user_id)
            answer = self._demo_text(user_id)
            markup = self.demo_menu(user_id)
        elif data == "settings_notifications":
            if user_id in self.notifications_disabled:
                self.notifications_disabled.remove(user_id)
            else:
                self.notifications_disabled.add(user_id)
                await self._clear_streak_notice(user_id)
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
        elif data.startswith("history_size:"):
            value = self._to_int(data.split(":", 1)[1])
            if value not in {20, 50, 100}:
                return
            self.history_sizes[user_id] = value
            await self._persist_user(user_id)
            await self.ensure_private_history(user_id)
            answer = self._settings_text(user_id)
            markup = self.settings_menu(user_id)
        elif data.startswith("delete:"):
            raw = data.split(":", 1)[1]
            value = 0 if raw == "off" else self._to_int(raw)
            if value in {10, 15}:
                value = 5
            if value not in {0, 5, 30, 60}:
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
            self.pending_confirmations[user_id] = "settings"
            answer = ("♻️ Вернуть настройки по умолчанию?\n"
                      "Уведомления и звук включатся, порог станет 4 раунда, история — 20 результатов, "
                      "удаление — через 5 секунд после конца серии. Демо-баланс и циклы сохранятся.")
            markup = {"inline_keyboard": [
                [{"text": "✅ Да, вернуть настройки", "callback_data": "settings_reset_confirm"}],
                [{"text": "⬅️ Отмена", "callback_data": "menu_settings"}],
            ]}
        elif data == "settings_reset_confirm":
            if self.pending_confirmations.pop(user_id, None) != "settings":
                await self._send_transient(user_id, chat_id,
                    "Подтверждение устарело. Для сброса выберите «Вернуть настройки по умолчанию» заново.",
                    self.settings_menu(user_id))
                return
            self.thresholds.pop(user_id, None)
            self.delete_seconds.pop(user_id, None)
            self.history_sizes.pop(user_id, None)
            self.notifications_disabled.discard(user_id)
            self.silent_users.discard(user_id)
            await self._persist_user(user_id)
            answer = self._settings_text(user_id)
            markup = self.settings_menu(user_id)
        elif data == "admin_users" and self.is_admin(user_id):
            self.admin_pending_actions.pop(user_id, None)
            answer = (f"🛡 Управление доступом\nС доступом: {len(self.authorized)}. Заблокировано: {len(self.blocked)}.\n"
                      "Выберите пользователя, затем нужное действие.")
            markup = self._admin_menu()
        elif data.startswith("admin_user:") and self.is_admin(user_id):
            answer, markup = self._admin_user_screen(self._to_int(data.split(":", 1)[1]))
        elif data.startswith(("admin_revoke:", "admin_block:", "admin_unblock:")) and self.is_admin(user_id):
            action, raw = data.split(":", 1)
            target = self._to_int(raw)
            if target is None or (self.is_admin(target) and action != "admin_unblock"):
                return
            self.admin_pending_actions[user_id] = (action, target)
            verb, effect = {
                "admin_revoke": ("Отозвать доступ", "Пользователю потребуется заново ввести пароль."),
                "admin_block": ("Заблокировать", "Пользователь потеряет доступ даже с правильным паролем."),
                "admin_unblock": ("Разблокировать", "Пользователь сможет заново войти с паролем."),
            }[action]
            answer = f"🛡 {verb} · ID {target}?\n{effect}"
            markup = {"inline_keyboard": [
                [{"text": "✅ Подтвердить действие", "callback_data": "admin_confirm"}],
                [{"text": "⬅️ Отмена", "callback_data": f"admin_user:{target}"}],
            ]}
        elif data == "admin_confirm" and self.is_admin(user_id):
            pending = self.admin_pending_actions.pop(user_id, None)
            if not pending:
                answer = "Это подтверждение уже недействительно. Выберите действие заново."
            else:
                action, target = pending
                if self.is_admin(target) and action != "admin_unblock":
                    return
                if action == "admin_unblock":
                    self.blocked.discard(target)
                    await self._persist_user(target)
                    answer = f"✅ Пользователь ID {target} разблокирован."
                else:
                    await self._revoke_user(target, block=action == "admin_block")
                    answer = f"✅ Доступ пользователя ID {target} " + ("заблокирован." if action == "admin_block" else "отозван.")
            markup = self._admin_menu()
        else:
            return

        await self._send_transient(user_id, chat_id, answer, markup)

    async def _refresh_history_callback(self, user_id, callback):
        callback_id = callback.get("id")
        try:
            await self.request("answerCallbackQuery", {
                "callback_query_id": callback_id,
                "text": "Проверяю историю…",
            })
        except (httpx.HTTPError, RuntimeError, KeyError):
            log.info("History refresh callback acknowledgement failed")

        try:
            if self.history_refresher is None:
                return

            # The callback is acknowledged before network/browser refresh work.
            # Telegram callback queries expire quickly, so never answer twice.
            result = await asyncio.wait_for(self.history_refresher(), timeout=8)
            if (
                result.get("ok")
                and user_id in self.authorized
                and user_id not in self.history_hidden
            ):
                await self.ensure_private_history(user_id)
            elif not result.get("ok"):
                log.info("Manual history refresh did not confirm a source response")
        except asyncio.CancelledError:
            raise
        except (TimeoutError, httpx.HTTPError, RuntimeError, KeyError):
            log.warning("Manual history refresh failed for user_id=%s", user_id)

    def _summary_text(self, user_id):
        limit = self.user_history_size(user_id)
        raw_values = list(self.history_provider() if self.history_provider else [])[:limit]
        values = []
        for value in raw_values:
            formatted = self._format_coefficient(value)
            if formatted is not None:
                values.append(float(formatted[:-1]))
        if not values:
            return "📈 Сводка\nПока нет завершённых раундов."
        below_two = sum(value < 2 for value in values)
        current_streak = 0
        for value in values:
            if value < 2:
                current_streak += 1
            else:
                break
        return (
            f"📈 Сводка за последние {len(values)} раундов\n"
            f"Средний коэффициент: {sum(values) / len(values):.2f}x\n"
            f"Максимальный: {max(values):.2f}x\n"
            f"Ниже 2x: {below_two}\n"
            f"Текущая серия ниже 2x: {current_streak}\n"
            "Статистика описывает завершённые раунды и не является прогнозом."
        )

    async def _revoke_user(self, user_id, *, block):
        chat_id = self.chats.get(user_id)
        history_message_id = self.history_messages.pop(user_id, None)
        menu_message_id = self.menu_messages.pop(user_id, None)
        streak_message_id = self.active_messages.pop(user_id, None)
        prompt_message_id = self.prompt_messages.pop(user_id, None)
        self.authorized.discard(user_id)
        self.history_hidden.discard(user_id)
        self.pending.discard(user_id)
        self.notifications_disabled.discard(user_id)
        self.silent_users.discard(user_id)
        self.thresholds.pop(user_id, None)
        self.delete_seconds.pop(user_id, None)
        self.history_sizes.pop(user_id, None)
        self.demo_ledgers.pop(user_id, None)
        self.demo_pending_inputs.pop(user_id, None)
        self.chats.pop(user_id, None)
        self.demo_panel_messages.pop(user_id, None)
        self._demo_panel_content.pop(user_id, None)
        if block:
            self.blocked.add(user_id)
        else:
            self.blocked.discard(user_id)
        await self._persist_user(user_id)
        if self.redis:
            await self.redis.hdel("luckyjet:demo_sessions", str(user_id))
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

    def _history_markup(self, user_id=None):
        limit = self.user_history_size(user_id) if user_id is not None else 20
        values = list(self.history_provider() if self.history_provider else [])[:limit]
        rows = []
        for start in range(0, len(values), 5):
            rows.append(
                [
                    {"text": self._colored_label(value), "callback_data": "history_refresh"}
                    for value in values[start : start + 5]
                ]
            )
        rows.append([{"text": "☰ Меню", "callback_data": "menu_open"}])
        return {"inline_keyboard": rows}

    async def toggle_private_history(self, user_id):
        user_id = int(user_id)
        lock = self._history_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            chat_id = self.chats.get(user_id)
            if chat_id is None:
                return False
            message_id = self.history_messages.get(user_id)
            if message_id is not None and user_id not in self.history_hidden:
                try:
                    await self.request("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
                except httpx.HTTPStatusError as exc:
                    description = str(exc.response.json().get("description", "")).lower()
                    if exc.response.status_code != 400 or "message to delete not found" not in description:
                        log.warning("History close rejected: status=%s", exc.response.status_code)
                        return False
                except (httpx.HTTPError, RuntimeError):
                    log.warning("History close failed")
                    return False
                self.history_hidden.add(user_id)
                self.history_messages.pop(user_id, None)
                await self._persist_user(user_id)
                log.info("Private history toggled off")
                return True

            self.history_hidden.discard(user_id)
            if await self._ensure_private_history(user_id):
                await self._persist_user(user_id)
                log.info("Private history toggled on")
                return True
            self.history_hidden.add(user_id)
            await self._persist_user(user_id)
            return False

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
                {"chat_id": chat_id, "text": "🎰 Lucky Jet — история коэффициентов", "reply_markup": markup},
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

    async def _handle_demo_round(self, user_id, coefficient):
        ledger = self.demo_ledgers.get(user_id)
        if ledger is None or not ledger.enabled:
            return
        ledger.process_round(coefficient)
        await self._persist_demo(user_id)

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

    async def _clear_streak_notice(self, user_id):
        ids = {self.active_messages.pop(user_id, None)}
        pending = self.pending_deletions.pop(user_id, None)
        if pending:
            ids.add(pending[0])
        for message_id in ids:
            if message_id not in {self.menu_messages.get(user_id), self.history_messages.get(user_id)}:
                await self._safe_delete(self.chats.get(user_id), message_id)
        await self._persist_user(user_id)

    def _start_streak_deletion(self, user_id, message_id, deadline):
        task = asyncio.create_task(self._resume_streak_delete(user_id, message_id, deadline))
        self._deletion_tasks.add(task)
        task.add_done_callback(self._deletion_tasks.discard)

    async def _resume_streak_delete(self, user_id, message_id, deadline):
        # Keep the persisted deadline on cancellation so a restart resumes it.
        await asyncio.sleep(max(0, deadline - time.time()))
        if self.pending_deletions.get(user_id) != (message_id, deadline):
            return
        try:
            await self.request("deleteMessage", {
                "chat_id": self.chats.get(user_id), "message_id": message_id,
            })
        except (httpx.HTTPError, RuntimeError):
            # Retain the deadline for recovery rather than orphaning the notice.
            log.warning("Streak deletion deferred: user_id=%s", user_id)
            return
        if self.pending_deletions.get(user_id) == (message_id, deadline):
            self.pending_deletions.pop(user_id, None)
            await self._persist_user(user_id)

    async def _update_streak_notice(self, user_id, ending=None):
        chat_id = self.chats.get(user_id)
        if chat_id is None:
            return
        if user_id in self.notifications_disabled:
            await self._clear_streak_notice(user_id)
            return
        message_id = self.active_messages.get(user_id)
        if ending is None and len(self.current_streak) < self.user_threshold(user_id):
            if message_id is not None:
                await self._clear_streak_notice(user_id)
            return
        if message_id is None:
            if ending is not None:
                return
            # Remove the previous completed notice before opening the next one.
            await self._clear_streak_notice(user_id)
            try:
                result = await self.request("sendMessage", {
                    "chat_id": chat_id, "text": "🎰 Lucky Jet | Серия <2x",
                    "reply_markup": self._streak_markup(),
                    "disable_notification": user_id in self.silent_users,
                })
                message_id = result.get("result", {}).get("message_id")
                if message_id is not None:
                    self.active_messages[user_id] = int(message_id)
                    await self._persist_user(user_id)
            except (httpx.HTTPError, RuntimeError):
                log.warning("Streak notification create failed: user_id=%s", user_id)
            return
        try:
            await self.request("editMessageReplyMarkup", {
                "chat_id": chat_id, "message_id": message_id,
                "reply_markup": self._streak_markup(ending=ending),
            })
        except (httpx.HTTPError, RuntimeError):
            # Keep the known id on network failures; never send a duplicate.
            log.info("Streak notification edit not applied: user_id=%s", user_id)
        if ending is not None:
            delay = self.user_delete_seconds(user_id)
            deadline = time.time() + delay if delay else 0
            self.active_messages.pop(user_id, None)
            self.pending_deletions[user_id] = (message_id, deadline)
            await self._persist_user(user_id)
            if deadline:
                self._start_streak_deletion(user_id, message_id, deadline)

    async def on_round(self, coefficient):
        value = self._format_coefficient(coefficient)
        if value is None:
            return
        self.last_round = value
        self.last_round_at = time.time()
        await self.update_private_histories()
        for user_id in list(self.authorized):
            await self._handle_demo_round(user_id, value)
            await self._refresh_demo_panel(user_id)
        number = float(value[:-1])

        if number < 2:
            self.current_streak.append(value)
            for user_id in list(self.authorized):
                await self._update_streak_notice(user_id)
        else:
            for user_id in list(self.active_messages):
                if user_id in self.authorized:
                    await self._update_streak_notice(user_id, ending=value)
            self.current_streak = []

    async def _schedule_delete(self, chat_id, message_id, delay):
        try:
            await asyncio.sleep(delay)
            await self.request("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, RuntimeError):
            log.warning("Private streak deletion failed: chat_id=%s message_id=%s", chat_id, message_id)
