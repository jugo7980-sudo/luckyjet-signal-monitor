import asyncio
from types import SimpleNamespace

import pytest

from luckyjet_signal.runtime_connections import RuntimeConnections, current_connections
from luckyjet_signal import monitor


def test_connections_reused_for_state_load_and_save_and_closed(monkeypatch):
    for key in ("ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "all_proxy", "https_proxy", "http_proxy"):
        monkeypatch.delenv(key, raising=False)
    clients = []

    class Redis:
        closed = False
        def __init__(self):
            self.raw = None
            clients.append(self)
        async def get(self, key):
            return self.raw
        async def set(self, key, value):
            self.raw = value
        async def aclose(self):
            self.closed = True

    import redis.asyncio
    monkeypatch.setenv('REDIS_URL', 'redis://test')
    monkeypatch.setattr(redis.asyncio, 'from_url', lambda *a, **kw: Redis())

    async def run():
        config = SimpleNamespace(base_amount=150)
        with pytest.raises(RuntimeError):
            async with RuntimeConnections() as resources:
                assert current_connections() is resources
                first = resources.http()
                assert resources.http() is first
                state = monitor.State(previous=['1.20x'])
                await monitor.save_state(config, state)
                await monitor.save_state(config, state)
                restored = await monitor.load_state(config)
                assert restored.previous == ['1.20x']
                assert len(clients) == 1
                assert not clients[0].closed
                raise RuntimeError('shutdown')
        assert first.is_closed
        assert clients[0].closed
        assert current_connections() is None

    asyncio.run(run())


def test_direct_polls_share_client_and_preserve_request_timeout(monkeypatch):
    import httpx
    from luckyjet_fast_poll import poll_direct_gateways_fast
    clients = []
    requests = []
    original = httpx.AsyncClient

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={'rounds': [1.2]})

    def client(**kwargs):
        instance = original(transport=httpx.MockTransport(respond), trust_env=False, **kwargs)
        clients.append(instance)
        return instance

    monkeypatch.setattr(httpx, 'AsyncClient', client)

    class Capture:
        async def direct_targets(self):
            return ['https://example.test/history'], {'authorization': 'test'}
        async def ingest(self, payload, source):
            return len(payload['rounds'])

    async def run():
        async with RuntimeConnections():
            for _ in range(2):
                assert await poll_direct_gateways_fast(monitor, Capture(), request_timeout=2.0) == 1
            assert len(clients) == 1
            assert not clients[0].is_closed
        assert clients[0].is_closed
        assert len(requests) == 2
        assert requests[0].extensions['timeout']['read'] == 2.0
    asyncio.run(run())
