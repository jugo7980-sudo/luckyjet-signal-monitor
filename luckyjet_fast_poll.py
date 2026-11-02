"""Fast concurrent polling for Lucky Jet direct history endpoints."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from luckyjet_signal.runtime_connections import current_connections

import httpx


log = logging.getLogger("luckyjet.fast_poll")


@asynccontextmanager
async def polling_client(request_timeout):
    resources = current_connections()
    if resources is not None:
        yield resources.http()
    else:
        async with httpx.AsyncClient(
            timeout=float(request_timeout), follow_redirects=True,
        ) as client:
            yield client


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

    async with polling_client(request_timeout) as client:
        async def fetch(url):
            try:
                options = {"timeout": float(request_timeout)} if current_connections() is not None else {}
                response = await client.get(url, headers=request_headers, **options)
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
                    capture._preferred_direct_url = url
                    log.debug(
                        "Fast direct endpoint returned %d completed rounds: %s",
                        count,
                        monitor.safe_url(url),
                    )
                return count
            except asyncio.CancelledError:
                raise
            except Exception:
                return 0

        candidates = list(urls)[:8]
        preferred = getattr(capture, "_preferred_direct_url", None)
        tasks = []
        try:
            if preferred in candidates:
                primary = asyncio.create_task(fetch(preferred))
                tasks.append(primary)
                # Hedge slow responses instead of waiting for the full timeout.
                done, _ = await asyncio.wait([primary], timeout=0.25)
                if done and primary.result():
                    return primary.result()
                candidates.remove(preferred)
            tasks.extend(asyncio.create_task(fetch(url)) for url in candidates)

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


class PollSummary:
    """Summarize routine success, but report outages and recovery immediately."""

    def __init__(self):
        self.last_report = None
        self.success = 0
        self.failed = 0
        self.unavailable = False

    def record(self, count, *, now=None):
        now = time.monotonic() if now is None else now
        if count:
            self.success += 1
            if self.unavailable:
                log.info("Direct history source recovered")
            self.unavailable = False
        else:
            self.failed += 1
            if not self.unavailable:
                log.warning("Direct history poll returned no valid history")
            self.unavailable = True
        if self.last_report is None or now - self.last_report >= 60:
            log.info("Direct history polling: success=%d failed=%d latest_rounds=%d",
                     self.success, self.failed, count)
            self.success = self.failed = 0
            self.last_report = now


def install_fast_poll(monitor):
    """Replace the serial direct poller with the concurrent implementation once."""
    if getattr(monitor, "_luckyjet_fast_poll_installed", False):
        return

    summary = PollSummary()

    async def poll(capture):
        count = await poll_direct_gateways_fast(monitor, capture)
        summary.record(count)
        return count

    monitor.poll_direct_gateways = poll
    monitor._luckyjet_fast_poll_installed = True
