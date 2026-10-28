import asyncio
import importlib
import importlib.util

from luckyjet_signal import direct_only


def test_fast_poll_module_exists_and_does_not_wait_for_slow_first_url(monkeypatch):
    spec = importlib.util.find_spec("luckyjet_fast_poll")
    assert spec is not None, "fast direct polling module is missing"
    fast_poll = importlib.import_module("luckyjet_fast_poll")

    class FakeResponse:
        status_code = 200
        text = '{"ok": true}'

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers=None):
            if url.endswith("/slow"):
                await asyncio.sleep(60)
            return FakeResponse()

    class Capture:
        async def direct_targets(self):
            return ["https://example/slow", "https://example/fast"], {}

        async def ingest(self, payload, source, event_name=""):
            return 1 if source.endswith("/fast") else 0

    class Monitor:
        @staticmethod
        def parse_possible_json(text):
            return {"ok": True}

        @staticmethod
        def safe_url(url):
            return url

    monkeypatch.setattr(fast_poll.httpx, "AsyncClient", FakeClient)

    async def run():
        return await asyncio.wait_for(
            fast_poll.poll_direct_gateways_fast(Monitor, Capture()),
            timeout=1,
        )

    assert asyncio.run(run()) == 1


def test_stale_successful_direct_history_forces_browser_reopen():
    assert hasattr(direct_only, "stale_direct_history")

    stale_since, reopen = direct_only.stale_direct_history(
        direct_only_mode=True,
        worked=True,
        changed=False,
        stale_since=None,
        now=100.0,
        timeout_seconds=90.0,
    )
    assert stale_since == 100.0
    assert reopen is False

    stale_since, reopen = direct_only.stale_direct_history(
        direct_only_mode=True,
        worked=True,
        changed=False,
        stale_since=stale_since,
        now=191.0,
        timeout_seconds=90.0,
    )
    assert reopen is True

    stale_since, reopen = direct_only.stale_direct_history(
        direct_only_mode=True,
        worked=True,
        changed=True,
        stale_since=100.0,
        now=200.0,
        timeout_seconds=90.0,
    )
    assert stale_since is None
    assert reopen is False
