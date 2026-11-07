"""Probe Happ VLESS nodes with temporary private Xray SOCKS (owner only).

Does not access 1win, alter active Xray service, cookies, Redis, or demo data.
Never logs credentials, proxy URI, HTTP headers, or request bodies.
"""
import asyncio
import json
import os
import tempfile
import time
from ipaddress import ip_address
from pathlib import Path

import httpx

from xray_runtime import make_config, parse_node_link

PORT = 10818  # Separate from the monitor's permanent 10808 SOCKS listener.
_SOCKS = f"socks5://127.0.0.1:{PORT}"
_CACHE_SECONDS = 600
_LAST = None
_LAST_AT = 0.0
_LOCK = asyncio.Lock()


async def _probe_node(name, node_uri):
    if not node_uri:
        return {"node": name, "status": "not_configured"}
    process = None
    try:
        outbound = parse_node_link(node_uri)
        config = make_config(outbound)
        config["inbounds"][0]["port"] = PORT
        with tempfile.TemporaryDirectory(prefix="happ-probe-") as directory:
            file = Path(directory) / "config.json"
            file.write_text(json.dumps(config), encoding="utf-8")
            file.chmod(0o600)
            process = await asyncio.create_subprocess_exec(
                "/usr/local/bin/xray", "run", "-c", str(file),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            ready = False
            for _ in range(45):
                if process.returncode is not None:
                    break
                try:
                    reader, writer = await asyncio.open_connection("127.0.0.1", PORT)
                    writer.close()
                    await writer.wait_closed()
                    ready = True
                    break
                except OSError:
                    await asyncio.sleep(0.1)
            if not ready:
                return {"node": name, "status": "socks_not_ready"}
            sources = (("https://api.country.is/", "country"),
                       ("https://ipwho.is/", "country_code"))
            failures = 0
            async with httpx.AsyncClient(proxy=_SOCKS, trust_env=False, timeout=8) as client:
                for url, key in sources:
                    try:
                        response = await client.get(url)
                        response.raise_for_status()
                        body = response.json()
                        address = ip_address(body.get("ip", ""))
                        country = body.get(key, "")
                        if not address.is_global or not isinstance(country, str) or len(country) != 2:
                            raise ValueError("Unexpected response")
                        return {"node": name, "status": "ok", "ip": str(address),
                                "country": country.upper()}
                    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                        failures += 1
            return {"node": name, "status": "egress_check_failed", "attempts": failures}
    except (ValueError, TypeError, OSError):
        return {"node": name, "status": "node_or_runtime_error"}
    finally:
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=4)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()


async def probe_happ_egress():
    """Sequential, cached checks. No direct fallback and no production port changes."""
    global _LAST, _LAST_AT
    async with _LOCK:
        if _LAST is not None and time.monotonic() - _LAST_AT < _CACHE_SECONDS:
            return {"cached": True, "nodes": _LAST}
        nodes = []
        for name, env in (
            ("primary", "HAPP_PRIMARY_URL"),
            ("fallback", "HAPP_FALLBACK_URL"),
        ):
            nodes.append(await _probe_node(name, os.getenv(env, "")))
        _LAST = nodes
        _LAST_AT = time.monotonic()
        return {"cached": False, "nodes": nodes}
