"""Regression: the real HTTP server must dispatch the archive summary to its own API.

This test exercises TCP -> health_server -> effective_auth -> summary -> Redis Stream.
A previous release incorrectly routed the valid summary URL to overview's
20/30/50/100-tile-only handler and returned HTTP 400.
"""
import asyncio
import hashlib
import hmac
import json
import socket
import time
from unittest.mock import patch
from urllib.parse import urlencode

import fakeredis.aioredis

from main import health_server
from round_archive import STREAM_KEY


def _signed():
    fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": 42})}
    message = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    key = hmac.new(b"WebAppData", b"fixture", hashlib.sha256).digest()
    fields["hash"] = hmac.new(key, message.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_summary_route_reaches_real_archive_api_without_overview_limits():
    async def run():
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        for i, value in enumerate([1.3, 151.0, 1.2, 300.0]):
            await redis.xadd(STREAM_KEY, {
                "type": "round", "round_id": f"test-round-{i}",
                "coefficient": str(value), "observed_at": str(int(time.time() * 1000)),
                "source_time": "2026-11-08T08:00:00+10:00",
            })

        class Gate:
            blocked = set()

            def __init__(self, redis):
                self.redis = redis

            async def is_authorized(self, uid):
                return uid == 42

            def is_admin(self, uid):
                return False

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        with patch.dict("os.environ", {"PORT": str(port)}):
            task = asyncio.create_task(health_server(Gate(redis), "fixture"))
            try:
                for _ in range(100):
                    try:
                        reader, writer = await asyncio.open_connection("127.0.0.1", port)
                        break
                    except OSError:
                        await asyncio.sleep(0.02)
                else:
                    raise AssertionError("HTTP server did not bind")

                request = (
                    "GET /api/round-summary?rounds=4&target=150&since=100 HTTP/1.1\r\n"
                    "Host: localhost\r\n"
                    "Authorization: tma " + _signed() + "\r\n"
                    "Connection: close\r\n\r\n"
                )
                writer.write(request.encode("ascii"))
                await writer.drain()
                payload = await asyncio.wait_for(reader.read(), timeout=5)
                writer.close()
                await writer.wait_closed()
                head, body = payload.split(b"\r\n\r\n", 1)
                assert b"200 OK" in head, payload.decode("utf-8", "replace")
                result = json.loads(body)
                assert result["source"] == "append_only_round_archive"
                assert result["rounds_requested"] == 4
                assert result["rounds_available"] == 4
                assert result["current"]["count"] == 2
                assert result["rounds_since_target"] == 0
                assert result["quality"]["complete_window"] is True

                # Query validation must come from the archive API, not overview.
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                request = (
                    "GET /api/round-summary?rounds=0&target=150&since=100 HTTP/1.1\r\n"
                    "Host: localhost\r\n"
                    "Authorization: tma " + _signed() + "\r\n"
                    "Connection: close\r\n\r\n"
                )
                writer.write(request.encode("ascii"))
                await writer.drain()
                payload = await asyncio.wait_for(reader.read(), timeout=5)
                writer.close()
                await writer.wait_closed()
                head, body = payload.split(b"\r\n\r\n", 1)
                assert b"400 Bad Request" in head
                message = json.loads(body)["error"]
                assert "50000" in message and "20, 30" not in message
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await redis.aclose()

    asyncio.run(run())
