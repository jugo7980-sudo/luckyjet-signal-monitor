"""Private Telegram bot access, settings, alerts, and admin controls."""
import asyncio
import hmac
from mini_app import app_url
import re
from history_ui import footer, pager, button, high_screen
import json
import logging
import os
import secrets
import time
from datetime import datetime, timedelta, timezone

import httpx
from release_reset import apply_release_reset, apply_access_reset
from luckyjet_signal.demo_ledger import (
    DEFAULT_BALANCE_CENTS,
    MAX_HISTORY,
    MINIMUM_STAKE_CENTS,
    TRIGGER_OPTIONS,
    DemoLedger,
    format_dollars,
    parse_dollars,
)

log = logging.getLogger("luckyjet.auth")
MENU_IDLE_SECONDS = 60
RESTART_CONFIRM_SECONDS = 60
RESTART_COOLDOWN_SECONDS = 15 * 60
RESTART_COOLDOWN_KEY = "luckyjet:user_restart:cooldown"
APP_PIN_KEY = "luckyjet:pinned_app_messages"  # legacy entries for cleanup
APP_PIN_REFRESH_KEY = "luckyjet:pinned_app_refresh_20261107_v2"
APP_LAUNCH_TTL_SECONDS = 45


THAILAND_TIME = timezone(timedelta(hours=7), name="Asia/Bangkok")


from history_panels import HistoryPanels
from notification_queue import streak_view
from telegram_delivery import interactive


