from __future__ import annotations

import logging

log = logging.getLogger("luckyjet.demomode")

_JS_CLICK = r"""
() => {
  const labels = [
    'Демо-режим',
    'Демо режим',
    'Demo mode',
    'Play demo',
    'Play for free'
  ];
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const visible = el => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && s.display !== 'none' &&
      s.visibility !== 'hidden' && Number(s.opacity) !== 0;
  };
  const nodes = Array.from(document.querySelectorAll('body *'))
    .filter(el => visible(el) && labels.includes(clean(el.innerText || el.textContent)));
  if (!nodes.length) return false;
  nodes.sort((a, b) => {
    const ar = a.getBoundingClientRect();
    const br = b.getBoundingClientRect();
    return ar.width * ar.height - br.width * br.height;
  });
  const leaf = nodes[0];
  const target = leaf.closest('button,a,[role="button"]') || leaf;
  target.click();
  return true;
}
"""


def _is_game_url(url):
    return "/casino/play/" in str(url).lower()


async def _js_click(page):
    try:
        return bool(await page.evaluate(_JS_CLICK))
    except Exception:
        return False


def wrap_click_demo_mode(delegate):
    async def wrapped(page):
        original_url = str(getattr(page, "url", ""))
        clicked = await delegate(page)

        if not clicked:
            clicked = await _js_click(page)
            if clicked:
                log.info("Demo mode clicked with JS fallback")

        if not clicked:
            return False

        try:
            await page.wait_for_timeout(1200)
        except Exception:
            pass

        current_url = str(getattr(page, "url", ""))
        if _is_game_url(original_url) and not _is_game_url(current_url):
            log.info(
                "Demo mode redirected away from game; reopening original game route"
            )
            await page.goto(
                original_url,
                wait_until="domcontentloaded",
                timeout=60000,
            )
            try:
                await page.wait_for_timeout(4000)
            except Exception:
                pass

        return True

    return wrapped
