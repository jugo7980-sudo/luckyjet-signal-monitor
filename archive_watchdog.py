"""Dual-collector archive watchdog with Redis election and deduplicated alerts.

Runs on both read-only collectors; Redis lease grants one leader at a time.
If a collector exits, the other takes over. The watchdog never changes DEMO
state, coefficient history, or source round order.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx

from round_archive import HEALTH_KEY, COLLECTOR_HEALTH_PREFIX

log = logging.getLogger("luckyjet.archive_watchdog")
WATCHDOG_STATE = "luckyjet:archive:v1:watchdog:state"
WATCHDOG_LEASE = "luckyjet:archive:v1:watchdog:lease"
ARCHIVE_STALE_MS = 180_000
COLLECTOR_STALE_MS = 150_000


def assess_health(archive: dict, primary: dict, backup: dict, *, now_ms: int):
    """Return severity and a short factual alert from observed Redis state."""
    def age(source, key):
        try:
            stamp = int(source.get(key) or 0)
        except (ValueError, TypeError):
            stamp = 0
        return now_ms - stamp if stamp else None

    archive_age = age(archive, "last_round_at")
    pri_age = age(primary, "last_ok_at")
    bak_age = age(backup, "last_ok_at")
    if archive_age is None or archive_age > ARCHIVE_STALE_MS:
        return "no_new_rounds", "Нет новых коэффициентов в архиве более 3 минут. Проверьте источник и оба сборщика."
    stale = [label for label, delta in (
        ("основной", pri_age), ("резервный", bak_age)
    ) if delta is None or delta > COLLECTOR_STALE_MS]
    if len(stale) == 2:
        return "both_collectors_stale", "Оба сборщика не подтверждали работу более 2,5 минут."
    if stale:
        return "collector_stale", (
            "Не отвечает " + stale[0] + " сборщик. Второй продолжает запись."
        )
    return "healthy", ""


async def check_once(redis, collector_name, sender, *, now_ms=None):
    """One elected collector notifies only on deterioration, recovery or new gap."""
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    if not await redis.set(WATCHDOG_LEASE, collector_name, nx=True, ex=8):
        return "not_leader"
    archive, primary, backup, prior = await asyncio.gather(
        redis.hgetall(HEALTH_KEY),
        redis.hgetall(COLLECTOR_HEALTH_PREFIX + "primary"),
        redis.hgetall(COLLECTOR_HEALTH_PREFIX + "backup"),
        redis.hgetall(WATCHDOG_STATE),
    )
    severity, message = assess_health(archive, primary, backup, now_ms=now)
    gaps = int(archive.get("gaps") or 0)
    if not prior:
        # Cold start: record the baseline. Do not send alerts about old gaps.
        await redis.hset(WATCHDOG_STATE, mapping={
            "status": severity, "last_gap_count": gaps, "updated_at": now,
        })
        return "initialized"

    previous_status = prior.get("status") or "healthy"
    previous_gaps = int(prior.get("last_gap_count") or 0)
    new_gap = gaps > previous_gaps
    changed = severity != previous_status
    if not changed and not new_gap:
        return "unchanged"

    if severity == "healthy":
        text = "LuckyJet: оба сборщика работают, новые коэффициенты поступают."
        if new_gap:
            text = "LuckyJet: в архиве обнаружен разрыв истории. Проверяйте полноту DEMO."
    else:
        text = "LuckyJet: " + message
        if new_gap:
            text += " Также обнаружен разрыв истории."
    try:
        delivered = bool(await sender(text))
    except Exception:
        log.warning("Archive warning delivery failed")
        delivered = False
    if not delivered:
        return "delivery_failed"
    await redis.hset(WATCHDOG_STATE, mapping={
        "status": severity, "last_gap_count": gaps, "updated_at": now,
    })
    log.warning("Archive health state changed: status=%s gap_alert=%s", severity, new_gap)
    return "notified"


async def send_telegram(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    admin = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not admin:
        log.warning("Archive watchdog cannot notify: Telegram credentials missing")
        return False
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            result = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": admin, "text": "⚠️ " + text},
            )
            return result.status_code == 200 and bool(result.json().get("ok"))
    except (httpx.HTTPError, ValueError):
        log.warning("Archive watchdog Telegram request failed")
        return False


async def watchdog_loop(redis, collector_name):
    """Runs separately from capture; a Telegram failure cannot stop polling."""
    while True:
        try:
            await check_once(redis, collector_name, send_telegram)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Archive watchdog cycle failed; polling unaffected")
        await asyncio.sleep(5)
