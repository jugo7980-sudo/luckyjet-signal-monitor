import asyncio

import pytest

import app_v1


def test_render_self_keepalive_uses_public_render_url_and_pings_before_idle(monkeypatch):
    calls = []
    sleeps = []

    class StopLoop(Exception):
        pass

    class FakeResponse:
        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url):
            calls.append(url)
            return FakeResponse()

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise StopLoop()

    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://example.onrender.com")
    monkeypatch.setattr(app_v1.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(app_v1.asyncio, "sleep", fake_sleep)

    async def run():
        with pytest.raises(StopLoop):
            await app_v1.render_self_keepalive(interval_seconds=240)

    asyncio.run(run())

    assert sleeps[0] == 240
    assert calls == ["https://example.onrender.com/"]
