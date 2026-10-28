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


_DOM_HINT_SCRIPT = r"""
() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const iframes = Array.from(document.querySelectorAll('iframe')).slice(0, 8).map(el => ({
    src: el.getAttribute('src') || '',
    dataSrc: el.getAttribute('data-src') || '',
    id: el.id || '',
    name: el.getAttribute('name') || '',
    title: el.getAttribute('title') || ''
  }));

  const demo = [];
  for (const el of Array.from(document.querySelectorAll('body *'))) {
    const text = clean(el.innerText || el.textContent);
    if (!text || !/демо-режим|демо режим|demo mode|play demo/i.test(text)) continue;
    const clickable = el.closest('a,button,[role="button"]') || el;
    demo.push({
      tag: clickable.tagName || '',
      text: clean(clickable.innerText || clickable.textContent).slice(0, 100),
      href: clickable.href || clickable.getAttribute?.('href') || '',
      role: clickable.getAttribute?.('role') || ''
    });
    if (demo.length >= 8) break;
  }
  return {iframes, demo};
}
"""


async def collect_dom_hints(page):
    raw = await page.evaluate(_DOM_HINT_SCRIPT)
    result = {"iframes": [], "demo": []}

    for item in list((raw or {}).get("iframes") or [])[:8]:
        result["iframes"].append(
            {
                "src": _safe_url(item.get("src", "")) if item.get("src") else "",
                "dataSrc": _safe_url(item.get("dataSrc", "")) if item.get("dataSrc") else "",
                "id": str(item.get("id", ""))[:80],
                "name": str(item.get("name", ""))[:80],
                "title": str(item.get("title", ""))[:100],
            }
        )

    for item in list((raw or {}).get("demo") or [])[:8]:
        result["demo"].append(
            {
                "tag": str(item.get("tag", ""))[:20],
                "text": str(item.get("text", ""))[:100],
                "href": _safe_url(item.get("href", "")) if item.get("href") else "",
                "role": str(item.get("role", ""))[:40],
            }
        )

    return result


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

        try:
            hints = await collect_dom_hints(page)
            log.info(
                "Browser DOM hints: iframes=%s demo=%s",
                hints["iframes"],
                hints["demo"],
            )
        except Exception as exc:
            log.info(
                "Browser DOM hints unavailable: %s",
                type(exc).__name__,
            )

        return await delegate(page, capture)

    return wrapped
