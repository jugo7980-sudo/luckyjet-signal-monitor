from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
from playwright.async_api import async_playwright

from .settings import Config

log = logging.getLogger("luckyjet.monitor")
STATE = Path("data/state.json")

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

DEMO_LABELS = (
    "Демо-режим",
    "Демо режим",
    "Играть бесплатно",
    "Demo mode",
    "Play demo",
    "Play for free",
)

NETWORK_URL_HINTS = (
    "crash",
    "100hp",
    "round",
    "history",
    "state",
    "lucky",
)


class State:
    def __init__(
        self,
        active=False,
        streak=0,
        next_amount=150,
        previous=None,
        last_round_id=None,
    ):
        self.active = bool(active)
        self.streak = int(streak)
        self.next_amount = int(next_amount)
        self.previous = list(previous or [])
        self.last_round_id = (
            str(last_round_id)
            if last_round_id
            else None
        )

    def dump(self):
        return {
            "active": self.active,
            "streak": self.streak,
            "next_amount": self.next_amount,
            "previous": self.previous,
            "last_round_id": self.last_round_id,
        }


class LiveCapture:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._rounds = []
        self._source = ""
        self._gateway_url = None
        self._gateway_headers = {}
        self._logged_urls = set()

    async def ingest(self, payload, source):
        rounds = extract_rounds_from_payload(payload)

        if not rounds:
            return 0

        async with self._lock:
            if len(rounds) >= 2:
                self._rounds = rounds[:50]

            else:
                item = rounds[0]
                rid = item.get("id")

                if rid:
                    self._rounds = [
                        item,
                        *[
                            r
                            for r in self._rounds
                            if r.get("id") != rid
                        ],
                    ][:50]

                elif (
                    not self._rounds
                    or self._rounds[0]["coefficient"]
                    != item["coefficient"]
                ):
                    self._rounds = [
                        item,
                        *self._rounds,
                    ][:50]

            self._source = source

            return len(self._rounds)

    async def snapshot(self):
        async with self._lock:
            return (
                [dict(r) for r in self._rounds],
                self._source,
            )

    async def remember_gateway(
        self,
        url,
        headers,
    ):
        safe = safe_url(url)
        selected = {}

        for key in (
            "accept",
            "user-agent",
            "origin",
            "referer",
            "session-id",
            "customer-id",
            "cookie",
        ):
            value = headers.get(key)

            if value:
                selected[key] = value

        async with self._lock:
            self._gateway_url = url
            self._gateway_headers = selected

            if safe not in self._logged_urls:
                self._logged_urls.add(safe)

                log.info(
                    "Discovered crash data endpoint: %s",
                    safe,
                )

    async def gateway(self):
        async with self._lock:
            return (
                self._gateway_url,
                dict(self._gateway_headers),
            )


async def _redis(config):
    url = os.getenv(
        "REDIS_URL",
        "",
    ).strip()

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
                raw = await r.get(
                    "luckyjet:state"
                )

                if raw:
                    return _state_from_dict(
                        json.loads(raw),
                        config,
                    )

            finally:
                await r.aclose()

        if STATE.exists():
            return _state_from_dict(
                json.loads(
                    STATE.read_text(
                        encoding="utf-8"
                    )
                ),
                config,
            )

    except Exception:
        log.exception(
            "State load failed"
        )

    return State(
        next_amount=config.base_amount
    )


def _state_from_dict(
    data,
    config,
):
    return State(
        active=data.get(
            "active",
            False,
        ),
        streak=data.get(
            "streak",
            0,
        ),
        next_amount=data.get(
            "next_amount",
            config.base_amount,
        ),
        previous=data.get(
            "previous"
        ),
        last_round_id=data.get(
            "last_round_id"
        ),
    )


async def save_state(
    config,
    state,
):
    raw = json.dumps(
        state.dump()
    )

    try:
        r = await _redis(config)

        if r:
            try:
                await r.set(
                    "luckyjet:state",
                    raw,
                )

                return

            finally:
                await r.aclose()

    except Exception:
        log.exception(
            "Redis save failed"
        )

    STATE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    STATE.write_text(
        raw,
        encoding="utf-8",
    )


