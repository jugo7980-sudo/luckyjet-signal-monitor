from __future__ import annotations

import logging
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger("luckyjet.netdiag")


def _safe_url(url):
    try:
        p = urlsplit(str(url))
        return urlunsplit((p.scheme, p.netloc, p.path, "", ""))
    except Exception:
        return "<invalid-url>"


def wrap_attach_network_capture(delegate, *, max_events=40):
    def wrapped(page, capture):
        delegate(page, capture)
        seen = 0

        def on_response(response):
            nonlocal seen
            if seen >= max_events:
                return
            try:
                resource_type = response.request.resource_type
                if resource_type not in ("xhr", "fetch", "document"):
                    return
                seen += 1
                log.info(
                    "Network response %d/%d type=%s status=%s url=%s",
                    seen,
                    max_events,
                    resource_type,
                    response.status,
                    _safe_url(response.url),
                )
            except Exception:
                return

        page.on("response", on_response)

    return wrapped
