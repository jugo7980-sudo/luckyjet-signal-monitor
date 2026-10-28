from __future__ import annotations
import asyncio
import json
import logging
import time
from pathlib import Path
import httpx
from playwright.async_api import async_playwright
from .settings import Config

log = logging.getLogger("luckyjet.monitor")
STATE = Path("data/state.json")

class State:
    def __init__(self, active=False, streak=0, next_amount=150, previous=None):
 self.active = bool(active)
 self.streak = int(streak)
 self.next_amount = int( ext_amount)
 self.previous = list(previous or [])

    def dump(self):
        return {
            "active": self.active,
            "streak": self.streak,
            "next_amount": self.next_amount,
            "previous": self.previous,
        }

async def _redis(config):
 url = __import__("os").getenv("REDIS_URL", "").strip()
    if not url:
        return None
    import redis.asyncio as redis
    return redis.from_url(url, decode_responses=True)

async def load_state(config):
    r = await _redis(config)
    try:
        if r:
            raw = await r.get("luckyjet:state")
            if raw:
                d = json.loads(raw)
                return State(d["active"], d["streak"], d["next_amount"], d.get("previous"))
        if STATE.exists():
            d = json.loads(STATE.read_text())
            return State(d["active"], d["streak"], d["next_amount"], d.get("previous"))
    except Exception:
        log.exception("state load failed")
    return State(next_amount=config.base_amount)

async def save_state(config, state):
    raw = json.dumps(state.dump())
    r = await _redis(config)
    try:
        if r:
            await r.set("luckyjet:state", raw)
            return
    except Exception:
        log.exception("redis save failed")
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(raw)

async def send(config, text):
    url = f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(url, json={"chat_id": config.telegram_chat_id, "text": text})
            response.raise_for_status()
            return True
    except Exception:
        log.exception("telegram send failed")
        return False

def new_results(previous, current):
    if not previous or not current or previous == current:
        return []
    limit = min(6, max(1, len(current) - 1))
    candidates = []
    for count in range(1, limit + 1):
        overlap = min(len(previous), len(current) - count)
        if overlap > 0 and current[count:count + overlap] == previous[:overlap]:
            candidates.append((overlap, list(reversed(current[:count]))))
    for count in range(1, limit + 1):
        overlap = min(len(previous) - count, len(current) - count)
        if overlap > 0 and current[:overlap] == previous[count:count + overlap]:
            candidates.append((overlap, current[-count:]))
    if not candidates:
        return []
    overlap, results = max(candidates, key=lambda item: item[0])
    minimum = min(3, max(1, min(len(previous), len(current)) - 1))
    return results if overlap >= minimum else []

async def read_history(page, config):
    script = """
(() => {
const rows = [];
for (const el of document.querySelectorAll('body *')) {
    if (el.children.length) continue;
    const text = (el.innerText || el.textContent || '').trim().toLowerCase();
    const clean = text.replaceAll('x','').replace(',','.').trim();
    if (!clean || clean.length > 12) continue;
    const value = Number(clean);
    if (!Number.isFinite(value) || value  1 || value > 100000) continue;
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) continue;
    if (rect.width  180 || rect.height > 90) continue;
    if (rect.top  0 || rect.top > innerHeight * 0.38) continue;
    let row = rows.find(r => Math.abs(r.y - rect.top)  12);
    if (!row) { row = {y: rect.top, items: []}; rows.push(row); }
    row.items.push({x: rect.left, value});
}
rows.sort((a,b) => b.items.length - a.items.length || a.y - b.y);
if (!rows.length || rows[0].items.length  3) return [];
rows[0].items.sort((a,b) => a.x - b.x);
return rows[0].items.map(i => i.value);
}
"""
    best = []
    for frame in page.frames:
        try:
            values = [float(v) for v in await frame.evaluate(script)]
            if len(values) > len(best):
                best = values
        except Exception:
            pass
    return best

async def page_issue(page):
    for frame in page.frames:
        try:
            text = " ".join((await frame.locator("body").inner_text(timeout=1000)).lower().split())
        except Exception:
            continue
        if "site is not available in your region" in text:
            return "Сайт недоступен в регионе сервера."
        if len(text) > 5000 and any(x in text for x in ("sign in", "log in", "войти")):
            return "Страница, похоже, требует авторизацию."
    return None

async def run_monitor(config: Config):
    state = await load_state(config)
    last_error_notice = 0.0
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=config.headless)
        context = await browser.new_context(viewport={"width": 430, "height": 932})
        page = await context.new_page()
        await page.goto(config.game_url, wait_until="domcontentloaded", timeout=60000)
        await send(config, "Монитор запущен. Ставки не выполняются: бот только читает завершённые результаты.")
        while True:
            try:
 current = await read_history(page, config)
                if len(current) >= config.minimum_history_items:
 issue = await page_issue(page)
 ow = time.monotonic()
                    if issue and ow - last_error_notice >= config.error_notify_cooldown:
                        await send(config, issue)
                        last_error_notice = now
                    elif not state.previous:
 state.previous = current
                        await save_state(config, state)
 log.info("initial history synchronized: %s", current)
                    elif current != state.previous:
                        for multiplier in new_results(state.previous, current):
 log.info("completed round %.2fx", multiplier)
                            if state.active:
                                if multiplier >= config.threshold:
                                    await send(
                                        config,
                                        f"Результат {multiplier:.2f}x. Наблюдаемая последовательность завершена.\n" 
                                        f"Снова жду {config.trigger_streak} результата подряд меньше {config.threshold:g}x.",
                                    )
 state.active = False
 state.streak = 0
                                else:
                                    await send(
                                        config,
                                        f"Результат {multiplier:.2f}x меньше {config.threshold:g}x.",
                                    )
                            else:
 state.streak = state.streak + 1 if multiplier > config.threshold else 0
                                if state.streak >= config.trigger_streak:
 state.active = True
 state.streak = 0
                                    await send(
                                        config,
                                        f"Наблюдено {config.trigger_streak} результата подряд меньше {config.threshold:g}x.\n" 
                                        f"Последний результат: {multiplier:.2f}x",
                                    )
 state.previous = current
                            await save_state(config, state)
                        await asyncio.sleep(config.poll_seconds)
            except Exception as exc:
 log.exception("monitor iteration failed")
 ow = time.monotonic()
                if ow - last_error_notice >= config.error_notify_cooldown:
                    await send(config, f"Ошибка мониторинга: {type(exc).__name__}. Пытаюсь продолжить.")
                    last_error_notice = now
                try:
                    if page.is_closed():
 page = await context.new_page()
                        await page.goto(config.game_url, wait_until="domcontentloaded", timeout=60000)
                except Exception:
 log.exception("page recovery failed")
                await asyncio.sleep(max(3.0, config.poll_seconds))    