async def send(
    config,
    message,
):
    token = config.telegram_bot_token
    chat_id = config.telegram_chat_id

    if not token or not chat_id:
        log.warning(
            "Telegram credentials missing"
        )

        return False

    url = (
        "https://api.telegram.org/"
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

        log.info(
            "Telegram message delivered"
        )

        return True

    except httpx.HTTPStatusError as exc:
        log.error(
            "Telegram rejected message: HTTP %s",
            exc.response.status_code,
        )

        return False

    except httpx.RequestError as exc:
        log.error(
            "Telegram connection failed: %s",
            type(exc).__name__,
        )

        return False


def format_last_multiplier(
    multiplier,
):
    return (
        "Последний коэффициент: "
        f"{multiplier:.2f}x"
    )


def safe_url(url):
    try:
        parts = urlsplit(url)

        return urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                "",
                "",
            )
        )

    except Exception:
        return "<invalid-url>"


def canonical_game_url(url):
    return re.sub(
        r"v_1wingames:luckyjet",
        "v_1winGames:LuckyJet",
        url,
        flags=re.IGNORECASE,
    )


def _number(value):
    if isinstance(
        value,
        bool,
    ):
        return None

    if isinstance(
        value,
        (int, float),
    ):
        result = float(value)

    elif isinstance(
        value,
        str,
    ):
        try:
            result = float(
                value.replace(
                    ",",
                    ".",
                ).strip()
            )

        except ValueError:
            return None

    else:
        return None

    if 1 <= result <= 100000:
        return result

    return None


def extract_rounds_from_payload(
    payload,
):
    found = []

    def visit(node):
        if isinstance(
            node,
            dict,
        ):
            coefficient = None

            for key in (
                "topCoefficient",
                "top_coefficient",
            ):
                if key in node:
                    coefficient = _number(
                        node.get(key)
                    )

                    if coefficient is not None:
                        break

            if coefficient is None:
                for key in (
                    "finalValues",
                    "final_values",
                ):
                    values = node.get(key)

                    if (
                        isinstance(
                            values,
                            list,
                        )
                        and values
                    ):
                        coefficient = _number(
                            values[0]
                        )

                        if coefficient is not None:
                            break

            if coefficient is not None:
                rid = (
                    node.get("id")
                    or node.get(
                        "roundId"
                    )
                    or node.get(
                        "round_id"
                    )
                    or node.get(
                        "hash"
                    )
                )

                found.append(
                    {
                        "id": (
                            str(rid)
                            if rid
                            else None
                        ),
                        "coefficient": coefficient,
                    }
                )

            for value in node.values():
                visit(value)

        elif isinstance(
            node,
            list,
        ):
            for value in node:
                visit(value)

    visit(payload)

    result = []
    seen_ids = set()

    for item in found:
        rid = item.get("id")

        if rid:
            if rid in seen_ids:
                continue

            seen_ids.add(rid)

        result.append(item)

    return result


def parse_possible_json(
    payload,
):
    if isinstance(
        payload,
        bytes,
    ):
        try:
            payload = payload.decode(
                "utf-8",
                errors="ignore",
            )

        except Exception:
            return None

    if not isinstance(
        payload,
        str,
    ):
        if isinstance(
            payload,
            (dict, list),
        ):
            return payload

        return None

    text = payload.strip()
    candidates = [text]

    for marker in (
        "{",
        "[",
    ):
        pos = text.find(marker)

        if pos > 0:
            candidates.append(
                text[pos:]
            )

    for candidate in candidates:
        try:
            return json.loads(
                candidate
            )

        except Exception:
            continue

    return None