class TelegramPasswordGate(HistoryPanels):
    def __init__(self, request, history_provider=None, admin_chat_id=None, password=None, history_refresher=None):
        self.request = request
        self.history_provider = history_provider
        self.demo_probe = None
        self.site_account = None
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
        self._command_menu_synced = set()
        self._app_launcher_cleaned = False
        self.history_messages = {}
        self.history_hidden = set()
        self._history_locks = {}
        self._menu_locks = {}
        self.prompt_messages = {}
        self.menu_messages = {}
        self.menu_deadlines = {}
        self._menu_tasks = {}
        self.notifications_disabled = set()
        self.silent_users = set()
        self.thresholds = {}
        self.delete_seconds = {}
        self.history_sizes = {}
        self.demo_ledgers = {}
        self._demo_history_views = {}
        self.demo_history_pages = {}
        self.history_open = set()
        self.demo_pending_inputs = {}
        self.admin_pending_actions = {}
        self.pending_confirmations = {}
        self.demo_panel_messages = {}
        self._demo_panel_content = {}
        self.failures = {}
        self.blocked_until = {}
        self._restart_pending = {}
        self._mini_app_control_locks = {}
        self.offset = None

        self.current_streak = []
        self.active_messages = {}
        self.pending_deletions = {}
        self.last_round = None
        self.last_round_at = None
        self.started_at = time.monotonic()
        self._deletion_tasks = set()

    @property
    def site_account(self):
        return self._site_account

    @site_account.setter
    def site_account(self, account):
        self._site_account = account
        if account is not None:
            account.gate = self

    async def _leave_site_account(self, user_id, **kwargs):
        account = self.site_account
        if account is not None and user_id == self.admin_chat_id and hasattr(account, 'leave'):
            return await account.leave(**kwargs)
        return True

    @property
    def current_streak(self):
        view = streak_view.get()
        return view[1] if view is not None and view[0] is self else self._current_streak

    @current_streak.setter
    def current_streak(self, value):
        self._current_streak = value

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
                await apply_access_reset(self.redis)
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
                        {"command": "app", "description": "Открыть приложение"},
                        {"command": "menu", "description": "Открыть меню"},
                        {"command": "status", "description": "Статус мониторинга"},
                        {"command": "settings", "description": "Настройки"},
                        {"command": "restart", "description": "Перезапустить личный интерфейс бота"},
                    ]
                },
            )
            await self.request("setChatMenuButton", {
                "menu_button": {"type": "commands"}
            })
        except (httpx.HTTPError, RuntimeError):
            log.warning("Telegram command registration failed")

        # Existing per-chat Web App overrides take priority over the global
        # commands menu. Explicitly replace those we know, plus admin's chat.
        private_chat_ids = {chat for chat in self.chats.values() if chat > 0}
        if self.admin_chat_id is not None and self.admin_chat_id > 0:
            private_chat_ids.add(self.admin_chat_id)
        for chat_id in sorted(private_chat_ids):
            await self._register_commands_button(chat_id)
        # Remove every old bot-owned launcher and stop rebuilding pins.
        # The command menu is the only permanent place to launch the app.
        await self._clean_legacy_pinned_launchers(private_chat_ids)
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
                    updates = result.get("result", [])
                    groups = {}
                    for update in updates:
                        event = update.get("callback_query") or update.get("message") or {}
                        uid = event.get("from", {}).get("id", 0)
                        groups.setdefault(uid, []).append(update)
                    slots = asyncio.Semaphore(16)

                    async def handle_user(batch):
                        async with slots:
                            token = interactive.set(True)
                            try:
                                await handle_batch(batch)
                            finally:
                                interactive.reset(token)

                    async def handle_batch(batch):
                            for update in batch:
                                try:
                                    await self.handle_update(update)
                                except (httpx.HTTPError, RuntimeError, ValueError, KeyError, TypeError) as exc:
                                    log.warning("Update handler failed: %s", type(exc).__name__)

                    async with asyncio.TaskGroup() as tasks:
                        for batch in groups.values():
                            tasks.create_task(handle_user(batch))
                    if updates:
                        self.offset = max(update["update_id"] for update in updates) + 1
                except asyncio.CancelledError:
                    raise
                except (httpx.HTTPError, RuntimeError, ValueError, KeyError, TypeError) as exc:
                    log.warning("Telegram polling failed: %s", type(exc).__name__)
                    await asyncio.sleep(2)
        finally:
            await self.close()

    async def close(self):
        notices = getattr(self, "notification_queue", None)
        if notices is not None:
            await notices.close()
        panels = getattr(self, "live_panels", None)
        if panels is not None:
            await panels.close()
        menu_tasks = list(self._menu_tasks.values())
        for task in menu_tasks:
            task.cancel()
        if menu_tasks:
            await asyncio.gather(*menu_tasks, return_exceptions=True)
        self._menu_tasks.clear()
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
        legacy_durations = [uid for uid, value in self.delete_seconds.items() if value in {5, 15}]
        if legacy_durations:
            pipe = self.redis.pipeline()
            for uid in legacy_durations:
                self.delete_seconds[uid] = 10
                pipe.hset("luckyjet:delete_seconds", str(uid), "10")
            await pipe.execute()
        self.history_sizes = {
            int(k): int(v)
            for k, v in (await self.redis.hgetall("luckyjet:history_sizes")).items()
        }
        self.demo_ledgers = {}
        self._demo_history_views = {}
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
        return min(10, max(2, value))

    def user_delete_seconds(self, user_id):
        value = int(self.delete_seconds.get(int(user_id), 10))
        if value in {5, 15}:
            return 10
        return value if value in {0, 10, 30, 60} else 10

    def user_history_size(self, user_id):
        value = int(self.history_sizes.get(int(user_id), 20))
        return value if value in {20, 30, 50, 100} else 20

    def _navigation_rows(self, back=None, back_label="⬅️ Назад"):
        return footer(back) if back else footer("menu_main")[1:]

    @staticmethod
    def _main_text():
        return "☰"

    def _supported_callback(self, data, user_id):
        if data in {
            "menu_open", "menu_close", "menu_main", "menu_status", "menu_stats",
            "menu_notifications", "menu_settings", "settings_notifications",
            "settings_sound", "settings_test", "settings_reset", "demo_menu",
            "demo_toggle", "demo_refresh_balance", "demo_set_balance", "demo_set_stake", "demo_input_cancel",
            "demo_daily", "demo_daily_date", "coef_date", "hi_date", "demo_history", "demo_stats", "demo_reset", "demo_reset_confirm", "demo_trigger_menu",
            "settings_threshold", "settings_history", "settings_delete", "settings_reset_confirm", "menu_help",
        }:
            return True
        if data.startswith("demo_daily:"):
            try:
                return datetime.strptime(data.split(":", 1)[1], "%Y-%m-%d").date() <= datetime.now(THAILAND_TIME).date()
            except ValueError:
                return False
        if data.startswith(("hi_", "demo_daily_days:")):
            return bool(re.fullmatch(r"(?:hi_days|demo_daily_days):[0-9]{1,6}|hi_day:[0-9]{4}-[0-9]{2}-[0-9]{2}:[0-9]{1,6}|hi_case:[a-f0-9]{16}:[0-9]{1,6}", data)) and (
                not data.startswith('hi_day:') or self._valid_history_date(data.split(':')[1]))
        if data.startswith("coef_days:"):
            return len(data.split(":")) == 2 and data.split(":")[1].isdigit() and int(data.split(":")[1]) < 10000
        if data.startswith("coef_day:"):
            parts = data.split(":")
            if len(parts) != 4 or (parts[2] != "10" or not parts[3].isdigit() or len(parts[3]) > 6):
                return False
            try:
                datetime.strptime(parts[1], "%Y-%m-%d")
                return True
            except ValueError:
                return False
        if data.startswith(("demo_list:", "demo_cycle:")):
            parts = data.split(":")
            return (len(parts) == 3 and len(parts[1]) == 8
                    and all(c in "0123456789abcdef" for c in parts[1])
                    and self._to_int(parts[2]) is not None and 0 <= self._to_int(parts[2]) < MAX_HISTORY)
        if data.startswith("demo_history:"):
            page = self._to_int(data.split(":", 1)[1])
            return page is not None and page >= 0
        if self.is_admin(user_id) and data.startswith("admin_user:"):
            return self._to_int(data.split(":", 1)[1]) is not None
        if self.is_admin(user_id) and data == "admin_confirm":
            return True
        if data.startswith("threshold:"):
            return self._to_int(data.split(":", 1)[1]) in range(2, 11)
        if data.startswith("history_size:"):
            return self._to_int(data.split(":", 1)[1]) in {20, 30, 50, 100}
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
            [{"text": "📊 Показать историю" if hidden else "📊 Скрыть историю", "callback_data": "menu_history"}],
            [{"text": "🧪 Демо-режим", "callback_data": "demo_menu"}],
            [{"text": "⚙️ Настройки", "callback_data": "menu_settings"}],
        ]
        if self.site_account is not None and user_id is not None and self.is_admin(user_id):
            rows.append([{"text": "👤 Аккаунт сайта", "callback_data": "site_status"}])
        if user_id is not None:
            balance = format_dollars(self._demo_ledger(user_id).balance_cents)
            rows.append([{"text": f"🔄 💵 Баланс: {balance}", "callback_data": "demo_refresh_balance"}])
        rows.append([{"text": "✖ Закрыть", "callback_data": "menu_close"}])
        return {"inline_keyboard": rows}

    def _demo_text(self, user_id):
        ledger = self._demo_ledger(user_id)
        if not ledger.enabled:
            state = "приостановлен, текущий цикл сохранён" if ledger.active else "выключен"
        else:
            state = "цикл в работе" if ledger.active else "ожидание условия запуска"
        if ledger.active:
            current_net = ledger.balance_cents - ledger.cycle_start_balance_cents
            progress = (
                f"Шаг: {len(ledger.cycle_rounds) + 1}\n"
                f"Следующая виртуальная ставка: {format_dollars(ledger.next_stake_cents)}\n"
                f"Текущий результат: {'+' if current_net > 0 else ''}{format_dollars(current_net)}\n"
                f"Баланс: {format_dollars(ledger.balance_cents)}"
            )
        else:
            progress = (
                f"Сейчас ниже 2x подряд: {ledger.low_streak} из {ledger.trigger_streak}\n"
                f"Баланс: {format_dollars(ledger.balance_cents)}"
            )
        return f"🧪 Демо · {state}\n{progress}"

    @staticmethod
    def _setting_row(label, value, action, style=None):
        value_button = {"text": value, "callback_data": action}
        if style:
            value_button["style"] = style
        return [{"text": label, "callback_data": action}, value_button]

    def demo_menu(self, user_id):
        ledger = self._demo_ledger(user_id)
        return {"inline_keyboard": [
            self._setting_row("🧪 Демо", "⏸ Выключить" if ledger.enabled else "▶️ Включить", "demo_toggle", style="danger" if ledger.enabled else "success"),
            self._setting_row("🎯 Старт: серия <2x", f"{ledger.trigger_streak} подряд", "demo_trigger_menu"),
            self._setting_row("🔄 Демо-баланс", format_dollars(ledger.balance_cents), "demo_refresh_balance"),
            self._setting_row("💰 Первая ставка", format_dollars(ledger.base_stake_cents), "demo_set_stake"),
            [{"text": "📜 История циклов", "callback_data": "demo_history"}],
            [{"text": "📶 Максимальный шаг", "callback_data": "demo_daily_days:0"}],
            [{"text": "📊 Статистика DEMO", "callback_data": "demo_stats"}],
            [{"text": "📈 История Кэф", "callback_data": "coef_days:0"}],
            [{"text": "♻️ Сбросить демо", "callback_data": "demo_reset"}],
            *self._navigation_rows(),
        ]}

    def _choice_screen(self, user_id, kind):
        ledger = self._demo_ledger(user_id)
        if kind == "demo_trigger_menu":
            text = "🎯 Старт демо: сколько раундов подряд ниже 2x?"
            if ledger.active:
                text += "\nТекущая сессия активна. Изменить можно после её завершения."
            choices = [(f"{n} подряд", f"demo_trigger:{n}", n == ledger.trigger_streak) for n in TRIGGER_OPTIONS]
            back = "demo_menu"
        elif kind == "settings_threshold":
            text = "🔔 Уведомлять: сколько раундов подряд ниже 2x?"
            choices = [(f"{n} подряд", f"threshold:{n}", n == self.user_threshold(user_id)) for n in range(2, 11)]
            back = "menu_settings"
        elif kind == "settings_history":
            text = "📋 Сколько результатов показывать в истории?"
            choices = [(f"{n} результатов", f"history_size:{n}", n == self.user_history_size(user_id)) for n in (20, 30, 50, 100)]
            back = "menu_settings"
        else:
            text = "⏱ Удалять уведомление после конца серии через:"
            choices = [(label, f"delete:{n}" if n else "delete:off", n == self.user_delete_seconds(user_id))
                       for n, label in ((10, "Через 10 секунд"), (30, "Через 30 секунд"), (60, "Через 60 секунд"), (0, "Не удалять"))]
            back = "menu_settings"
        buttons = [{"text": ("✓ " if selected else "") + label, "callback_data": action} for label, action, selected in choices]
        columns = 3 if kind in {"settings_threshold", "demo_trigger_menu"} else 2
        rows = [buttons[i:i + columns] for i in range(0, len(buttons), columns)]
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

    @staticmethod
    def _cycle_duration(started_at, ended_at):
        try:
            start = datetime.fromisoformat(started_at)
            end = datetime.fromisoformat(ended_at)
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            seconds = max(0, int((end - start).total_seconds()))
        except (TypeError, ValueError):
            return "неизвестно"
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours} ч {minutes} мин {seconds} сек"
        if minutes:
            return f"{minutes} мин {seconds} сек"
        return f"{seconds} сек"

    def _demo_history_text(self, user_id, page=0, history=None):
        history = self._demo_ledger(user_id).history if history is None else history
        if not history:
            return "📜 История демо-циклов\nЦиклов пока нет."
        page = min(max(0, page), len(history) - 1)
        entry = history[page]
        outcome = entry.get("outcome")
        reason = {
            "recovered": "цель 2x достигнута",
            "insufficient_balance": "недостаточно баланса на следующий шаг",
            "incomplete_data": "неполные данные",
            "stopped_by_user": "остановлена пользователем",
        }.get(outcome, str(outcome or "неизвестно"))
        lines = [
            f"📜 Демо-цикл {page + 1} из {len(history)} · новые сначала",
            "Время Таиланда, UTC+7",
            f"Начало: {self._cycle_time(entry.get('started_at'))}",
            f"Завершение: {self._cycle_time(entry.get('ended_at'))}",
            f"Длительность: {self._cycle_duration(entry.get('started_at'), entry.get('ended_at'))}",
            "",
        ]
        for step, item in enumerate(entry.get("rounds", []), 1):
            stake = int(item.get("stake_cents", 0) or 0)
            payout = int(item.get("payout_cents", 0) or 0)
            step_net = payout - stake if payout else -stake
            step_result = ("+" if step_net > 0 else "") + format_dollars(step_net)
            lines.append(
                f"Шаг {step}: {format_dollars(stake)} · {item.get('coefficient', '?')}x · {step_result}"
            )
        if outcome == "recovered":
            lines.append("")
            lines.append("Потраченные средства возвращены.")
        lines += ["", f"Причина завершения: {reason}",
                  f"Всего поставлено: {format_dollars(entry.get('total_stake_cents', 0) or 0)}"]
        payout = entry.get("payout_cents")
        if payout is not None:
            lines.append(f"Выплата: {format_dollars(payout)}")
        net = entry.get("net_cents")
        if net is not None:
            net = int(net)
            lines.append(f"Чистый результат: {'+' if net > 0 else ''}{format_dollars(net)}")
        lines.append(
            f"Баланс: {format_dollars(entry.get('balance_before_cents', 0) or 0)} → "
            f"{format_dollars(entry.get('balance_after_cents', 0) or 0)}"
        )
        return "\n".join(lines)


    def _demo_stats_text(self, user_id):
        ledger = self._demo_ledger(user_id)
        history = list(ledger.history)
        since_reset = ledger.balance_cents - DEFAULT_BALANCE_CENTS
        period = (f"За последние {MAX_HISTORY} сессий" if len(history) == MAX_HISTORY
                  else f"По истории ({len(history)} из {MAX_HISTORY})")
        successful = sum(entry.get("outcome") == "recovered" for entry in history)
        losses = sum(entry.get("outcome") == "insufficient_balance" for entry in history)
        incomplete = sum(entry.get("outcome") == "incomplete_data" for entry in history)
        stopped = sum(entry.get("outcome") == "stopped_by_user" for entry in history)
        financial = [entry for entry in history
                     if entry.get("outcome") in {"recovered", "insufficient_balance"}
                     and entry.get("net_cents") is not None]
        total_net = sum(int(entry.get("net_cents", 0)) for entry in financial)
        average_steps = (
            sum(len(entry.get("rounds") or []) for entry in financial) / len(financial)
            if financial else 0.0
        )
        return (
            "📊 Статистика DEMO\n"
            f"Всего сессий: {len(history)}\n"
            f"Успешных: {successful}\n"
            f"Проигрышных: {losses}\n"
            f"Неполных: {incomplete}\n"
            f"Остановлено вручную: {stopped}\n"
            f"С последнего сброса: {'+' if since_reset > 0 else ''}{format_dollars(since_reset)}\n"
            f"{period}: {'+' if total_net > 0 else ''}{format_dollars(total_net)}\n"
            f"Среднее шагов: {average_steps:.1f}"
        )


    async def _coefficient_screen(self, data):
        stats = getattr(self, "coefficient_stats", None)
        if stats is None or stats.redis is None:
            return "📈 Статистика появится после подключения хранилища.", {"inline_keyboard": self._navigation_rows("demo_menu")}
        parts=data.split(":")
        if parts[0]=="coef_days":
            rows=[]
            from datetime import datetime,timedelta
            from zoneinfo import ZoneInfo
            today=datetime.now(ZoneInfo("Asia/Bangkok")).date()
            page=int(parts[1])
            dates,total=await stats.days(page)
            pages=max(1,(total+9)//10)
            if page>=pages:
                page=pages-1
                dates,total=await stats.days(page)
            for day in dates:
                label=datetime.strptime(day,'%Y-%m-%d').strftime('%d.%m.%Y')
                rows.append([{"text":label,"callback_data":f"coef_day:{day}:10:{page}"}])
            title=f"📈 История Кэф · {page+1}/{pages}\nТаиланд, UTC+7 · новые сначала"
            if not rows:
                title+="\nДанных пока нет."
            pager=[]
            if page:
                pager.append({"text":"◀","callback_data":f"coef_days:{page-1}"})
            if page+1<pages:
                pager.append({"text":"▶","callback_data":f"coef_days:{page+1}"})
            return title, {"inline_keyboard":rows+([pager] if pager else [])+[[button("📅 Дата", "coef_date"),button("Интервалы >100x", "hi_days:0")]]+self._navigation_rows("demo_menu")}
        _,day,threshold,page=parts
        summary=await stats.summary(day)
        from datetime import datetime
        label=datetime.strptime(day,'%Y-%m-%d').strftime('%d.%m.%Y')
        text=f"📈 История Кэф · {label}\nЗафиксировано раундов: {summary.get('total', 0)}"
        if summary.get('lt2_tracking'):
            label_lt2 = "<2x с обновления" if summary.get('lt2_partial') else "<2x"
            text+=f"\n{label_lt2}: {summary.get('lt2', 0)}"
        else:
            text+="\n<2x: нет данных"
        for t in (2,5):
            value = summary.get(str(t),0) if summary.get('new_threshold_tracking') else 'нет данных'
            suffix = ' с обновления' if summary.get('new_threshold_partial') else ''
            text += f"\n>{t}x{suffix}: {value}"
        text+=''.join(f"\n>{t}x: {summary.get(str(t),0)}" for t in (10,25,50,100,200,300,500,800,1000,1500,2000,3000,5000))
        return text,{"inline_keyboard":self._navigation_rows(f"coef_days:{page}")}


    def _demo_history_menu(self, user_id, page=0):
        count = len(self._demo_ledger(user_id).history)
        page = min(max(0, page), max(0, count - 1))
        pager = []
        if page:
            pager.append({"text": "⬅️ Новый", "callback_data": f"demo_history:{page - 1}"})
        if page + 1 < count:
            pager.append({"text": "Старый ➡️", "callback_data": f"demo_history:{page + 1}"})
        return {"inline_keyboard": [*([pager] if pager else []), *self._navigation_rows("demo_menu")]}

    def _set_demo_history_page(self, user_id, page):
        self.demo_history_pages[user_id] = max(0, int(page))
        self.menu_deadlines[user_id] = 0
        task = self._menu_tasks.pop(user_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _demo_history_list(self, user_id, page=0, token=None):
        view = self._demo_history_views.get(user_id)
        if page == 0 and token is not None:
            view = None
        if not view or token != view[0]:
            token = secrets.token_hex(4)
            history = json.loads(json.dumps(self._demo_ledger(user_id).history))
            view = self._demo_history_views[user_id] = (token, history)
        token, history = view
        pages = max(1, (len(history) + 9) // 10)
        page = min(max(0, page), pages - 1)
        text = f"📜 История демо-циклов · {len(history)} циклов · {page + 1}/{pages}\nТаиланд, UTC+7 · новые сначала"
        if not history:
            text += "\nЦиклов пока нет."
        rows = []
        for index in range(page * 10, min(len(history), (page + 1) * 10)):
            entry = history[index]
            stamp = self._cycle_time(entry.get("ended_at") or entry.get("started_at"))
            if stamp != "время не сохранено":
                stamp = stamp[:5] + " " + stamp[11:16]
            if entry.get("outcome") == "incomplete_data" or entry.get("net_cents") is None:
                result = "⚠️ данные"
            else:
                net = int(entry.get("net_cents", 0))
                result = ("+" if net > 0 else "") + format_dollars(net)
            round_count = len(entry.get("rounds") or [])
            rows.append([{"text": f"{round_count} шаг. · {stamp} · {result}", "callback_data": f"demo_cycle:{token}:{index}"}])
        pager = []
        if page:
            pager.append({"text": "◀", "callback_data": f"demo_list:{token}:{page - 1}"})
        if page + 1 < pages:
            pager.append({"text": "▶", "callback_data": f"demo_list:{token}:{page + 1}"})
        return text, {"inline_keyboard": [*rows, *([pager] if pager else []), *self._navigation_rows("demo_menu", "🧪 Демо-режим")]}

    def _demo_cycle_screen(self, user_id, token, index):
        view = self._demo_history_views.get(user_id)
        if not view or view[0] != token or index >= len(view[1]):
            return self._demo_history_list(user_id)
        text = self._demo_history_text(user_id, index, history=view[1])
        return text, {"inline_keyboard": self._navigation_rows(
            f"demo_list:{token}:{index // 10}", "⬅️ К списку циклов")}

    @staticmethod
    def _valid_history_date(day):
        try:
            return datetime.strptime(day, '%Y-%m-%d').date() <= datetime.now(THAILAND_TIME).date()
        except ValueError:
            return False

    def _daily_days_screen(self, user_id, page):
        values=self._demo_ledger(user_id).daily_max_steps
        dates=sorted(values,reverse=True); pages=max(1,(len(dates)+9)//10); page=min(page,pages-1)
        rows=[[button(f"{datetime.strptime(day,'%Y-%m-%d'):%d.%m.%Y} · {values[day]} шаг.",f"demo_daily:{day}")] for day in dates[page*10:page*10+10]]
        rows+=pager(page,pages,lambda p:f'demo_daily_days:{p}')
        rows+=[[button('📅 Дата','demo_daily_date'),button('Сегодня','demo_daily')]]+footer('demo_menu')
        text=f'Максимальный шаг · {page+1}/{pages}\nТаиланд, UTC+7'
        if not dates:text+='\nЗаписей пока нет.'
        return text,{'inline_keyboard':rows}

    def _demo_daily_screen(self, user_id, day=None):
        today = datetime.now(THAILAND_TIME).date()
        selected = datetime.strptime(day, "%Y-%m-%d").date() if day else today
        value = self._demo_ledger(user_id).daily_max_steps.get(selected.isoformat())
        text = (f"📊 Максимальный шаг за сутки\n{selected:%d.%m.%Y}\n"
                + (f"Максимальный шаг: {value}" if value else "Нет сохранённых данных за этот день.")
                + "\nВремя Таиланда, UTC+7")
        pager = []
        if selected > datetime.min.date():
            pager.append({"text": "◀ Предыдущий день", "callback_data": f"demo_daily:{selected - timedelta(days=1)}"})
        if selected < today:
            pager.append({"text": "Следующий день ▶", "callback_data": f"demo_daily:{selected + timedelta(days=1)}"})
        rows = ([pager] if pager else []) + [[{"text": "📅 Выбрать дату", "callback_data": "demo_daily_date"}],
                [{"text": "Сегодня", "callback_data": "demo_daily"}]]
        return text, {"inline_keyboard": [*rows, *self._navigation_rows("demo_daily_days:0")]}

    async def _demo_input(self, user_id, chat_id, raw, message_id=None):
        field = self.demo_pending_inputs.pop(user_id, None)
        if field in {"daily_date", "coef_date", "hi_date"}:
            await self._safe_delete(chat_id, message_id)
            try:
                selected = datetime.strptime(raw.strip(), "%d.%m.%Y").date()
                if selected > datetime.now(THAILAND_TIME).date():
                    raise ValueError("Future date")
                self.history_open.add(user_id)
                if field == "daily_date":
                    answer, markup = self._demo_daily_screen(user_id, selected.isoformat())
                else:
                    target = f"hi_day:{selected}:0" if field == "hi_date" else f"coef_day:{selected}:10:0"
                    answer, markup = await (high_screen(self.coefficient_stats,target) if field == "hi_date" else self._coefficient_screen(target))
            except ValueError:
                self.demo_pending_inputs[user_id] = field
                answer = "Введите существующую дату в формате ДД.ММ.ГГГГ, не позднее сегодняшней."
                markup = {"inline_keyboard": self._navigation_rows({"daily_date":"demo_daily_days:0","coef_date":"coef_days:0","hi_date":"hi_days:0"}[field])}
            await self._send_transient(user_id, chat_id, answer, markup)
            return True
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
            "inline_keyboard": self._navigation_rows("demo_input_cancel")
        }
        await self._send_transient(user_id, chat_id, answer, markup)
        return True

    def _settings_text(self, user_id):
        return "⚙️ Настройки уведомлений и истории"

    def settings_menu(self, user_id):
        enabled = user_id not in self.notifications_disabled
        sound = user_id not in self.silent_users
        delay = self.user_delete_seconds(user_id)
        return {"inline_keyboard": [
            self._setting_row("🔔 Уведомления", "ВКЛ" if enabled else "ВЫКЛ", "settings_notifications"),
            self._setting_row("🎯 Серия ниже 2x", f"{self.user_threshold(user_id)} подряд", "settings_threshold"),
            self._setting_row("📋 История", f"{self.user_history_size(user_id)} результатов", "settings_history"),
            self._setting_row("⏱ После серии", f"{delay} сек." if delay else "Не удалять", "settings_delete"),
            self._setting_row("🔊 Звук", "ВКЛ" if sound else "ВЫКЛ", "settings_sound"),
            [{"text": "🧪 Тест", "callback_data": "settings_test"}],
            [{"text": "♻️ Сброс настроек", "callback_data": "settings_reset"}],
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
        return (f"🛡 Пользователь ID {target}\nСтатус: {state}.",
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

    def _start_menu_timer(self, user_id):
        task = self._menu_tasks.get(user_id)
        if task is not None and not task.done():
            return
        task = asyncio.create_task(self._menu_timeout(user_id))
        self._menu_tasks[user_id] = task

        def finished(done):
            if self._menu_tasks.get(user_id) is done:
                self._menu_tasks.pop(user_id, None)
        task.add_done_callback(finished)

    def _touch_menu(self, user_id):
        if user_id in self.demo_history_pages:
            self.menu_deadlines[user_id] = 0
            return
        if user_id in self.history_open:
            self.menu_deadlines[user_id] = 0
            task=self._menu_tasks.pop(user_id,None)
            if task is not None and task is not asyncio.current_task(): task.cancel()
            return
        if user_id in self.demo_history_pages:
            return
        if user_id in self.menu_messages:
            self.menu_deadlines[user_id] = time.time() + MENU_IDLE_SECONDS
            self._start_menu_timer(user_id)

    async def _menu_timeout(self, user_id):
        while user_id in self.menu_messages:
            deadline = self.menu_deadlines.get(user_id)
            if deadline is None or deadline == 0:
                return
            await asyncio.sleep(max(0, deadline - time.time()))
            try:
                if await self._close_menu(user_id, expected_deadline=deadline):
                    return
            except (httpx.HTTPError, RuntimeError):
                log.warning("Menu timeout failed: user_id=%s", user_id)
                await asyncio.sleep(5)

    async def _close_menu(self, user_id, *, expected_deadline=None, now=None):
        instant = time.time() if now is None else now
        if expected_deadline is not None and (
                self.menu_deadlines.get(user_id) != expected_deadline or instant < expected_deadline):
            return False
        if not await self._leave_site_account(user_id, expected_deadline=expected_deadline, now=now):
            return False
        lock = self._menu_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            if expected_deadline is not None:
                instant = time.time() if now is None else now
                if self.menu_deadlines.get(user_id) != expected_deadline or instant < expected_deadline:
                    return False
            message_id = self.menu_messages.get(user_id)
            chat_id = self.chats.get(user_id)
            if message_id is not None and chat_id is not None:
                try:
                    await self.request("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
                except httpx.HTTPStatusError as exc:
                    try:
                        description = str(exc.response.json().get("description", "")).lower()
                    except (ValueError, AttributeError):
                        description = ""
                    if exc.response.status_code != 403 and not (
                        exc.response.status_code == 400 and
                        ("message to delete not found" in description or "message_id_invalid" in description)
                    ):
                        raise
            self.menu_messages.pop(user_id, None)
            self.history_open.discard(user_id)
            self.menu_deadlines.pop(user_id, None)
            task = self._menu_tasks.pop(user_id, None)
            if task is not None and task is not asyncio.current_task():
                task.cancel()
            self.demo_panel_messages.pop(user_id, None)
            self._demo_panel_content.pop(user_id, None)
            self._demo_history_views.pop(user_id, None)
            self.demo_history_pages.pop(user_id, None)
            self.demo_pending_inputs.pop(user_id, None)
            self.pending_confirmations.pop(user_id, None)
            self.admin_pending_actions.pop(user_id, None)
            await self._persist_user(user_id)
        if user_id in self.history_messages:
            await self.ensure_private_history(user_id)
        return True

    async def _send_transient(self, user_id, chat_id, text, reply_markup):
        lock = self._menu_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            was_open = user_id in self.menu_messages
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
                self._touch_menu(user_id)
                if any(button.get("callback_data") == "demo_toggle"
                       for row in reply_markup.get("inline_keyboard", []) for button in row):
                    self.demo_panel_messages[user_id] = int(message_id)
                    self._demo_panel_content[user_id] = (text, reply_markup)

        if not was_open and user_id in self.menu_messages and user_id in self.history_messages:
            await self.ensure_private_history(user_id)

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

    async def _register_commands_button(self, chat_id):
        """Remove the legacy per-chat Mini App launcher next to the message field."""
        if chat_id in self._command_menu_synced:
            return
        try:
            await self.request("setChatMenuButton", {
                "chat_id": chat_id,
                "menu_button": {"type": "commands"},
            })
            self._command_menu_synced.add(chat_id)
            if chat_id == self.admin_chat_id:
                try:
                    current = await self.request("getChatMenuButton", {"chat_id": chat_id})
                    data = current.get("result") if isinstance(current, dict) else None
                    actual = data.get("type") if isinstance(data, dict) else None
                    log.info("Admin chat menu button verified: %s", actual)
                except (httpx.HTTPError, RuntimeError):
                    log.warning("Could not verify admin chat menu button")
        except (httpx.HTTPError, RuntimeError):
            log.warning("Could not restore Telegram commands button for chat_id=%s", chat_id)

    async def _clean_legacy_pinned_launchers(self, chats=()):
        """Delete only our old pinned app-launch messages, never unrelated pins.

        Telegram does not let bots list arbitrary chat history. Redis stores
        the IDs of launch messages created by earlier versions; getChat can
        additionally identify the current pin by its bot-owned text.
        """
        if self.redis is None:
            return
        known = await self.redis.hgetall(APP_PIN_KEY)
        targets = set(chats)
        for raw_chat_id in known:
            chat_id = self._to_int(raw_chat_id)
            if chat_id and chat_id > 0:
                targets.add(chat_id)
        removed = 0
        failures = 0
        for chat_id in sorted(targets):
            message_ids = set()
            cached = self._to_int(known.get(str(chat_id)))
            if cached and cached > 0:
                message_ids.add(cached)
            try:
                response = await self.request("getChat", {"chat_id": chat_id})
                info = response.get("result") if isinstance(response, dict) else None
                pinned = info.get("pinned_message") if isinstance(info, dict) else None
                if (isinstance(pinned, dict)
                        and pinned.get("text") == "Lucky Jet · приложение"):
                    pinned_id = self._to_int(pinned.get("message_id"))
                    if pinned_id and pinned_id > 0:
                        message_ids.add(pinned_id)
            except (httpx.HTTPError, RuntimeError):
                log.warning("Cannot check old launcher pin for chat_id=%s", chat_id)
            if not message_ids:
                continue
            succeeded = True
            for message_id in message_ids:
                try:
                    await self.request("unpinChatMessage", {
                        "chat_id": chat_id, "message_id": message_id,
                    })
                except httpx.HTTPStatusError as exc:
                    # No longer pinned/deleted: safe to attempt removal.
                    if exc.response.status_code not in (400, 403):
                        succeeded = False
                except (httpx.HTTPError, RuntimeError):
                    succeeded = False
                try:
                    await self.request("deleteMessage", {
                        "chat_id": chat_id, "message_id": message_id,
                    })
                except httpx.HTTPStatusError as exc:
                    # Telegram refuses deletion of messages older than 48h.
                    if exc.response.status_code not in (400, 403):
                        succeeded = False
                except (httpx.HTTPError, RuntimeError):
                    succeeded = False
            if succeeded:
                await self.redis.hdel(APP_PIN_KEY, str(chat_id))
                removed += 1
            else:
                failures += 1
        await self.redis.delete(APP_PIN_REFRESH_KEY)
        self._app_launcher_cleaned = True
        log.info("Legacy pinned Mini App cleanup: chats=%s cleared=%s retry_needed=%s",
                 len(targets), removed, failures)

    async def _show_app_launcher(self, chat_id):
        """Telegram slash commands cannot open Web Apps directly.

        Return a temporary inline Web App button and remove it automatically.
        The user's /app command is also removed, so chat stays uncluttered.
        """
        result = await self.request("sendMessage", {
            "chat_id": chat_id,
            "text": "Открыть Lucky Jet:",
            "reply_markup": {"inline_keyboard": [[{
                "text": "Открыть приложение",
                "web_app": {"url": app_url()},
            }]]},
        })
        payload = result.get("result") if isinstance(result, dict) else None
        message_id = self._to_int(payload.get("message_id")) if isinstance(payload, dict) else None
        if message_id is not None:
            task = asyncio.create_task(self._schedule_delete(
                chat_id, message_id, APP_LAUNCH_TTL_SECONDS,
            ))
            self._deletion_tasks.add(task)
            task.add_done_callback(self._deletion_tasks.discard)

    async def _prepare_start(self, user_id, chat_id):
        """Explicit /start recovers panels even after a locally cleared chat."""
        history_lock = self._history_locks.setdefault(user_id, asyncio.Lock())
        menu_lock = self._menu_locks.setdefault(user_id, asyncio.Lock())
        async with history_lock, menu_lock:
            self.chats[user_id] = chat_id
            ids = {self.history_messages.pop(user_id, None),
                   self.menu_messages.pop(user_id, None),
                   self.prompt_messages.pop(user_id, None)}
            self.menu_deadlines.pop(user_id, None)
            self.history_hidden.discard(user_id)
            self.demo_pending_inputs.pop(user_id, None)
            self.pending_confirmations.pop(user_id, None)
            self.admin_pending_actions.pop(user_id, None)
            self._demo_history_views.pop(user_id, None)
            self.demo_history_pages.pop(user_id, None)
            self.demo_panel_messages.pop(user_id, None)
            self._demo_panel_content.pop(user_id, None)
            for message_id in ids:
                await self._safe_delete(chat_id, message_id)
            await self._persist_user(user_id)
        await self._register_commands_button(chat_id)

    async def _request_restart(self, user_id, chat_id):
        """Offer a confirmation for restarting only this user's Telegram UI."""
        if self.redis is None:
            await self.request("sendMessage", {
                "chat_id": chat_id,
                "text": "Перезапуск недоступен: нет подключения к Redis.",
            })
            return
        cooldown_key = f"{RESTART_COOLDOWN_KEY}:{user_id}"
        remaining = await self.redis.ttl(cooldown_key)
        if remaining > 0:
            await self.request("sendMessage", {
                "chat_id": chat_id,
                "text": f"Личный перезапуск временно недоступен. Повтори через {max(1, (remaining + 59) // 60)} мин.",
            })
            return
        nonce = secrets.token_urlsafe(12)
        self._restart_pending[user_id] = (nonce, time.monotonic() + RESTART_CONFIRM_SECONDS)
        await self.request("sendMessage", {
            "chat_id": chat_id,
            "text": (
                "Перезапустить только твой интерфейс Telegram-бота?\\n"
                "Активный DEMO-цикл будет остановлен и сохранён в истории.\\n"
                "Баланс, статистика, настройки и Premium-доступ сохранятся.\\n"
                "Mini App и другие пользователи не затрагиваются.\\n"
                "Подтверждение действует 60 секунд."
            ),
            "reply_markup": {"inline_keyboard": [
                [{"text": "Перезапустить", "callback_data": f"restart_confirm:{nonce}"}],
                [{"text": "Отмена", "callback_data": f"restart_cancel:{nonce}"}],
            ]},
        })

    async def _restart_user_session(self, user_id, chat_id):
        """Reset only this user's transient Telegram UI; preserve persistent data."""
        lock = self._mini_app_control_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            ledger = self.demo_ledgers.get(user_id)
            if ledger is not None:
                if ledger.active:
                    ledger.finish_active("stopped_by_user")
                ledger.enabled = False
                ledger.low_streak = 0
                await self._persist_demo(user_id)

        self.history_open.discard(user_id)
        history_was_hidden = user_id in self.history_hidden
        await self._clear_streak_notice(user_id)
        await self._prepare_start(user_id, chat_id)
        if history_was_hidden:
            self.history_hidden.add(user_id)
            await self._persist_user(user_id)

        if await self.is_authorized(user_id):
            if not history_was_hidden:
                await self.ensure_private_history(user_id)
            await self._send_transient(user_id, chat_id, self._main_text(), self.menu(user_id))
        else:
            await self.request("sendMessage", {
                "chat_id": chat_id,
                "text": "Личный интерфейс перезапущен. Для входа используй /start.",
            })

    async def _restart_callback(self, callback, user_id, chat_id, data):
        """Apply only the requesting user's confirmed restart."""
        async def reply(message, alert=False):
            try:
                await self.request("answerCallbackQuery", {
                    "callback_query_id": callback["id"], "text": message,
                    "show_alert": bool(alert),
                })
            except (httpx.HTTPError, RuntimeError, KeyError):
                pass

        if user_id is None or chat_id is None:
            return
        if user_id in self.blocked and not self.is_admin(user_id):
            await reply("Доступ заблокирован.", True)
            return
        action, _, nonce = data.partition(":")
        pending = self._restart_pending.get(user_id)
        if not pending or not hmac.compare_digest(nonce, pending[0]) or time.monotonic() > pending[1]:
            await reply("Подтверждение устарело. Используй /restart.", True)
            return
        self._restart_pending.pop(user_id, None)
        if action == "restart_cancel":
            await reply("Отменено.")
            return
        if self.redis is None:
            await reply("Перезапуск сейчас недоступен.", True)
            return

        cooldown_key = f"{RESTART_COOLDOWN_KEY}:{user_id}"
        accepted = await self.redis.set(
            cooldown_key, "1", ex=RESTART_COOLDOWN_SECONDS, nx=True,
        )
        if not accepted:
            remaining = await self.redis.ttl(cooldown_key)
            await reply(f"Повтори через {max(1, (remaining + 59) // 60)} мин.", True)
            return
        try:
            await self._restart_user_session(user_id, chat_id)
        except Exception:
            await self.redis.delete(cooldown_key)
            log.exception("Personal Telegram UI restart failed: user_id=%s", user_id)
            await reply("Не удалось перезапустить интерфейс.", True)
            return
        await self._safe_delete(
            chat_id, self._to_int(((callback.get("message") or {}).get("message_id"))
        )
        await reply("Личный интерфейс перезапущен.")
        log.info("Personal Telegram UI restarted: user_id=%s", user_id)

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

        # Remove Telegram's generated pin service notification when it refers
        # specifically to the old Mini App launcher, not the user's own pins.
        old_pin = msg.get("pinned_message")
        if isinstance(old_pin, dict) and old_pin.get("text") == "Lucky Jet · приложение":
            await self._safe_delete(chat_id, message_id)
            return

        value = (msg.get("text") or "").strip()
        command = value.split("@", 1)[0].split(" ", 1)[0].lower()

        if user_id in self.blocked and not self.is_admin(user_id):
            await self._safe_delete(chat_id, message_id)
            await self._replace_prompt(user_id, chat_id, "⛔ Доступ заблокирован.")
            return

        if command == "/app":
            await self._register_commands_button(chat_id)
            await self._safe_delete(chat_id, message_id)
            await self._show_app_launcher(chat_id)
            return

        if command == "/restart":
            await self._register_commands_button(chat_id)
            await self._safe_delete(chat_id, message_id)
            await self._request_restart(user_id, chat_id)
            return

        if await self.is_authorized(user_id):
            if self.site_account is not None and await self.site_account.handle_message(msg):
                return
            # Never make Telegram API calls before processing sensitive
            # website-login inputs. Restore legacy menu after that check.
            await self._register_commands_button(chat_id)
            if command in {"/start", "/menu", "/settings", "/status", "/help"}:
                self.history_open.discard(user_id)
                self.demo_pending_inputs.pop(user_id, None)
                self.pending_confirmations.pop(user_id, None)
                self.admin_pending_actions.pop(user_id, None)
            self.chats[user_id] = chat_id
            if command in {"/start", "/menu"}:
                await self._register_commands_button(chat_id)
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
                    self._main_text(),
                    self.menu(user_id),
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
        if data.startswith(("restart_confirm:", "restart_cancel:")):
            await self._restart_callback(callback, user_id, chat_id, data)
            return
        if self.site_account is not None:
            user = self._to_int((callback.get("from") or {}).get("id"))
            if user in self.authorized and await self.site_account.handle_callback(callback):
                return
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
        if data not in {"noop", "history_refresh", "demo_warning_close"}:
            await self._leave_site_account(user_id)
        if data == "demo_warning_close":
            await self._safe_delete(chat_id, message_id)
            return

        if data not in {"noop", "history_refresh", "demo_warning_close"}:
            if data.startswith(('coef_days:', 'coef_day:', 'hi_days:', 'hi_day:', 'hi_case:', 'demo_daily_days:', 'demo_daily:')) or data in {'demo_daily', 'demo_history', 'demo_stats'} or data.startswith(('demo_list:', 'demo_cycle:')):
                self.history_open.add(user_id)
            else:
                self.history_open.discard(user_id)
        history_page = None
        if data == "demo_history":
            history_page = 0
        elif data.startswith("demo_list:"):
            history_page = self._to_int(data.rsplit(":", 1)[-1])
        elif data.startswith("demo_cycle:"):
            history_index = self._to_int(data.rsplit(":", 1)[-1])
            history_page = history_index // 10 if history_index is not None else 0
        elif data.startswith("demo_history:"):
            history_index = self._to_int(data.split(":", 1)[1])
            history_page = history_index // 10 if history_index is not None else 0

        if history_page is not None:
            self._set_demo_history_page(user_id, history_page)
        else:
            self.demo_history_pages.pop(user_id, None)
            if data != "menu_close":
                self._touch_menu(user_id)
        self.demo_pending_inputs.pop(user_id, None)
        if data not in {"settings_reset_confirm", "demo_reset_confirm"}:
            self.pending_confirmations.pop(user_id, None)
        if data != "admin_confirm" and not data.startswith(("admin_revoke:", "admin_block:", "admin_unblock:")):
            self.admin_pending_actions.pop(user_id, None)

        if history_refresh:
            await self._persist_user(user_id)
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
            await self._register_commands_button(chat_id)
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
            await self._close_menu(user_id)
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
            answer, markup = self._main_text(), self.menu(user_id)
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
            if ledger.active and ledger.enabled:
                answer = "Активная DEMO-сессия ещё не завершена. Выключить DEMO можно после её завершения или сброса."
                markup = self.demo_menu(user_id)
            else:
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
            if value not in TRIGGER_OPTIONS:
                return
            if ledger.active:
                answer = "Текущая DEMO-сессия ещё не завершена. Условие запуска можно изменить после её завершения."
                markup = self.demo_menu(user_id)
            else:
                if not ledger.set_trigger(value):
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
                markup = {"inline_keyboard": self._navigation_rows("demo_menu")}
        elif data == "demo_input_cancel":
            self.demo_pending_inputs.pop(user_id, None)
            answer = self._demo_text(user_id)
            markup = self.demo_menu(user_id)
        elif data in {"coef_date", "hi_date"}:
            self.demo_pending_inputs[user_id] = data
            answer = "📅 Дата: ДД.ММ.ГГГГ"
            markup = {"inline_keyboard": footer('hi_days:0' if data == 'hi_date' else 'coef_days:0')}
        elif data.startswith("hi_"):
            stats=getattr(self,'coefficient_stats',None)
            if stats is None or stats.redis is None:
                answer, markup = 'Хранилище недоступно.', {'inline_keyboard':footer('coef_days:0')}
            else:
                answer, markup = await high_screen(stats,data)
        elif data.startswith("demo_daily_days:"):
            answer, markup = self._daily_days_screen(user_id,int(data.split(':')[1]))
        elif data.startswith("coef_"):
            answer, markup = await self._coefficient_screen(data)
        elif data == "demo_daily_date":
            self.demo_pending_inputs[user_id] = "daily_date"
            answer = "📅 Введите дату в формате ДД.ММ.ГГГГ, например 07.11.2026."
            markup = {"inline_keyboard": self._navigation_rows("demo_daily")}
        elif data == "demo_daily" or data.startswith("demo_daily:"):
            day = data.split(":", 1)[1] if ":" in data else None
            answer, markup = self._demo_daily_screen(user_id, day)
        elif data == "demo_history":
            answer, markup = self._demo_history_list(user_id)
        elif data == "demo_stats":
            answer = self._demo_stats_text(user_id)
            markup = {"inline_keyboard": self._navigation_rows("demo_menu")}
        elif data.startswith("demo_list:"):
            _, token, page = data.split(":")
            answer, markup = self._demo_history_list(user_id, int(page), token)
        elif data.startswith("demo_cycle:"):
            _, token, index = data.split(":")
            answer, markup = self._demo_cycle_screen(user_id, token, int(index))
        elif data.startswith("demo_history:"):
            # Compatibility with buttons rendered before the paginated list.
            page = self._to_int(data.split(":", 1)[1])
            answer, markup = self._demo_history_text(user_id, page), self._demo_history_menu(user_id, page)
        elif data == "demo_reset":
            self.pending_confirmations[user_id] = "demo"
            state = "включённым" if self._demo_ledger(user_id).enabled else "выключенным"
            answer = ("♻️ Сбросить DEMO?\n"
                      "Баланс станет $10,000.00, стартовая ставка — $10.00. "
                      "Активная сессия прекратится. История циклов, максимальные шаги по дням и вся статистика DEMO обнулятся. "
                      f"Условие запуска сохраняется. Демо останется {state}.")
            markup = {"inline_keyboard": [
                [{"text": "✅ Да, сбросить", "callback_data": "demo_reset_confirm"}],
                [{"text": "Отмена", "callback_data": "demo_menu"}],
            ]}
            markup["inline_keyboard"] += self._navigation_rows()
        elif data == "demo_reset_confirm":
            if self.pending_confirmations.pop(user_id, None) != "demo":
                await self._send_transient(user_id, chat_id,
                    "Подтверждение устарело. Для сброса выберите «Сбросить демо-баланс и циклы» заново.",
                    self.demo_menu(user_id))
                return
            ledger = self._demo_ledger(user_id)
            ledger.reset()
            self._demo_history_views.pop(user_id, None)
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
            if value not in range(2, 11):
                return
            self.thresholds[user_id] = value
            await self._persist_user(user_id)
            answer = self._settings_text(user_id)
            markup = self.settings_menu(user_id)
        elif data.startswith("history_size:"):
            value = self._to_int(data.split(":", 1)[1])
            if value not in {20, 30, 50, 100}:
                return
            self.history_sizes[user_id] = value
            await self._persist_user(user_id)
            await self.ensure_private_history(user_id)
            answer = self._settings_text(user_id)
            markup = self.settings_menu(user_id)
        elif data.startswith("delete:"):
            raw = data.split(":", 1)[1]
            value = 0 if raw == "off" else self._to_int(raw)
            if value in {5, 15}:
                value = 10
            if value not in {0, 10, 30, 60}:
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
                      "Уведомления и звук: ВКЛ · серия: 4 · история: 20 · удаление: 10 сек.\nДемо сохранится.")
            markup = {"inline_keyboard": [
                [{"text": "✅ Да, вернуть настройки", "callback_data": "settings_reset_confirm"}],
                [{"text": "⬅️ Отмена", "callback_data": "menu_settings"}],
            ]}
            markup["inline_keyboard"] += self._navigation_rows()
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
            answer = f"🛡 Управление доступом\nС доступом: {len(self.authorized)} · Заблокировано: {len(self.blocked)}"
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
                [{"text": "✅ Подтвердить действие", "callback_data": "admin_confirm", "style": "success" if data.startswith("admin_unblock:") else "danger"}],
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
        self.menu_deadlines.pop(user_id, None)
        timer = self._menu_tasks.pop(user_id, None)
        if timer is not None:
            timer.cancel()
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
        self._demo_history_views.pop(user_id, None)
        self.demo_history_pages.pop(user_id, None)
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
            await self.redis.hdel("luckyjet:demo_completed_cycles", str(user_id))
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
        if user_id not in self.menu_messages:
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

    async def _send_demo_warning(self, user_id, text):
        chat_id = self.chats.get(user_id)
        if chat_id is None:
            return
        try:
            result = await self.request("sendMessage", {
                "chat_id": chat_id,
                "text": text,
                "reply_markup": {"inline_keyboard": [[
                    {"text": "✖ Закрыть", "callback_data": "demo_warning_close"}
                ]]},
            })
            message_id = result.get("result", {}).get("message_id")
            if message_id is not None:
                task = asyncio.create_task(self._schedule_delete(chat_id, int(message_id), 600))
                self._deletion_tasks.add(task)
                task.add_done_callback(self._deletion_tasks.discard)
        except (httpx.HTTPError, RuntimeError):
            log.warning("Demo warning delivery failed: user_id=%s", user_id)

    async def _refresh_open_demo_history(self, user_id):
        if self.demo_history_pages.get(user_id) != 0 or user_id not in self.menu_messages:
            return
        self._demo_history_views.pop(user_id, None)
        text, markup = self._demo_history_list(user_id)
        chat_id = self.chats.get(user_id)
        if chat_id is not None:
            await self._send_transient(user_id, chat_id, text, markup)

    async def _handle_demo_round(self, user_id, coefficient):
        lock = self._mini_app_control_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            ledger = self.demo_ledgers.get(user_id)
            if ledger is None or not ledger.enabled:
                return None
            event = ledger.process_round(coefficient)
            await self._persist_demo(user_id)
        if not event:
            return None
        if event.get("type") == "insufficient_balance" and event.get("entry"):
            entry = event["entry"]
            net = int(entry.get("net_cents", 0) or 0)
            await self._send_demo_warning(
                user_id,
                "⚠️ DEMO-сессия завершена\n"
                "Причина: недостаточно баланса\n"
                f"Шагов: {len(entry.get('rounds') or [])}\n"
                f"Результат: {'+' if net > 0 else ''}{format_dollars(net)}\n"
                f"Остаток: {format_dollars(ledger.balance_cents)}\n"
                f"Новая ставка: {format_dollars(ledger.base_stake_cents)}",
            )
        if event.get("type") == "recovered" or event.get("entry") is not None:
            await self._refresh_open_demo_history(user_id)
        return event

    async def on_history_gap(self):
        for user_id in list(self.authorized):
            ledger = self.demo_ledgers.get(user_id)
            if ledger is None or not ledger.enabled:
                continue
            ledger.low_streak = 0
            entry = ledger.finish_active("incomplete_data") if ledger.active else None
            await self._persist_demo(user_id)
            if entry is not None:
                await self._send_demo_warning(
                    user_id,
                    "⚠️ DEMO-сессия остановлена\n"
                    "Причина: не удалось восстановить пропущенные результаты\n"
                    f"Подтверждено шагов: {len(entry.get('rounds') or [])}\n"
                    "Баланс сохранён на последнем подтверждённом значении",
                )
                await self._refresh_open_demo_history(user_id)

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
        if user_id not in self.active_messages and user_id not in self.pending_deletions:
            return
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
                return False
            return
        try:
            await self.request("editMessageReplyMarkup", {
                "chat_id": chat_id, "message_id": message_id,
                "reply_markup": self._streak_markup(ending=ending),
            })
        except (httpx.HTTPError, RuntimeError) as exc:
            unchanged = False
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 400:
                try:
                    unchanged = "message is not modified" in str(exc.response.json().get("description", "")).lower()
                except (ValueError, TypeError):
                    pass
            if not unchanged:
                log.info("Streak notification edit not applied: user_id=%s", user_id)
                return False
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
        number = float(value[:-1])

        if number < 2:
            self.current_streak.append(value)
            notices = getattr(self, "notification_queue", None)
            if notices is not None:
                await notices.submit(self.authorized - self.notifications_disabled, self.current_streak)
            else:
                for user_id in list(self.authorized):
                    await self._update_streak_notice(user_id)
        else:
            notices = getattr(self, "notification_queue", None)
            if notices is not None:
                await notices.submit(self.authorized - self.notifications_disabled, self.current_streak, ending=value)
            else:
                for user_id in list(self.active_messages):
                    if user_id in self.authorized:
                        await self._update_streak_notice(user_id, ending=value)
            self.current_streak = []

        panels = getattr(self, "live_panels", None)
        if panels is None:
            await self.update_private_histories()
        for user_id in list(self.authorized):
            await self._handle_demo_round(user_id, value)
            if panels is None:
                await self._refresh_demo_panel(user_id)
        if panels is not None:
            panels.request()

    async def _schedule_delete(self, chat_id, message_id, delay):
        try:
            await asyncio.sleep(delay)
            await self.request("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, RuntimeError):
            log.warning("Private streak deletion failed: chat_id=%s message_id=%s", chat_id, message_id)

