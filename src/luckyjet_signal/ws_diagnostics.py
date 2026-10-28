from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

log = logging.getLogger("luckyjet.wsdiag")

_SENSITIVE_KEYS = (
    "token",
    "auth",
    "cookie",
    "session",
    "customer",
    "email",
    "password",
)


def _redact(value: Any, key: str = "", depth: int = 0) -> Any:
    key_l = key.lower()
    if any(part in key_l for part in _SENSITIVE_KEYS):
        return "<redacted>"

    if depth >= 5:
        return f"<{type(value).__name__}>"

    if isinstance(value, dict):
        result = {}
        for index, (child_key, child_value) in enumerate(value.items()):
            if index >= 30:
                result["<more>"] = len(value) - 30
                break
            result[str(child_key)] = _redact(
                child_value,
                key=str(child_key),
                depth=depth + 1,
            )
        return result

    if isinstance(value, list):
        return [
            _redact(item, depth=depth + 1)
            for item in value[:8]
        ] + ([f"<{len(value) - 8} more>"] if len(value) > 8 else [])

    if isinstance(value, str):
        if len(value) <= 80:
            return value
        return f"<str:{len(value)}>"

    if isinstance(value, (int, float, bool)) or value is None:
        return value

    return f"<{type(value).__name__}>"


def _parse_socketio_payload(payload: Any) -> Any:
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="ignore")

    if not isinstance(payload, str):
        return payload

    text = payload.strip()
    starts = [pos for pos in (text.find("["), text.find("{")) if pos >= 0]
    if not starts:
        return {"raw_type": "str", "length": len(text)}

    candidate = text[min(starts):]
    try:
        return json.loads(candidate)
    except Exception:
        return {
            "raw_type": "str",
            "length": len(text),
            "prefix": text[:80],
        }


def summarize_ws_payload(payload: Any) -> str:
    parsed = _parse_socketio_payload(payload)
    safe = _redact(parsed)
    return json.dumps(
        safe,
        ensure_ascii=False,
        sort_keys=True,
        separators=(", ", ": "),
    )


def wrap_ws_handler(
    delegate: Callable[..., Awaitable[Any]],
    *,
    max_samples: int = 8,
):
    seen = 0

    async def wrapped(payload, capture, source):
        nonlocal seen
        if seen < max_samples:
            seen += 1
            log.info(
                "WS frame sample %d/%d source=%s payload=%s",
                seen,
                max_samples,
                source,
                summarize_ws_payload(payload),
            )
        return await delegate(payload, capture, source)

    return wrapped