def new_results(
    previous,
    current,
):
    if (
        not previous
        or not current
        or previous == current
    ):
        return []

    limit = min(
        8,
        max(
            1,
            len(current) - 1,
        ),
    )

    candidates = []

    for count in range(
        1,
        limit + 1,
    ):
        overlap = min(
            len(previous),
            len(current) - count,
        )

        if overlap <= 0:
            continue

        if (
            current[
                count:count + overlap
            ]
            == previous[:overlap]
        ):
            candidates.append(
                (
                    overlap,
                    list(
                        reversed(
                            current[:count]
                        )
                    ),
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
            min(
                len(previous),
                len(current),
            ) - 1,
        ),
    )

    if overlap >= minimum:
        return results

    return []


async def read_dom_history(page):
    script = r"""
(() => {
    const visible = el => {
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);

        return (
            r.width > 0 &&
            r.height > 0 &&
            s.display !== 'none' &&
            s.visibility !== 'hidden' &&
            Number(s.opacity) !== 0
        );
    };

    const points = [];

    for (
        const el
        of document.querySelectorAll('body *')
    ) {
        if (
            el.children.length ||
            !visible(el)
        ) {
            continue;
        }

        const text = (
            el.innerText ||
            el.textContent ||
            ''
        ).trim();

        const m = text.match(
            /^(\d+(?:[.,]\d+)?)\s*x$/i
        );

        if (!m) {
            continue;
        }

        const value = Number(
            m[1].replace(',', '.')
        );

        if (
            !Number.isFinite(value) ||
            value < 1 ||
            value > 100000
        ) {
            continue;
        }

        const r = el.getBoundingClientRect();

        if (
            r.top < 0 ||
            r.top > innerHeight * 0.5
        ) {
            continue;
        }

        points.push({
            value,
            x: r.left,
            y: r.top
        });
    }

    const rows = [];

    for (const item of points) {
        let row = rows.find(
            r => Math.abs(r.y - item.y) < 14
        );

        if (!row) {
            row = {
                y: item.y,
                items: []
            };

            rows.push(row);
        }

        row.items.push(item);
    }

    rows.sort(
        (a, b) =>
            b.items.length -
            a.items.length ||
            a.y -
            b.y
    );

    const best = rows.find(
        r => r.items.length >= 3
    );

    if (!best) {
        return [];
    }

    best.items.sort(
        (a, b) =>
            a.x - b.x
    );

    return best.items
        .slice(0, 30)
        .map(x => x.value);
})()
"""

    best = []

    for frame in list(
        page.frames
    ):
        try:
            values = await asyncio.wait_for(
                frame.evaluate(
                    script
                ),
                timeout=5,
            )

            values = [
                float(v)
                for v in values
            ]

            if len(values) > len(best):
                best = values

        except Exception:
            continue

    return best


async def _click_demo_js(
    frame,
    labels,
):
    script = r"""
(labels) => {
    const norm = s =>
        (s || '')
        .replace(/\s+/g, ' ')
        .trim();

    const visible = el => {
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);

        return (
            r.width > 1 &&
            r.height > 1 &&
            s.display !== 'none' &&
            s.visibility !== 'hidden' &&
            Number(s.opacity) !== 0
        );
    };

    for (const label of labels) {
        const all = Array.from(
            document.querySelectorAll('body *')
        );

        const candidates = all.filter(
            el =>
                norm(
                    el.innerText ||
                    el.textContent
                ) === label
        );

        candidates.sort(
            (a, b) =>
                a.children.length -
                b.children.length
        );

        for (const el of candidates) {
            if (!visible(el)) {
                continue;
            }

            const target =
                el.closest(
                    'button,a,[role="button"],[onclick],[tabindex]'
                ) || el;

            if (!visible(target)) {
                continue;
            }

            target.scrollIntoView({
                block: 'center',
                inline: 'center'
            });

            const r =
                target.getBoundingClientRect();

            const hit =
                document.elementFromPoint(
                    r.left + r.width / 2,
                    r.top + r.height / 2
                );

            const clickable =
                hit &&
                (
                    hit === target ||
                    target.contains(hit)
                )
                    ? hit
                    : target;

            clickable.dispatchEvent(
                new PointerEvent(
                    'pointerdown',
                    {
                        bubbles: true,
                        cancelable: true
                    }
                )
            );

            clickable.dispatchEvent(
                new PointerEvent(
                    'pointerup',
                    {
                        bubbles: true,
                        cancelable: true
                    }
                )
            );

            clickable.dispatchEvent(
                new MouseEvent(
                    'click',
                    {
                        bubbles: true,
                        cancelable: true,
                        view: window
                    }
                )
            );

            return {
                ok: true,
                label,
                tag: target.tagName,
                role:
                    target.getAttribute(
                        'role'
                    ) || '',
                className:
                    typeof target.className
                        === 'string'
                        ? target.className.slice(
                            0,
                            120
                        )
                        : ''
            };
        }
    }

    return {
        ok: false
    };
}
"""

    try:
        return await frame.evaluate(
            script,
            list(labels),
        )

    except Exception:
        return {
            "ok": False
        }


