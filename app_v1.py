from __future__ import annotations

import asyncio
import logging
import os

import httpx

from history_refresh import HistoryRefresh

import main as base
from luckyjet_reliable_gate import ReliableTelegramPasswordGate
from luckyjet_source_guard import install_source_guard
from luckyjet_signal import direct_only
from luckyjet_signal import monitor
from luckyjet_signal.browser_runtime import low_memory_async_playwright
from luckyjet_signal.demo_mode import wrap_click_demo_mode
from luckyjet_signal.settings import Config
from telegram_polling_lease import TelegramPollingLease


log = logging.getLogger("luckyjet.v1")


async def telegram_request_with_diagnostics(request, method, payload):
    """Call Telegram while preserving the API error description in Render logs."""
    try:
        return await request(method, payload)
    except httpx.HTTPStatusError as exc:
        description = ""
        try:
            description = str(exc.response.json().get("description", ""))
        except Exception:
            description = ""
        log.error(
            "Telegram API %s failed: HTTP %s %s",
            method,
            exc.response.status_code,
            description[:300],
        )
        raise
    except RuntimeError as exc:
        log.error("Telegram API %s failed: %s", method, str(exc)[:300])
        raise


async def prepare_telegram_polling(request, admin_chat_id=None):
    """Validate the bot token and remove any webhook that blocks getUpdates."""
    me = await request("getMe", {})
    bot = me.get("result", {})
    log.info(
        "Telegram bot connected: id=%s username=@%s",
        bot.get("id", "?"),
        bot.get("username", "?"),
    )

    webhook = await request("getWebhookInfo", {})
    info = webhook.get("result", {})
    webhook_url = str(info.get("url") or "").strip()
    pending = int(info.get("pending_update_count") or 0)
    log.info(
        "Telegram webhook state: configured=%s pending_updates=%s",
        bool(webhook_url),
        pending,
    )
    if webhook_url:
        await request("deleteWebhook", {"drop_pending_updates": False})
        log.warning("Telegram webhook removed so long polling can start")

    if admin_chat_id:
        try:
            chat = await request("getChat", {"chat_id": admin_chat_id})
            result = chat.get("result", {})
            log.info(
                "Telegram admin chat reachable: id=%s type=%s",
                result.get("id", admin_chat_id),
                result.get("type", "unknown"),
            )
        except (httpx.HTTPError, RuntimeError):
            log.exception("Telegram admin chat is not reachable")


def seed_private_streak(gate, values) -> None:
    """Restore the current chronological <2x run from newest-first history."""
    gate.current_streak = []
    for value in reversed(list(values or [])):
        formatted = base.format_history_coefficient(value)
        if formatted is None:
            continue
        if float(formatted[:-1]) < 2:
            gate.current_streak.append(formatted)
        else:
            gate.current_streak.clear()


def record_history_coefficient(panel, coefficient):
    """Update the in-memory history; Telegram rendering belongs to private user panels."""
    formatted = base.format_history_coefficient(coefficient)
    if formatted is None:
        return None
    panel.history = base.prepend_history(
        panel.history,
        formatted,
        limit=panel.limit,
    )
    return formatted


async def retire_legacy_history_panel(panel) -> None:
    """Remove the pre-auth shared history so only authenticated histories remain."""
    message_id = panel.message_id
    chat_id = panel.config.telegram_chat_id
    if message_id is None or not chat_id:
        return
    try:
        await panel._request(
            "deleteMessage",
            {"chat_id": chat_id, "message_id": message_id},
        )
    except (httpx.HTTPError, RuntimeError):
        log.info("Legacy shared history panel already absent or not removable")
    finally:
        panel.message_id = None


async def main_async() -> None:
    config = Config.from_env()
    install_source_guard(monitor)
    monitor.async_playwright = low_memory_async_playwright
    monitor.click_demo_mode = wrap_click_demo_mode(monitor.click_demo_mode)

    state = await monitor.load_state(config)
    panel = base.TelegramHistoryPanel(config, initial_history=state.previous)

    async def telegram_request(method, payload):
        return await telegram_request_with_diagnostics(panel._request, method, payload)

    await prepare_telegram_polling(telegram_request, config.telegram_chat_id)

    refresh = HistoryRefresh()
    monitor.history_refresh = refresh

    password_gate = ReliableTelegramPasswordGate(
        telegram_request,
        history_provider=lambda: panel.history,
        history_refresher=refresh.request,
        admin_chat_id=config.telegram_chat_id,
    )
    polling_lease = TelegramPollingLease(os.getenv("REDIS_URL", ""))
    seed_private_streak(password_gate, state.previous)

    async def handle_round(config_or_message, message=None):
        actual = str(config_or_message) if message is None else message
        coefficient = base.coefficient_from_message(actual)
        if coefficient is None:
            return True

        formatted = record_history_coefficient(panel, coefficient)
        if formatted is None:
            return False

        await password_gate.on_round(formatted)
        return True

    monitor.send = handle_round
    await retire_legacy_history_panel(panel)

    try:
        await asyncio.gather(
            base.health_server(),
            polling_lease.run(password_gate.start()),
            direct_only.run_monitor(monitor, config, state=state),
        )
    finally:
        await password_gate.close()
        await refresh.close()
        monitor.history_refresh = None
        await polling_lease.release()
        await polling_lease.close()
        await panel.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
