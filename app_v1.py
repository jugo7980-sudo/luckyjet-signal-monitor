from __future__ import annotations

import asyncio
import logging

import httpx

import main as base
from luckyjet_reliable_gate import ReliableTelegramPasswordGate
from luckyjet_source_guard import install_source_guard
from luckyjet_signal import monitor
from luckyjet_signal.browser_runtime import low_memory_async_playwright
from luckyjet_signal.demo_mode import wrap_click_demo_mode
from luckyjet_signal.settings import Config


log = logging.getLogger("luckyjet.v1")


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
    password_gate = ReliableTelegramPasswordGate(
        panel._request,
        history_provider=lambda: panel.history,
        admin_chat_id=config.telegram_chat_id,
    )
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
            password_gate.start(),
            monitor.run_monitor(config, state=state),
        )
    finally:
        await password_gate.close()
        await panel.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