async def click_demo_mode(page):
    for frame in list(
        page.frames
    ):
        for label in DEMO_LABELS:
            try:
                locator = frame.get_by_text(
                    label,
                    exact=True,
                )

                count = min(
                    await locator.count(),
                    6,
                )

                for i in range(count):
                    item = locator.nth(i)

                    if await item.is_visible():
                        await (
                            item
                            .scroll_into_view_if_needed(
                                timeout=2000
                            )
                        )

                        await item.click(
                            timeout=4000
                        )

                        log.info(
                            "Demo mode clicked "
                            "with Playwright: %s",
                            label,
                        )

                        return True

            except Exception:
                continue

    for frame in list(
        page.frames
    ):
        result = await _click_demo_js(
            frame,
            DEMO_LABELS,
        )

        if result.get("ok"):
            log.info(
                "Demo mode clicked "
                "with DOM fallback: "
                "label=%s tag=%s role=%s",
                result.get("label"),
                result.get("tag"),
                result.get("role"),
            )

            return True

    log.warning(
        "Exact demo control "
        "was not clickable"
    )

    return False


async def page_issue(page):
    for frame in list(
        page.frames
    ):
        try:
            text = " ".join(
                (
                    await frame
                    .locator("body")
                    .inner_text(
                        timeout=800
                    )
                )
                .lower()
                .split()
            )

        except Exception:
            continue

        if (
            "site is not available "
            "in your region"
            in text
        ):
            return (
                "Сайт недоступен "
                "в регионе сервера."
            )

    return None


async def diagnose_page(
    page,
    capture,
):
    rounds, source = (
        await capture.snapshot()
    )

    frame_info = []

    for frame in list(
        page.frames
    )[:8]:
        try:
            frame_info.append(
                safe_url(
                    frame.url
                )
            )

        except Exception:
            frame_info.append(
                "<frame-error>"
            )

    log.info(
        "Diagnostics: frames=%d "
        "frame_urls=%s "
        "network_rounds=%d "
        "source=%s",
        len(page.frames),
        frame_info,
        len(rounds),
        source or "none",
    )


async def handle_response(
    response,
    capture,
):
    try:
        url = response.url.lower()

        if not any(
            hint in url
            for hint
            in NETWORK_URL_HINTS
        ):
            return

        if any(
            bad in url
            for bad in (
                "leaderboard",
                "top-coefficient",
                "top_coeff",
            )
        ):
            return

        if (
            response.request.resource_type
            not in (
                "xhr",
                "fetch",
                "document",
                "other",
            )
        ):
            return

        content_type = (
            await response.all_headers()
        ).get(
            "content-type",
            "",
        ).lower()

        if (
            "json"
            not in content_type
            and not any(
                x in url
                for x in (
                    "/state",
                    "history",
                    "round",
                )
            )
        ):
            return

        text = await response.text()

        if len(text) > 1_500_000:
            return

        payload = parse_possible_json(
            text
        )

        if payload is None:
            return

        count = await capture.ingest(
            payload,
            (
                "response:"
                + safe_url(
                    response.url
                )
            ),
        )

        if count:
            log.info(
                "Captured completed-round "
                "history from network: "
                "%d rounds",
                count,
            )

    except Exception:
        return


