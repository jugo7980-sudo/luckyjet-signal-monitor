from __future__ import annotations

import asyncio
import logging

import main as base
from luckyjet_password_gate import TelegramPasswordGate
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


async def main_async() -> None:
    config = Config.from_env()
    install_source_guard(monitor)
    monitor.async_playwright = low_memory_async_playwright
    monitor.click_demo_mode = wrap_click_demo_mode(monitor.click_demo_mode)

    state = await monitor.load_state(config)
    panel = base.TelegramHistoryPanel(config, initial_history=state.previous)
    password_gate = TelegramPasswordGate(
        panel._request,
        history_provider=lambda: panel.history,
        admin_chat_id=config.telegram_chat_id,
    )
    seed_private_streak(password_gate, state.previous)

    async def handle_round(config_or_message, message=None):
        actual = str(config_or_message) if message is None else message
        coefficient = base.coefficient_from_message(actual)

        if coefficient is not None and len(panel.history) < 2 and len(state.previous) >= 2:
            latest = base.format_history_coefficient(coefficient)
            saved = base.format_history_coefficient(state.previous[0])
            if latest == saved:
                panel.history = [
                    formatted
                    for value in state.previous[: panel.limit]
                    if (formatted := base.format_history_coefficient(value)) is not None
                ]
                seed_private_streak(password_gate, state.previous)
                return await panel.ensure_panel()

        result = await panel.handle_monitor_message(config_or_message, message)
        if coefficient is not None:
            await password_gate.on_round(coefficient)
        return result

    monitor.send = handle_round

    try:
        await asyncio.gather(
            base.health_server(),
            password_gate.start(),
            panel.ensure_panel(),
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
