"""Standalone, read-only Lucky Jet round collector.

This process never launches Telegram polling, site-account login, DEMO balance
calculation, or any data reset. It only archives completed source rounds.

Run in two distinct Railway services with:
  python collector_worker.py
  REDIS_URL, GAME_URL, COLLECTOR_NAME=collector-a|collector-b
Optional: POLL_SECONDS=0.5 and the same read-only game egress config as main.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from types import SimpleNamespace

from redis.asyncio import from_url
from playwright.async_api import async_playwright

from round_archive import RoundArchive
from luckyjet_signal import monitor
from xray_runtime import XrayManager

log = logging.getLogger("luckyjet.collector")


async def capture_source(playwright, config, archive, *, poll_seconds=0.5):
    """Keep polling the discovered direct history independently of UI/login."""
    capture = monitor.LiveCapture()
    browser = None
    context = None
    try:
        # Prefer the same completed-history gateway independently verified by
        # the production monitor. The direct API may not require a browser.
        gateway = os.getenv(
            "DIRECT_HISTORY_URL",
            "https://crash-gateway-grm-cr.gamedev-tech.cc/history",
        ).strip()
        if gateway.startswith("https://"):
            capture._candidate_urls.add(gateway)
        initial = await monitor.poll_direct_gateways(capture)
        healthy_direct = 1 if initial else 0
        if initial:
            rounds, _ = await capture.snapshot()
            await archive.append_snapshot(rounds)
            log.info("Collector initialized from direct completed-round endpoint")
        else:
            # Bootstrap in an isolated browser only if direct access failed.
            # Owner login and bot/browser sessions are never accessed here.
            opts = {"headless": True}
            if monitor.game_proxy:
                opts["proxy"] = {"server": monitor.game_proxy}
            browser = await playwright.chromium.launch(**opts)
            context = await browser.new_context(
                viewport={"width": 430, "height": 932}, locale="ru-RU",
            )
            page = await monitor.open_game(context, config, capture)
            if page and not page.is_closed():
                rounds, _ = await capture.snapshot()
                if rounds and all(r.get("id") for r in rounds):
                    await archive.append_snapshot(rounds)
        last_good = time.monotonic()
        last_report = 0.0
        while True:
            began = time.monotonic()
            count = await monitor.poll_direct_gateways(capture)
            rounds, source = await capture.snapshot()
            direct = bool(count and rounds and source.startswith("direct:"))
            if direct:
                outcome = await archive.append_snapshot(rounds)
                last_good = time.monotonic()
                healthy_direct += 1
                if outcome["gap"]:
                    log.warning("Source history discontinuity, archived a gap marker")
                if healthy_direct >= 3 and browser is not None:
                    await context.close()
                    await browser.close()
                    context, browser = None, None
                    log.info("Collector direct API stable; browser released")
            else:
                healthy_direct = 0
                if time.monotonic() - last_good >= 60:
                    raise RuntimeError("Source history unavailable for >=60s")
            if time.monotonic() - last_report >= 60:
                log.info(
                    "Collector alive: source=%s rounds=%d direct=%s",
                    "direct" if direct else "missing", len(rounds), direct,
                )
                last_report = time.monotonic()
            await asyncio.sleep(max(0.05, poll_seconds - (time.monotonic() - began)))
    finally:
        if context:
            try:
                await context.close()
            except Exception:
                pass
        if browser:
            try:
                await browser.close()
            except Exception:
                pass


async def main_async():
    redis_url = os.getenv("REDIS_URL", "").strip()
    game_url = os.getenv("GAME_URL", "").strip()
    name = os.getenv("COLLECTOR_NAME", "").strip()
    if not redis_url or not game_url or not name:
        raise RuntimeError("REDIS_URL, GAME_URL and COLLECTOR_NAME are required")
    seconds = max(0.2, float(os.getenv("POLL_SECONDS", "0.5")))
    redis = from_url(redis_url, decode_responses=True)
    archive = RoundArchive(redis, name)
    try:
        await redis.ping()
        async with XrayManager() as xray:
            await xray.start_from_env()
            monitor.game_proxy = xray.proxy_url
            try:
                async with async_playwright() as playwright:
                    while True:
                        try:
                            await capture_source(
                                playwright, SimpleNamespace(game_url=game_url),
                                archive, poll_seconds=seconds,
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            log.exception("Collector restart after source failure")
                            await archive.heartbeat_error(type(exc).__name__)
                            await asyncio.sleep(3.0)
            finally:
                monitor.game_proxy = None
    finally:
        await redis.aclose()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