async def handle_request(
    request,
    capture,
):
    try:
        url_l = request.url.lower()

        if (
            "crash-gateway"
            not in url_l
            and "100hp.app"
            not in url_l
        ):
            return

        if (
            request.method.upper()
            != "GET"
        ):
            return

        if not any(
            hint in url_l
            for hint in (
                "/state",
                "history",
                "round",
            )
        ):
            return

        if any(
            bad in url_l
            for bad in (
                "leaderboard",
                "top-coefficient",
                "top_coeff",
            )
        ):
            return

        headers = (
            await request.all_headers()
        )

        await capture.remember_gateway(
            request.url,
            headers,
        )

    except Exception:
        return


async def handle_ws_frame(
    payload,
    capture,
    source,
):
    try:
        parsed = parse_possible_json(
            payload
        )

        if parsed is None:
            return

        count = await capture.ingest(
            parsed,
            source,
        )

        if count:
            log.info(
                "Captured completed "
                "round(s) from WebSocket: "
                "%d stored",
                count,
            )

    except Exception:
        return


def attach_network_capture(
    page,
    capture,
):
    def on_request(request):
        asyncio.create_task(
            handle_request(
                request,
                capture,
            )
        )

    def on_response(response):
        asyncio.create_task(
            handle_response(
                response,
                capture,
            )
        )

    def on_websocket(ws):
        safe = safe_url(
            ws.url
        )

        log.info(
            "WebSocket opened: %s",
            safe,
        )

        def on_frame(payload):
            asyncio.create_task(
                handle_ws_frame(
                    payload,
                    capture,
                    f"ws:{safe}",
                )
            )

        ws.on(
            "framereceived",
            on_frame,
        )

    def on_page_error(exc):
        log.warning(
            "Browser page error: %s",
            type(exc).__name__,
        )

    def on_request_failed(request):
        try:
            url = safe_url(
                request.url
            )

            if any(
                h
                in request.url.lower()
                for h
                in NETWORK_URL_HINTS
            ):
                log.warning(
                    "Game request failed: %s",
                    url,
                )

        except Exception:
            pass

    page.on(
        "request",
        on_request,
    )

    page.on(
        "response",
        on_response,
    )

    page.on(
        "websocket",
        on_websocket,
    )

    page.on(
        "pageerror",
        on_page_error,
    )

    page.on(
        "requestfailed",
        on_request_failed,
    )


async def poll_discovered_gateway(
    capture,
):
    url, headers = (
        await capture.gateway()
    )

    if not url:
        return 0

    try:
        async with httpx.AsyncClient(
            timeout=10,
            follow_redirects=True,
        ) as client:
            response = await client.get(
                url,
                headers=headers,
            )

        if response.status_code >= 400:
            return 0

        payload = parse_possible_json(
            response.text
        )

        if payload is None:
            return 0

        return await capture.ingest(
            payload,
            (
                "direct:"
                + safe_url(url)
            ),
        )

    except Exception:
        return 0


async def wait_for_game_data(
    page,
    capture,
    timeout_seconds=20,
):
    end = (
        time.monotonic()
        + timeout_seconds
    )

    while time.monotonic() < end:
        rounds, _ = (
            await capture.snapshot()
        )

        if rounds:
            return True

        dom = await read_dom_history(
            page
        )

        if len(dom) >= 3:
            return True

        await asyncio.sleep(1)

    return False


