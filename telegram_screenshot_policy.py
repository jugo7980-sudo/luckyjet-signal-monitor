"""Restrict Telegram message saving for everyone except the verified owner.

Applied only to new Bot API messages. Existing protected messages cannot be
unprotected via editMessageText, and Telegram client policies may still apply.
"""
from __future__ import annotations

_PROTECTABLE = frozenset({
    "sendMessage", "sendPhoto", "sendDocument", "sendVideo",
    "sendAnimation", "sendAudio", "sendVoice", "sendVideoNote",
    "sendSticker", "sendMediaGroup", "copyMessage",
})


def apply_screenshot_policy(method, payload, owner_chat_id):
    """Preserve owner screenshot ability; protect every other recipient."""
    if method not in _PROTECTABLE:
        return payload

    try:
        owner = int(owner_chat_id)
        recipient = int(payload.get("chat_id"))
    except (ValueError, TypeError, AttributeError):
        owner = recipient = None

    result = dict(payload)
    if owner is not None and owner > 0 and owner == recipient:
        # The Bot API defaults to unprotected. Omission also avoids a form-data
        # false literal when uploading files with multipart encoding.
        result.pop("protect_content", None)
    else:
        result["protect_content"] = True
    return result
