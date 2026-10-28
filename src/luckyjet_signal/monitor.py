from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

from .settings import Config

log = logging.getLogger("luckyjet.monitor")
STATE = Path("data/state.json")

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

DIAGNOSTIC_INTERVAL = 60

HISTORY_SCRIPT = r"""
(() => {
    const visible = el => {
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 0 && r.height > 0 &&
            s.display !== "none" &&
            s.visibility !== "hidden" &&
            Number(s.opacity) !== 0;
    };

    const candidates = [];

    for (const el of document.querySelectorAll("body *")) {
        if (el.children.length || !visible(el)) continue;

        const text = (
            el.innerText || el.textContent || ""
        ).trim();

        const match = text.match(
            /^(\d+(?:[.,]\d{1,2})?)\s*x$/i
        );

        if (!match) continue;

        const value = Number(
            match[1].replace(",", ".")
        );

        if (
            !Number.isFinite(value) ||
            value < 1 ||
            value > 100000
        ) continue;

        const r = el.getBoundingClientRect();

        candidates.push({
            value,
            x: r.left,
            y: r.top
        });
    }

    const groups = [];

    for (const item of candidates) {
        let group = groups.find(
            g => Math.abs(g.y - item.y) < 14
        );

        if (!group) {
            group = {y: item.y, items: []};
            groups.push(group);
        }

        group.items.push(item);
    }

    groups.sort(
        (a, b) =>
            b.items.length - a.items.length ||
            a.y - b.y
    );

    const best = groups.find(
        g => g.items.length >= 3
    );

    if (!best) return [];

    best.items.sort((a, b) => a.x - b.x);

    return best.items
        .slice(0, 30)
        .map(item => item.value);
})()
"""

DIAGNOSTIC_SCRIPT = r"""
(() => {
    const body = document.body;
    const text = body
        ? (body.innerText || "").slice(0, 350)
        : "";

    const elements = document.querySelectorAll("*");

    let canvasCount = 0;
    let iframeCount = 0;
    let multiplierCount = 0;

    for (const el of elements) {
        if (el.tagName === "CANVAS") canvasCount++;
        if (el.tagName === "IFRAME") iframeCount++;

        if (el.children.length) continue;

        const value = (
            el.innerText || el.textContent || ""
        ).trim();

        if (/^\d+(?:[.,]\d+)?\s*x$/i.test(value)) {
            multiplierCount++;
        }
    }

    return {
        title: document.title.slice(0, 100),
        ready: document.readyState,
        bodyLength: body
            ? (body.innerText || "").length
            : 0,
        elements: elements.length,
        canvases: canvasCount,
        iframes: iframeCount,
        multipliers: multiplierCount,
        sample: text.replace(/\s+/g, " ")
    };
})()
"""


class State:
    def __init__(
        self,
        active=False,
        streak=0,
        next_amount=150,
        previous=None,
    ):
        self.active = bool(active)
        self.streak = int(streak)
        self.next_amount = int(next_amount)
        self.previous = list(previous or [])

    def dump(self):
        return {
            "active": self.active,
            "streak": self.streak,
            "next_amount": self.next_amount,
            "previous": self.previous,
        }


async def _redis(config):
    url = os.getenv("REDIS_URL", "").strip()

    if not url:
        return None

    import redis.asyncio as redis

    return redis.from_url(
        url,
        decode_responses=True,
    )


async def load_state(config):
    try:
        r = await _redis(config)

        if r:
            try:
                raw = await r.get("luckyjet:state")

                if raw:
                    d = json.loads(raw)

                    return State(
                        active=d.get("active", False),
                        streak=d.get("streak", 0),
                        next_amount=d.get(
                            "next_amount",
                            config.base_amount,
                        ),
                        previous=d.get("previous"),
                    )
            finally:
                await r.aclose()

        if STATE.exists():
            d = json.loads(
                STATE.read_text(encoding="utf-8")
            )

            return State(
                active=d.get("active", False),
                streak=d.get("streak", 0),
                next_amount=d.get(
                    "next_amount",
                    config.base_amount,
                ),
                previous=d.get("previous"),
            )

    except Exception:
        log.exception("State load failed")

    return State(next_amount=config.base_amount)


