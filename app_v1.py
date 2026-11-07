from __future__ import annotations

import asyncio
import logging
import os

import httpx

from history_refresh import HistoryRefresh
from telegram_delivery import TelegramDelivery
from live_panel_updates import LivePanelUpdates
from notification_queue import NotificationQueue
from coefficient_stats import CoefficientStats
from full_data_reset import apply_complete_data_reset
from demo_probe import DemoProbe
from site_account import SiteAccount, SessionStore
from site_login_browser import SiteLoginBrowser

import main as base
from luckyjet_fast_poll import install_fast_poll
from luckyjet_reliable_gate import ReliableTelegramPasswordGate
from luckyjet_source_guard import install_source_guard
from luckyjet_signal import direct_only
from luckyjet_signal import monitor
from luckyjet_signal.browser_runtime import low_memory_async_playwright
from luckyjet_signal.demo_mode import wrap_click_demo_mode
from luckyjet_signal.settings import Config
from luckyjet_signal.runtime_connections import RuntimeConnections
from telegram_polling_lease import TelegramPollingLease
from xray_runtime import XrayManager


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
        if (method.startswith("editMessage") and exc.response.status_code == 400
                and "message is not modified" in description.lower()):
            log.debug("Telegram %s already up to date", method)
            raise
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


def configure_monitor_runtime() -> None:
    """Install production runtime patches in one testable place."""
    install_source_guard(monitor)
    install_fast_poll(monitor)
    monitor.async_playwright = low_memory_async_playwright
    monitor.click_demo_mode = wrap_click_demo_mode(monitor.click_demo_mode)


async def render_self_keepalive(interval_seconds=240) -> None:
    """Keep a free Render web service active with light inbound self-traffic."""
    url = os.getenv("RENDER_EXTERNAL_URL", "").strip()
    if not url:
        return
    url = url.rstrip("/") + "/"

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        while True:
            await asyncio.sleep(float(interval_seconds))
            try:
                response = await client.get(url)
                response.raise_for_status()
                log.info("Render self keepalive succeeded")
            except asyncio.CancelledError:
                raise
            except httpx.HTTPError as exc:
                log.warning("Render self keepalive failed: %s", type(exc).__name__)


async def main_async() -> None:
    async with RuntimeConnections():
        async with XrayManager() as xray:
            await xray.start_from_env()
            # Only game-source requests use Xray. Telegram, Redis, and Mini
            # App keep their direct transport even when the proxy is active.
            monitor.game_proxy = xray.proxy_url
            try:
                await _main_async()
            finally:
                monitor.game_proxy = None


async def _main_async() -> None:
    config = Config.from_env()
    configure_monitor_runtime()

    state = await monitor.load_state(config)
    panel = base.TelegramHistoryPanel(config, initial_history=state.previous)

    delivery = TelegramDelivery(panel._request)

    async def telegram_request(method, payload):
        return await telegram_request_with_diagnostics(delivery, method, payload)

    await prepare_telegram_polling(telegram_request, config.telegram_chat_id)

    refresh = HistoryRefresh()
    monitor.history_refresh = refresh

    password_gate = ReliableTelegramPasswordGate(
        telegram_request,
        history_provider=lambda: panel.history,
        history_refresher=refresh.request,
        admin_chat_id=config.telegram_chat_id,
    )
    async def render_panels():
        await password_gate.update_private_histories()
        for user_id in list(password_gate.authorized):
            await password_gate._refresh_demo_panel(user_id)

    live_panels = LivePanelUpdates(render_panels)
    password_gate.live_panels = live_panels
    password_gate.notification_queue = NotificationQueue(password_gate)
    password_gate.coefficient_stats = CoefficientStats(password_gate.redis)
    monitor.browser_lock = asyncio.Lock()
    login_url = os.getenv("SITE_LOGIN_URL", "").strip() or config.game_url
    site_account = SiteAccount(
        password_gate.admin_chat_id,
        telegram_request,
        SessionStore(os.getenv("SITE_SESSION_PATH", "data/site-auth/session.json"), login_url),
        lambda: SiteLoginBrowser(
            login_url, headless=config.headless,
            proxy_url=monitor.game_proxy, expected_country='JP',
        ),
        monitor.browser_lock,
    )
    password_gate.site_account = site_account
    monitor.site_account = site_account
    demo_probe = DemoProbe()
    monitor.demo_probe = demo_probe
    password_gate.demo_probe = demo_probe

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
    original_process_history = monitor.process_network_history

    async def process_history(config, state, rounds):
        await password_gate.coefficient_stats.ingest(rounds)
        result = await original_process_history(config, state, rounds)
        if getattr(state, "history_gap", False):
            await password_gate.on_history_gap()
            state.history_gap = False
            # Persist the advanced source cursor only after all affected DEMO
            # ledgers have safely recorded the gap. A crash before this save
            # leaves the old cursor in Redis, so the gap is detected again.
            await monitor.save_state(config, state)
        return result

    monitor.process_network_history = process_history

    async def run_owned():
        # This is the only worker that owns Telegram polling. The previous
        # deployment is no longer writing to Redis after lease acquisition.
        # Reset BEFORE reloading monitor history and starting either worker.
        redis = await monitor._redis(config)
        if redis is None:
            raise RuntimeError("Full reset requires Redis persistence")
        reset_applied = await apply_complete_data_reset(redis)
        if reset_applied:
            password_gate.coefficient_stats.reset_baseline_pending = True
            log.warning("Complete data reset applied: next source snapshot is baseline only")
        password_gate.coefficient_stats.redis = redis
        # Reload AFTER reset; never retain old coefficients, DEMO or user data.
        restored = await monitor.load_state(config)
        state.__dict__.update(restored.__dict__)
        panel.history = [base.format_history_coefficient(value) for value in state.previous][:panel.limit]
        seed_private_streak(password_gate, state.previous)
        async with asyncio.TaskGroup() as owned:
            owned.create_task(password_gate.start())
            owned.create_task(direct_only.run_monitor(monitor, config, state=state))
    await retire_legacy_history_panel(panel)

    try:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(base.health_server(password_gate, config.telegram_bot_token))
            tasks.create_task(render_self_keepalive())
            tasks.create_task(polling_lease.run(run_owned()))
    finally:
        monitor.process_network_history = original_process_history
        await site_account.close()
        monitor.site_account = None
        await live_panels.close()
        await demo_probe.close()
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

