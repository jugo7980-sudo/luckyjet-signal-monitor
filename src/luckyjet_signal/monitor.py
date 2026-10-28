
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
            return True

    except Exception:
        log.exception("Telegram send failed")
        return False


def new_results(previous, current):
    """
    History is expected newest-first.
    Return new results oldest-first.
    """

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
    script = r"""
(() => {
    const rows = [];

    for (
        const el of document.querySelectorAll("body *")
    ) {
        if (el.children.length) continue;

        const text = (
            el.innerText ||
            el.textContent ||
            ""
        ).trim().toLowerCase();

        if (
            !/^\d+(?:[.,]\d+)?\s*x?$/.test(text)
        ) {
            continue;
        }

        const value = Number(
            text
                .replace(/x\s*$/, "")
                .replace(",", ".")
                .trim()
        );

        if (
            !Number.isFinite(value) ||
            value < 1 ||
            value > 100000
        ) {
            continue;
        }

        const rect =
            el.getBoundingClientRect();

        const style =
            getComputedStyle(el);

        if (
            style.display === "none" ||
            style.visibility === "hidden" ||
            Number(style.opacity) === 0
        ) {
            continue;
        }

        if (
            rect.width <= 0 ||
            rect.width > 180 ||
            rect.height <= 0 ||
            rect.height > 90
        ) {
            continue;
        }

        if (
            rect.top < 0 ||
            rect.top > innerHeight * 0.38
        ) {
            continue;
        }

        let row = rows.find(
            r => Math.abs(r.y - rect.top) < 12
        );

        if (!row) {
            row = {
                y: rect.top,
                items: [],
            };

            rows.push(row);
        }

        row.items.push({
            x: rect.left,
            value,
        });
    }

    rows.sort(
        (a, b) =>
            b.items.length - a.items.length ||
            a.y - b.y
    );

    if (
        !rows.length ||
        rows[0].items.length < 3
    ) {
        return [];
    }

    rows[0].items.sort(
        (a, b) => a.x - b.x
    );

    return rows[0].items.map(
        item => item.value
    );
})()
"""

    best = []

    for frame in page.frames:
        try:
            values = [
                float(v)
                for v in await frame.evaluate(script)
            ]

            if len(values) > len(best):
                best = values

        except Exception:
            log.debug(
                "Frame history read failed",
                exc_info=True,
            )

    return best


async def page_issue(page):
    for frame in page.frames:
        try:
            text = " ".join(
                (
                    await frame.locator("body").inner_text(
                        timeout=1000
                    )
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


async def run_monitor(config: Config):
    state = await load_state(config)

    last_error_notice = 0.0

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

            page = await context.new_page()

            await page.goto(
                config.game_url,
                wait_until="domcontentloaded",
                timeout=60000,
            )

            await send(
                config,
                "Монитор запущен. "
                "Ставки не выполняются. "
                "Отслеживаются завершённые раунды.",
            )

            while True:
                try:
                    issue = await page_issue(page)
                    now = time.monotonic()

                    if issue:
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
                                                f"раунда подряд "
                                                f"ниже "
                                                f"{config.threshold:g}x.\n"
                                                f"Последний: "
                                                f"{multiplier:.2f}x"
                                            ),
                                        )

                                    elif state.active:
                                        await send(
                                            config,
                                            (
                                                f"Ещё один раунд "
                                                f"ниже "
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
                                                f"Серия завершилась.\n"
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

                    await asyncio.sleep(
                        config.poll_seconds
                    )

                except asyncio.CancelledError:
                    raise

                except Exception as exc:
                    log.exception(
                        "Monitor iteration failed"
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

                    try:
                        if page.is_closed():
                            page = await context.new_page()

                            await page.goto(
                                config.game_url,
                                wait_until="domcontentloaded",
                                timeout=60000,
                            )

                    except Exception:
                        log.exception(
                            "Page recovery failed"
                        )

                    await asyncio.sleep(
                        max(
                            3.0,
                            config.poll_seconds,
                        )
                    )

        finally:
            await browser.close()
