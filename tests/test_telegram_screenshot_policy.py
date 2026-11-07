"""Owner may save bot content; every other Telegram recipient remains protected."""
from telegram_screenshot_policy import apply_screenshot_policy


def test_only_owner_can_save_bot_messages_and_diagnostic_photos():
    for method in ("sendMessage", "sendPhoto", "sendDocument", "sendMediaGroup"):
        owner = apply_screenshot_policy(
            method, {"chat_id": 42, "text": "status", "protect_content": True}, 42
        )
        other = apply_screenshot_policy(
            method, {"chat_id": 43, "text": "status"}, 42
        )
        assert owner["chat_id"] == 42 and "protect_content" not in owner
        assert other["chat_id"] == 43 and other["protect_content"] is True


def test_owner_missing_or_group_recipient_fails_closed():
    assert apply_screenshot_policy("sendMessage", {"chat_id": 42}, None)["protect_content"] is True
    assert apply_screenshot_policy("sendMessage", {"chat_id": -10042}, 42)["protect_content"] is True
    assert apply_screenshot_policy("sendPhoto", {"chat_id": "@group"}, 42)["protect_content"] is True


def test_edit_callback_and_file_payloads_are_not_broken():
    payload = {"chat_id": 42, "message_id": 9, "text": "status"}
    assert apply_screenshot_policy("editMessageText", payload, 42) is payload
    image = ("diagnostic.png", b"fixture", "image/png")
    photo_payload = {"chat_id": 42, "photo_file": image, "protect_content": True}
    applied = apply_screenshot_policy("sendPhoto", photo_payload, 42)
    assert applied["photo_file"] == image
    assert "protect_content" not in applied
    assert photo_payload["protect_content"] is True
