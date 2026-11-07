"""One-off isolated VLESS/REALITY egress diagnostics; never logs node credentials."""
import asyncio
import json
import os
import tempfile
from pathlib import Path

import httpx

from xray_runtime import parse_node_link, make_config

PROXY = "socks5://127.0.0.1:10808"
CHECKS = (("https://api.country.is/", "country"), ("https://ipwho.is/", "country_code"))


async def test_one(name, node):
    if not node:
        print(json.dumps({"node": name, "status": "NOT_CONFIGURED"}), flush=True)
        return
    process = None
    try:
        outgoing = parse_node_link(node)
        # Configuration including the credentials is only written into this
        # short-lived container, permissions 0600, never committed or logged.
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory, "xray.json")
            file.write_text(json.dumps(make_config(outgoing)))
            file.chmod(0o600)
            process = await asyncio.create_subprocess_exec(
                "/usr/local/bin/xray", "run", "-c", str(file),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            ready = False
            for _ in range(50):
                if process.returncode is not None:
                    break
                try:
                    reader, writer = await asyncio.open_connection("127.0.0.1", 10808)
                    writer.close()
                    await writer.wait_closed()
                    ready = True
                    break
                except OSError:
                    await asyncio.sleep(0.1)
            if not ready:
                print(json.dumps({"node":name, "status":"SOCKS_NOT_READY"}), flush=True)
                return
            async with httpx.AsyncClient(proxy=PROXY, trust_env=False, timeout=12) as client:
                for url, field in CHECKS:
                    try:
                        response = await client.get(url)
                        response.raise_for_status()
                        payload = response.json()
                        address = payload.get("ip")
                        country = payload.get(field)
                        if (not isinstance(address, str)
                                or not isinstance(country, str)):
                            raise ValueError("bad response")
                        # No node URL, credential, cookies or HTTP headers printed.
                        print(json.dumps({"node":name, "check":url.split("/")[2],
                                          "status":"OK", "ip":address,
                                          "country":country.upper()}), flush=True)
                    except (httpx.HTTPError, ValueError, TypeError):
                        print(json.dumps({"node":name, "check":url.split("/")[2],
                                          "status":"REQUEST_FAILED"}), flush=True)
    except (ValueError, TypeError, OSError):
        print(json.dumps({"node":name, "status":"NODE_OR_PROCESS_ERROR"}), flush=True)
    finally:
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=4)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()


async def run():
    print(json.dumps({"status":"START", "purpose":"isolated_happ_exit_probe"}), flush=True)
    # Use one container; connections tested sequentially with fresh Xray process.
    await test_one("primary", os.getenv("HAPP_PRIMARY_URL", ""))
    await test_one("fallback", os.getenv("HAPP_FALLBACK_URL", ""))
    print(json.dumps({"status":"DONE"}), flush=True)


asyncio.run(run())
