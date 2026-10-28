from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger("luckyjet.direct_only")


def should_release_browser_page(rounds, source) -> bool:
    """Return True once completed-round history is coming from the direct API."""
    return bool(rounds) and str(source or "").startswith("direct:")


async def refresh_live_history(monitor, config, state, capture, page):
    """Refresh from the direct API first; only use DOM while a live page exists."""
    before = (state.last_round_id, list(state.previous))
    received = await monitor.poll_direct_gateways(capture)
    network_rounds, _ = await capture.snapshot()

    if received and network_rounds:
        worked = await monitor.process_network_history(config, state, network_rounds)
    elif page is not None and not page.is_closed():
        dom_history = await monitor.read_dom_history(page)
        worked = await monitor.process_dom_history(config, state, dom_history)
    else:
        worked = False

    return {
        "ok": bool(received and worked),
        "worked": bool(worked),
        "changed": before != (state.last_round_id, list(state.previous)),
    }


async def run_monitor(monitor, config, *, state=None):
    """Run the monitor with Chromium only as a bootstrap/fallback source."""
    if state is None:
        state = await monitor.load_state(config)

    last_error_notice = 0.0
    startup_sent = False

    log.info("Monitor starting; browser-on-demand mode")

    async with monitor.async_playwright() as p:
        browser = None
        context = None
        page = None
        capture = None
        direct_only = False
        missing_since = None
        last_diagnostic = 0.0

        async def close_browser_stack():
            nonlocal browser, context, page

            if page is not None:
                try:
                    if not page.is_closed():
                        await page.close()
                except Exception:
                    pass
                page = None

            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass
                context = None

            if browser is not None:
                try:
                    await browser.close()
                except Exception:
                    pass
                browser = None

        async def open_browser_stack():
            nonlocal browser, context, page, capture

            browser = await p.chromium.launch(headless=config.headless)
            context = await browser.new_context(
                viewport={"width": 430, "height": 932},
                locale="ru-RU",
            )
            capture = monitor.LiveCapture()
            page = await monitor.open_game(context, config, capture)

        try:
            while True:
                try:
                    if not direct_only and (page is None or page.is_closed()):
                        if monitor.history_refresh is not None:
                            monitor.history_refresh.bind(None)

                        await close_browser_stack()
                        await open_browser_stack()
                        missing_since = None
                        last_diagnostic = 0.0

                        if not startup_sent:
                            await monitor.send(
                                config,
                                (
                                    "Монитор запущен. "
                                    "Ставки не выполняются. "
                                    "Отслеживаются только завершённые раунды."
                                ),
                            )
                            startup_sent = True

                    now = time.monotonic()

                    if page is not None and not page.is_closed():
                        issue = await monitor.page_issue(page)
                    else:
                        issue = None

                    if issue:
                        if now - last_error_notice >= config.error_notify_cooldown:
                            await monitor.send(config, issue)
                            last_error_notice = now
                        await asyncio.sleep(config.poll_seconds)
                        continue

                    async def fetch_current():
                        return await refresh_live_history(
                            monitor,
                            config,
                            state,
                            capture,
                            page,
                        )

                    if monitor.history_refresh is not None:
                        monitor.history_refresh.bind(fetch_current)
                        outcome = await monitor.history_refresh.request()
                    else:
                        outcome = await fetch_current()

                    worked = outcome["worked"]
                    rounds, source = await capture.snapshot()

                    if worked:
                        missing_since = None

                        if (
                            not direct_only
                            and page is not None
                            and should_release_browser_page(rounds, source)
                        ):
                            log.info(
                                "Direct history is stable; closing Chromium to save memory"
                            )
                            await close_browser_stack()
                            direct_only = True

                    else:
                        if missing_since is None:
                            missing_since = now
                        elif now - missing_since >= 90:
                            log.warning(
                                "Direct history unavailable for 90s; reopening browser"
                            )
                            if monitor.history_refresh is not None:
                                monitor.history_refresh.bind(None)
                            await close_browser_stack()
                            direct_only = False
                            capture = None
                            missing_since = None
                            await asyncio.sleep(3)
                            continue

                    if now - last_diagnostic >= 45:
                        frames = 0
                        dom = []

                        if page is not None and not page.is_closed():
                            frames = len(page.frames)
                            if not rounds:
                                dom = await monitor.read_dom_history(page)

                        log.info(
                            "History diagnostics: network=%d source=%s dom=%d frames=%d browser=%s",
                            len(rounds),
                            source or "none",
                            len(dom),
                            frames,
                            "off" if direct_only else "on",
                        )

                        if page is not None and not page.is_closed():
                            await monitor.diagnose_page(page, capture)

                        last_diagnostic = now

                    await asyncio.sleep(config.poll_seconds)

                except asyncio.CancelledError:
                    raise

                except Exception as exc:
                    log.error("Monitor iteration failed: %s", type(exc).__name__)
                    log.debug("Monitor error details", exc_info=True)

                    now = time.monotonic()
                    if now - last_error_notice >= config.error_notify_cooldown:
                        await monitor.send(
                            config,
                            (
                                "Ошибка мониторинга: "
                                f"{type(exc).__name__}. "
                                "Повторная попытка."
                            ),
                        )
                        last_error_notice = now

                    if monitor.history_refresh is not None:
                        monitor.history_refresh.bind(None)

                    await close_browser_stack()
                    direct_only = False
                    capture = None

                    await asyncio.sleep(max(3.0, config.poll_seconds))

        finally:
            if monitor.history_refresh is not None:
                monitor.history_refresh.bind(None)
            await close_browser_stack()