async def navigate_and_start_demo(
    page,
    url,
    capture,
):
    log.info(
        "Opening game page: %s",
        safe_url(url),
    )

    response = await page.goto(
        url,
        wait_until="domcontentloaded",
        timeout=60000,
    )

    log.info(
        "Game navigation completed: "
        "HTTP %s, frames=%d",
        (
            response.status
            if response
            else "unknown"
        ),
        len(page.frames),
    )

    await page.wait_for_timeout(
        6000
    )

    clicked = await click_demo_mode(
        page
    )

    if clicked:
        await page.wait_for_timeout(
            2500
        )

    else:
        log.warning(
            "Demo click did not "
            "fire on this route"
        )

    ready = await wait_for_game_data(
        page,
        capture,
        timeout_seconds=15,
    )

    await diagnose_page(
        page,
        capture,
    )

    return ready


async def open_game(
    context,
    config,
    capture,
):
    page = await context.new_page()

    attach_network_capture(
        page,
        capture,
    )

    try:
        original = config.game_url

        if await navigate_and_start_demo(
            page,
            original,
            capture,
        ):
            return page

        canonical = canonical_game_url(
            original
        )

        if canonical != original:
            log.info(
                "Retrying with canonical "
                "Lucky Jet route"
            )

            if await navigate_and_start_demo(
                page,
                canonical,
                capture,
            ):
                return page

        log.warning(
            "Game opened but no "
            "completed-round history "
            "is visible yet"
        )

        return page

    except Exception:
        await page.close()
        raise


async def process_completed_round(
    config,
    state,
    multiplier,
):
    log.info(
        "Completed round: %.2fx",
        multiplier,
    )

    await send(
        config,
        format_last_multiplier(
            multiplier
        ),
    )

    if multiplier < config.threshold:
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


async def process_network_history(
    config,
    state,
    rounds,
):
    if not rounds:
        return False

    ids_available = bool(
        rounds[0].get("id")
    )

    coefficients = [
        float(
            r["coefficient"]
        )
        for r in rounds
    ]

    if ids_available:
        newest_id = rounds[0].get(
            "id"
        )

        if not state.last_round_id:
            state.last_round_id = (
                newest_id
            )

            state.previous = (
                coefficients
            )

            await send(
                config,
                format_last_multiplier(
                    coefficients[0]
                ),
            )

            await save_state(
                config,
                state,
            )

            log.info(
                "Initial network history: "
                "%d rounds",
                len(rounds),
            )

            return True

        index = next(
            (
                i
                for i, item
                in enumerate(rounds)
                if item.get("id")
                == state.last_round_id
            ),
            None,
        )

        if index is None:
            if (
                newest_id
                and newest_id
                != state.last_round_id
            ):
                await process_completed_round(
                    config,
                    state,
                    coefficients[0],
                )

            state.last_round_id = (
                newest_id
            )

            state.previous = (
                coefficients
            )

            await save_state(
                config,
                state,
            )

            return True

        new_items = list(
            reversed(
                rounds[:index]
            )
        )

        for item in new_items:
            await process_completed_round(
                config,
                state,
                float(
                    item[
                        "coefficient"
                    ]
                ),
            )

        if new_items:
            state.last_round_id = (
                newest_id
            )

            state.previous = (
                coefficients
            )

            await save_state(
                config,
                state,
            )

        return True

    return await process_dom_history(
        config,
        state,
        coefficients,
    )


async def process_dom_history(
    config,
    state,
    current,
):
    if (
        len(current)
        < config.minimum_history_items
    ):
        return False

    if not state.previous:
        state.previous = list(
            current
        )

        await send(
            config,
            format_last_multiplier(
                float(
                    current[0]
                )
            ),
        )

        await save_state(
            config,
            state,
        )

        log.info(
            "Initial DOM history: %s",
            current[:8],
        )

        return True

    if current == state.previous:
        return True

    results = new_results(
        state.previous,
        current,
    )

    if not results:
        log.warning(
            "History changed but "
            "could not align; "
            "previous=%s current=%s",
            state.previous[:8],
            current[:8],
        )

        state.previous = list(
            current
        )

        await save_state(
            config,
            state,
        )

        return True

    for multiplier in results:
        await process_completed_round(
            config,
            state,
            float(multiplier),
        )

    state.previous = list(
        current
    )

    await save_state(
        config,
        state,
    )

    return True


