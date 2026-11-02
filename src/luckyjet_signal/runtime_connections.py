"""Connection pools owned by one application run, inherited by its tasks."""
from contextvars import ContextVar

import httpx

_current = ContextVar('runtime_connections', default=None)


def current_connections():
    return _current.get()


class RuntimeConnections:
    def __init__(self):
        self._http = None
        self._redis = {}

    async def __aenter__(self):
        self._token = _current.set(self)
        return self

    def http(self):
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=4.0, follow_redirects=True)
        return self._http

    def redis(self, url):
        if url not in self._redis:
            import redis.asyncio
            self._redis[url] = redis.asyncio.from_url(url, decode_responses=True)
        return self._redis[url]

    async def __aexit__(self, *exc):
        try:
            if self._http is not None:
                await self._http.aclose()
        finally:
            try:
                for client in self._redis.values():
                    await client.aclose()
            finally:
                _current.reset(self._token)
