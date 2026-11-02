import asyncio
from types import SimpleNamespace
import luckyjet_fast_poll as poller


def test_primary_reused_and_failure_switches_to_backup(monkeypatch):
    calls = []
    failing = set()
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, url, **kwargs):
            calls.append(url)
            return SimpleNamespace(status_code=500 if url in failing else 200, text='{}')
    class Capture:
        async def direct_targets(self): return ['https://a', 'https://b'], {}
        async def ingest(self, *args): return 20
    monitor = SimpleNamespace(parse_possible_json=lambda text: {}, safe_url=lambda url: url)
    monkeypatch.setattr(poller.httpx, 'AsyncClient', lambda **kw: Client())
    async def run():
        capture = Capture()
        await poller.poll_direct_gateways_fast(monitor, capture)
        calls.clear()
        await poller.poll_direct_gateways_fast(monitor, capture)
        assert calls == [capture._preferred_direct_url]
        failing.add(capture._preferred_direct_url)
        calls.clear()
        assert await poller.poll_direct_gateways_fast(monitor, capture) == 20
        assert len(calls) == 2
        assert capture._preferred_direct_url not in failing
    asyncio.run(run())