async def save_state(config, state):
    raw = json.dumps(state.dump())

    try:
        r = await _redis(config)

        if r:
            try:
                await r.set("luckyjet:state", raw)
                return
            finally:
                await r.aclose()

    except Exception:
        log.exception("Redis save failed")

    STATE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    STATE.write_text(
        raw,
        encoding="utf-8",
    )


async def send(config, message):
    token = config.telegram_bot_token
    chat_id = config.telegram_chat_id

    if not token or not chat_id:
        log.warning("Telegram credentials missing")
        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{token}/sendMessage"
    )

    try:
        async with httpx.AsyncClient(
            timeout=15
        ) as client:
            response = await client.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": message,
                },
            )

            response.raise_for_status()

        log.info("Telegram message delivered")
        return True

    except httpx.HTTPStatusError as exc:
        log.error(
            "Telegram HTTP error: %s",
            exc.response.status_code,
        )
        return False

    except httpx.RequestError as exc:
        log.error(
            "Telegram connection error: %s",
            type(exc).__name__,
        )
        return False


def new_results(previous, current):
    if not previous or not current:
        return []

    if previous == current:
        return []

    limit = min(
        6,
        max(1, len(current) - 1),
    )

    candidates = []

    for count in range(1, limit + 1):
        overlap = min(
            len(previous),
            len(current) - count,
        )

        if overlap <= 0:
            continue

        if (
            current[count:count + overlap]
            == previous[:overlap]
        ):
            candidates.append(
                (
                    overlap,
                    list(reversed(current[:count])),
                )
            )

    if not candidates:
        return []

    overlap, results = max(
        candidates,
        key=lambda item: item[0],
    )

    minimum = min(
        3,
        max(
            1,
            min(len(previous), len(current)) - 1,
        ),
    )

    if overlap < minimum:
        return []

    return results


async def read_history(page, config):
    best = []

    for frame in page.frames:
        try:
            values = await asyncio.wait_for(
                frame.evaluate(HISTORY_SCRIPT),
                timeout=10,
            )

            values = [
                float(v)
                for v in values
            ]

            if len(values) > len(best):
                best = values

        except Exception:
            log.debug(
                "Frame history read failed",
                exc_info=True,
            )

    return best


async def diagnose_frames(page):
    log.info(
        "Frame diagnostics: total_frames=%d",
        len(page.frames),
    )

    for index, frame in enumerate(page.frames):
        try:
            data = await asyncio.wait_for(
                frame.evaluate(DIAGNOSTIC_SCRIPT),
                timeout=10,
            )

            log.info(
                "Frame %d diagnostics: %s",
                index,
                json.dumps(
                    data,
                    ensure_ascii=False,
                ),
            )

        except Exception as exc:
            log.warning(
                "Frame %d diagnostic failed: %s",
                index,
                type(exc).__name__,
            )


async def page_issue(page):
    for frame in page.frames:
        try:
            text = " ".join(
                (
                    await frame.locator(
                        "body"
                    ).inner_text(timeout=1000)
                ).lower().split()
            )

        except Exception:
            continue

        if (
            "site is not available in your region"
            in text
        ):
            return (
                "Сайт недоступен "
                "в регионе сервера."
            )

    return None


