import asyncio

import app_v1


class FakeRequest:
    def __init__(self):
        self.calls = []

    async def __call__(self, method, payload):
        self.calls.append((method, payload))
        if method == "getMe":
            return {"ok": True, "result": {"username": "test_bot", "id": 123}}
        if method == "getWebhookInfo":
            return {
                "ok": True,
                "result": {
                    "url": "https://example.com/hook",
                    "pending_update_count": 7,
                },
            }
        if method == "deleteWebhook":
            return {"ok": True, "result": True}
        raise AssertionError(method)


def test_preflight_removes_webhook_before_long_polling():
    request = FakeRequest()

    asyncio.run(app_v1.prepare_telegram_polling(request))

    methods = [method for method, _ in request.calls]
    assert methods == ["getMe", "getWebhookInfo", "deleteWebhook"]
    assert request.calls[-1][1] == {"drop_pending_updates": False}
