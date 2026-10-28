"""Fast concurrent polling for Lucky Jet direct history endpoints."""
from __future__ import annotations

import asyncio
import logging

import httpx


log = logging.getLogger("luckyjet.fast_poll")


async def poll_direct_gateways_fast(monitor, capture, *, request_timeout=4.0):
    """Poll candidate direct endpoints concurrently and use the first valid history."""
    urls, headers = await capture.direct_targets()
    if not urls:
        return 0

    request_headers = dict(headers)
    request_headers.setdefault(
        "accept",
        "application/json, text/plain, */*",
    )
    request_headers.setdefault(
        "user-agent",
        (
            "Mozilla/5.0 (Linux; Android 13) "
            "AppleWebKit/537.36 Chrome/137.0 Mobile Safari/537.36"
        ),
    )

    async with httpx.AsyncClient(
        timeout=float(request_timeout),
        follow_redirects=True,
    ) as client:
        async def fetch(url):
            try:
                response = await client.get(url, headers=request_headers)
                if response.status_code >= 400:
                    return 0

                payload = monitor.parse_possible_json(response.text)
                if payload is None:
                    return 0

                count = await capture.ingest(
                    payload,
                    "direct:" + monitor.safe_url(url),
                )
                if count:
                    log.info(
                        "Fast direct endpoint returned %d completed rounds: %s",
                        count,
                        monitor.safe_url(url),
                    )
                return count
            except asyncio.CancelledError:
                raise
            except Exception:
                return 0

        tasks = [
            asyncio.create_task(fetch(url))
            for url in list(urls)[:8]
        ]

        try:
            for completed in asyncio.as_completed(tasks):
                count = await completed
                if count:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    return count
            return 0
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)


def install_fast_poll(monitor):
    """Replace the serial direct poller with the concurrent implementation once."""
    if getattr(monitor, "_luckyjet_fast_poll_installed", False):
        return

    async def poll(capture):
        return await poll_direct_gateways_fast(monitor, capture)

    monitor.poll_direct_gateways = poll
    monitor._luckyjet_fast_poll_installed = True
