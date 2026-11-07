"""Optional, fail-safe Xray egress for Lucky Jet game traffic only.

The official Xray binary is built into the container. A Railway secret
(XRAY_SUBSCRIPTION_URL or XRAY_NODE_URL) supplies a compatible VLESS/Trojan
node. If the subscription is an HTML landing page, Xray is NOT enabled.
No subscription URL, node credentials, or config JSON is ever logged.
Telegram, Redis, health checks, and Mini App traffic remain direct.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import tempfile
import uuid
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

log = logging.getLogger("luckyjet.xray")
_NODE_RE = re.compile(r"(?:^|\s)(vless|trojan)://", re.IGNORECASE)
_MAX_BYTES = 1_000_000
_USER_AGENTS = (
    "v2rayNG/1.10.14",
    "Happ/1.0",
)
_SOCKS_HOST = "127.0.0.1"
_SOCKS_PORT = 10808


class XrayConfigError(ValueError):
    """Unsupported/invalid node or a subscription landing page."""


def _first(query, key, default=""):
    return query.get(key, [default])[0]


def parse_node_link(link: str) -> dict:
    """Convert supported URI links into an Xray outbound (no credentials logged)."""
    url = urlsplit(link.strip())
    scheme = url.scheme.lower()
    if scheme not in {"vless", "trojan"}:
        raise XrayConfigError("Unsupported proxy protocol")
    host, port = url.hostname, url.port
    if not host or not port or not 1 <= port <= 65535:
        raise XrayConfigError("Missing upstream host or port")
    query = parse_qs(url.query, keep_blank_values=True)
    transport = _first(query, "type", "tcp").lower()
    if transport not in {"tcp", "ws", "grpc"}:
        raise XrayConfigError("Unsupported transport type")
    security = _first(query, "security", "tls" if scheme == "trojan" else "none").lower()
    if security not in {"tls", "reality"}:
        raise XrayConfigError("Secure TLS/REALITY transport required")
    server_name = _first(query, "sni", host)
    stream = {"network": transport, "security": security}
    if transport == "ws":
        ws = {"path": _first(query, "path", "/")}
        if _first(query, "host"):
            ws["headers"] = {"Host": _first(query, "host")}
        stream["wsSettings"] = ws
    elif transport == "grpc":
        stream["grpcSettings"] = {"serviceName": _first(query, "serviceName")}
    fingerprint = _first(query, "fp", "chrome")
    if security == "reality":
        public_key = _first(query, "pbk")
        if not public_key or not server_name:
            raise XrayConfigError("REALITY public key or SNI missing")
        stream["realitySettings"] = {
            "serverName": server_name,
            "fingerprint": fingerprint,
            "publicKey": public_key,
            "shortId": _first(query, "sid"),
            "spiderX": _first(query, "spx", "/"),
        }
    else:
        stream["tlsSettings"] = {
            "serverName": server_name,
            "fingerprint": fingerprint,
        }
        alpn = _first(query, "alpn")
        if alpn:
            stream["tlsSettings"]["alpn"] = alpn.split(",")
    if scheme == "vless":
        user_id = unquote(url.username or "")
        try:
            user_id = str(uuid.UUID(user_id))
        except (ValueError, AttributeError):
            raise XrayConfigError("Invalid VLESS user ID") from None
        user = {"id": user_id, "encryption": "none"}
        flow = _first(query, "flow")
        if flow:
            user["flow"] = flow
        outbound = {
            "tag": "secure-egress",
            "protocol": "vless",
            "settings": {"vnext": [{"address": host, "port": port, "users": [user]}]},
        }
    else:
        password = unquote(url.username or "")
        if not password:
            raise XrayConfigError("Missing Trojan password")
        outbound = {
            "tag": "secure-egress",
            "protocol": "trojan",
            "settings": {"servers": [{"address": host, "port": port, "password": password}]},
        }
    outbound["streamSettings"] = stream
    return outbound


def _decode_subscription(data: str) -> str:
    data = data.strip().lstrip("\ufeff")
    if not data:
        raise XrayConfigError("Empty subscription")
    if "<html" in data[:512].lower() or "<!doctype" in data[:512].lower():
        raise XrayConfigError("Subscription returned an HTML installation page")
    if _NODE_RE.search(data):
        return data
    compact = re.sub(r"\s+", "", data)
    if not re.fullmatch(r"[A-Za-z0-9+/_=-]+", compact):
        raise XrayConfigError("Subscription is not a supported node list")
    try:
        blob = base64.urlsafe_b64decode(compact + "=" * (-len(compact) % 4))
        decoded = blob.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        raise XrayConfigError("Subscription encoding is unsupported") from None
    if not _NODE_RE.search(decoded):
        raise XrayConfigError("No compatible proxy nodes in subscription")
    return decoded


def first_compatible_node(raw: str) -> dict:
    decoded = _decode_subscription(raw)
    for line in decoded.splitlines():
        node = line.strip()
        if node.lower().startswith(("vless://", "trojan://")):
            try:
                return parse_node_link(node)
            except (XrayConfigError, ValueError):
                continue
    raise XrayConfigError("No compatible VLESS/Trojan TLS/REALITY node")


async def fetch_node(subscription: str, *, client=None) -> dict:
    """Try native subscription user agents; never echo URL or response content."""
    parsed = urlsplit(subscription)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username:
        raise XrayConfigError("Only HTTPS subscription URLs are accepted")
    if client is not None:
        return await _fetch_using_client(client, subscription)
    async with httpx.AsyncClient(timeout=12, follow_redirects=True, trust_env=False) as http:
        return await _fetch_using_client(http, subscription)


async def _fetch_using_client(http, subscription):
    for agent in _USER_AGENTS:
        try:
            response = await http.get(
                subscription,
                headers={"User-Agent": agent, "Accept": "*/*"},
            )
            if response.status_code != 200 or len(response.content) > _MAX_BYTES:
                continue
            return first_compatible_node(response.text)
        except (XrayConfigError, UnicodeError, httpx.HTTPError):
            continue
    raise XrayConfigError("Subscription did not return compatible Xray nodes")


def make_config(outbound: dict) -> dict:
    if outbound.get("protocol") not in {"vless", "trojan"}:
        raise XrayConfigError("Refusing unverified direct/blackhole outbound")
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [{
            "listen": _SOCKS_HOST,
            "port": _SOCKS_PORT,
            "protocol": "socks",
            "settings": {"auth": "noauth", "udp": False},
            "tag": "game-socks",
        }],
        "outbounds": [outbound],
    }


class XrayManager:
    """Owns a private SOCKS subprocess; exits cleanly without affecting the bot."""
    def __init__(self, *, binary="/usr/local/bin/xray"):
        self.binary = binary
        self.process = None
        self._directory = None
        self.proxy_url = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.stop()

    async def start_from_env(self) -> bool:
        node = os.getenv("XRAY_NODE_URL", "").strip()
        subscription = os.getenv("XRAY_SUBSCRIPTION_URL", "").strip()
        if not node and not subscription:
            log.info("Xray egress not configured; game traffic stays direct")
            return False
        try:
            outbound = parse_node_link(node) if node else await fetch_node(subscription)
            return await self._start_outbound(outbound)
        except (XrayConfigError, httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
            log.warning("Xray not activated (%s); game traffic stays direct", type(exc).__name__)
            await self.stop()
            return False

    async def _start_outbound(self, outbound: dict) -> bool:
        self._directory = tempfile.TemporaryDirectory(prefix="luckyjet-xray-")
        config_path = Path(self._directory.name) / "config.json"
        config_path.write_text(json.dumps(make_config(outbound)), encoding="utf-8")
        os.chmod(config_path, 0o600)
        self.process = await asyncio.create_subprocess_exec(
            self.binary, "run", "-c", str(config_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        for _ in range(40):
            if self.process.returncode is not None:
                raise XrayConfigError("Xray exited before readiness")
            try:
                reader, writer = await asyncio.open_connection(_SOCKS_HOST, _SOCKS_PORT)
                writer.close()
                await writer.wait_closed()
                break
            except OSError:
                await asyncio.sleep(0.1)
        else:
            raise XrayConfigError("Local Xray SOCKS port not ready")

        # A listening socket alone does not prove a functioning tunnel.
        test_proxy = f"socks5://{_SOCKS_HOST}:{_SOCKS_PORT}"
        try:
            async with httpx.AsyncClient(proxy=test_proxy, trust_env=False, timeout=10) as http:
                response = await http.get("https://api.ipify.org?format=json")
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict) or not result.get("ip"):
                    raise XrayConfigError("Proxy probe gave no egress IP")
        except (httpx.HTTPError, ValueError) as exc:
            raise XrayConfigError("Xray outbound connection probe failed") from None
        self.proxy_url = test_proxy
        log.info("Xray game egress verified through SOCKS; Telegram and Redis remain direct")
        return True

    async def stop(self):
        self.proxy_url = None
        if self.process is not None:
            if self.process.returncode is None:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    self.process.kill()
                    await self.process.wait()
            self.process = None
        if self._directory is not None:
            self._directory.cleanup()
            self._directory = None
