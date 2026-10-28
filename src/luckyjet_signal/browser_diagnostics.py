from __future__ import annotations

import logging
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger("luckyjet.browserdiag")


def _safe_url(url):
    try:
        parts = urlsplit(str(url))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except Exception:
        return "<invalid-url>"


def summarize_targets(page_urls, frame_urls):
    return {
        "pages": [_safe_url(url) for url in list(page_urls)[:8]],
        "frames": [_safe_url(url) for url in list(frame_urls)[:12]],
    }


def wrap_diagnose_page(delegate):
    async def wrapped(page, capture):
        try:
            pages = [p.url for p in list(page.context.pages)]
        except Exception:
            pages = ["<pages-error>"]

        try:
            frames = [f.url for f in list(page.frames)]
        except Exception:
            frames = ["<frames-error>"]

        summary = summarize_targets(pages, frames)
        log.info(
            "Browser targets: pages=%s frames=%s",
            summary["pages"],
            summary["frames"],
        )
        return await delegate(page, capture)

    return wrapped