async def open_game(context, config):
    page = await context.new_page()

    try:
        log.info("Opening Lucky Jet page")

        response = await page.goto(
            config.game_url,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        log.info(
            "Game page opened: HTTP %s",
            response.status if response else "unknown",
        )

        await page.wait_for_timeout(8000)

        demo_labels = [
            "Демо-режим",
            "Демо режим",
            "Играть бесплатно",
            "Demo mode",
            "Play demo",
            "Play for free",
        ]

        demo_started = False

        for frame in page.frames:
            if demo_started:
                break

            for label in demo_labels:
                try:
                    button = frame.get_by_role(
                        "button",
                        name=label,
                        exact=True,
                    )

                    if await button.count() == 1:
                        if await button.is_visible():
                            await button.click(timeout=5000)

                            log.info(
                                "Demo mode button clicked: %s",
                                label,
                            )

                            demo_started = True
                            break

                except Exception as exc:
                    log.debug(
                        "Demo button unavailable: %s",
                        type(exc).__name__,
                    )

        if not demo_started:
            log.warning(
                "Demo mode button not found; "
                "no other controls were clicked"
            )

        await page.wait_for_timeout(12000)

        log.info(
            "After demo attempt: frames=%d",
            len(page.frames),
        )

        await diagnose_frames(page)

        return page

    except Exception:
        await page.close()
        raise


async def run_monitor(config: Config):
    state = await load_state(config)

    last_error_notice = 0.0
    last_diagnostic = 0.0

    log.info(
        "Monitor starting; read-only mode"
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=config.headless
        )

        try:
            context = await browser.new_context(
                viewport={
                    "width": 430,
                    "height": 932,
                }
            )

            page = None

            while True:
                try:
                    if (
                        page is None
                        or page.is_closed()
                    ):
                        page = await open_game(
                            context,
                            config,
                        )

                        last_diagnostic = 0.0

                        await send(
                            config,
                            "Монитор запущен. "
                            "Ставки не выполняются. "
                            "Отслеживаются завершённые раунды.",
                        )

                    issue = await page_issue(page)
                    now = time.monotonic()

                    if issue:
                        log.warning(
                            "Game page issue: %s",
                            issue,
                        )

                        if (
                            now - last_error_notice
                            >= config.error_notify_cooldown
                        ):
                            await send(config, issue)
                            last_error_notice = now

                        await asyncio.sleep(
                            config.poll_seconds
                        )
                        continue

                    if (
                        now - last_diagnostic
                        >= DIAGNOSTIC_INTERVAL
                    ):
                        await diagnose_frames(page)
                        last_diagnostic = now

                    current = await read_history(
                        page,
                        config,
                    )

                    if (
                        len(current)
                        >= config.minimum_history_items
                    ):
                        if not state.previous:
                            state.previous = current

                            await save_state(
                                config,
                                state,
                            )

                            log.info(
                                "Initial history: %s",
                                current,
                            )

                        elif current != state.previous:
                            results = new_results(
                                state.previous,
                                current,
                            )

                            if not results:
                                log.warning(
                                    "History changed but "
                                    "could not align: "
                                    "previous=%s current=%s",
                                    state.previous[:8],
                                    current[:8],
                                )

                            for multiplier in results:
                                log.info(
                                    "Completed round: %.2fx",
                                    multiplier,
                                )

                                if (
                                    multiplier
                                    < config.threshold
                                ):
                                    state.streak += 1

                                    if (
                                        state.streak
                                        == config.trigger_streak
                                    ):
                                        state.active = True

                                        await send(
                                            config,
                                            (
                                                f"Зафиксировано "
                                                f"{state.streak} "
                                                f"раунда подряд ниже "
                                                f"{config.threshold:g}x.\n"
                                                f"Последний: "
                                                f"{multiplier:.2f}x"
                                            ),
                                        )

                                    elif state.active:
                                        await send(
                                            config,
                                            (
                                                f"Ещё один раунд ниже "
                                                f"{config.threshold:g}x: "
                                                f"{multiplier:.2f}x.\n"
                                                f"Длина серии: "
                                                f"{state.streak}"
                                            ),
                                        )

                                else:
                                    if state.active:
                                        await send(
                                            config,
                                            (
                                                "Серия завершилась.\n"
                                                f"Результат: "
                                                f"{multiplier:.2f}x"
                                            ),
                                        )

                                    state.active = False
                                    state.streak = 0

                            state.previous = current

                            await save_state(
                                config,
                                state,
                            )

                    else:
                        log.debug(
                            "Insufficient history: %d/%d",
                            len(current),
                            config.minimum_history_items,
                        )

                    await asyncio.sleep(
                        config.poll_seconds
                    )

                except asyncio.CancelledError:
                    raise

                except Exception as exc:
                    log.error(
                        "Monitor iteration failed: %s",
                        type(exc).__name__,
                    )

                    log.debug(
                        "Monitor error details",
                        exc_info=True,
                    )

                    now = time.monotonic()

                    if (
                        now - last_error_notice
                        >= config.error_notify_cooldown
                    ):
                        await send(
                            config,
                            (
                                "Ошибка мониторинга: "
                                f"{type(exc).__name__}. "
                                "Повторная попытка."
                            ),
                        )

                        last_error_notice = now

                    if page is not None:
                        try:
                            await page.close()
                        except Exception:
                            pass

                    page = None

                    await asyncio.sleep(
                        max(
                            3.0,
                            config.poll_seconds,
                        )
                    )

        finally:
            await browser.close()