async def run_monitor(
    config: Config,
):
    state = await load_state(
        config
    )

    capture = None
    last_error_notice = 0.0
    last_diagnostic = 0.0
    missing_since = None
    startup_sent = False

    log.info(
        "Monitor starting; "
        "read-only mode"
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=config.headless
        )

        try:
            context = (
                await browser.new_context(
                    viewport={
                        "width": 430,
                        "height": 932,
                    },
                    locale="ru-RU",
                )
            )

            page = None

            while True:
                try:
                    if (
                        page is None
                        or page.is_closed()
                    ):
                        capture = LiveCapture()

                        page = await open_game(
                            context,
                            config,
                            capture,
                        )

                        missing_since = None
                        last_diagnostic = 0.0

                        if not startup_sent:
                            await send(
                                config,
                                (
                                    "Монитор запущен. "
                                    "Ставки не выполняются. "
                                    "Отслеживаются только "
                                    "завершённые раунды."
                                ),
                            )

                            startup_sent = True

                    issue = await page_issue(
                        page
                    )

                    now = time.monotonic()

                    if issue:
                        log.warning(
                            "Game page issue: %s",
                            issue,
                        )

                        if (
                            now
                            - last_error_notice
                            >= config
                            .error_notify_cooldown
                        ):
                            await send(
                                config,
                                issue,
                            )

                            last_error_notice = (
                                now
                            )

                        await asyncio.sleep(
                            config.poll_seconds
                        )

                        continue

                    await poll_discovered_gateway(
                        capture
                    )

                    (
                        network_rounds,
                        source,
                    ) = await capture.snapshot()

                    worked = False

                    if network_rounds:
                        worked = (
                            await process_network_history(
                                config,
                                state,
                                network_rounds,
                            )
                        )

                    else:
                        dom_history = (
                            await read_dom_history(
                                page
                            )
                        )

                        worked = (
                            await process_dom_history(
                                config,
                                state,
                                dom_history,
                            )
                        )

                    if worked:
                        missing_since = None

                    else:
                        if missing_since is None:
                            missing_since = now

                        elif (
                            now
                            - missing_since
                            >= 90
                        ):
                            log.warning(
                                "No completed-round "
                                "history for 90s; "
                                "reopening game"
                            )

                            try:
                                await page.close()

                            except Exception:
                                pass

                            page = None
                            missing_since = None

                            await asyncio.sleep(3)

                            continue

                    if (
                        now
                        - last_diagnostic
                        >= 60
                    ):
                        (
                            network_rounds,
                            source,
                        ) = await capture.snapshot()

                        if network_rounds:
                            dom = []

                        else:
                            dom = (
                                await read_dom_history(
                                    page
                                )
                            )

                        log.info(
                            "History diagnostics: "
                            "network=%d "
                            "source=%s "
                            "dom=%d "
                            "frames=%d",
                            len(network_rounds),
                            source or "none",
                            len(dom),
                            len(page.frames),
                        )

                        await diagnose_page(
                            page,
                            capture,
                        )

                        last_diagnostic = (
                            now
                        )

                    await asyncio.sleep(
                        config.poll_seconds
                    )

                except asyncio.CancelledError:
                    raise

                except Exception as exc:
                    log.error(
                        "Monitor iteration "
                        "failed: %s",
                        type(exc).__name__,
                    )

                    log.debug(
                        "Monitor error details",
                        exc_info=True,
                    )

                    now = time.monotonic()

                    if (
                        now
                        - last_error_notice
                        >= config
                        .error_notify_cooldown
                    ):
                        await send(
                            config,
                            (
                                "Ошибка мониторинга: "
                                f"{type(exc).__name__}. "
                                "Повторная попытка."
                            ),
                        )

                        last_error_notice = (
                            now
                        )

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