"""Xray integration must not disrupt the existing Telegram bot."""
import asyncio
import base64
import json
from pathlib import Path

import pytest

from xray_runtime import (
    XrayConfigError, XrayManager, fetch_node, first_compatible_node,
    make_config, parse_node_link,
)


UID = "b831381d-6324-4d53-ad4f-8cda48b30811"
VLESS = (
    f"vless://{UID}@node.example.org:443?type=tcp&security=reality"
    "&sni=www.example.com&fp=chrome&pbk=TEST_PUBLIC_KEY&sid=de"
    "&flow=xtls-rprx-vision#Tokyo"
)


def test_parse_vless_reality_security_and_private_loopback():
    outbound = parse_node_link(VLESS)
    assert outbound["protocol"] == "vless"
    assert outbound["settings"]["vnext"][0]["address"] == "node.example.org"
    assert outbound["settings"]["vnext"][0]["users"][0]["id"] == UID
    assert outbound["settings"]["vnext"][0]["users"][0]["flow"] == "xtls-rprx-vision"
    stream = outbound["streamSettings"]
    assert stream["security"] == "reality"
    assert stream["realitySettings"]["publicKey"] == "TEST_PUBLIC_KEY"
    assert stream["realitySettings"]["serverName"] == "www.example.com"

    config = make_config(outbound)
    assert config["inbounds"] == [{
        "listen": "127.0.0.1", "port": 10808, "protocol": "socks",
        "settings": {"auth": "noauth", "udp": False}, "tag": "game-socks",
    }]
    assert config["outbounds"] == [outbound]
    assert not [r for r in config["outbounds"] if r["protocol"] == "freedom"]


def test_trojan_tls_ws_server_name_and_transport():
    node = ("trojan://supersecret@edge.example.org:443?type=ws&security=tls"
            "&sni=cdn.example.org&path=%2Ffeed&host=cdn.example.org")
    outbound = parse_node_link(node)
    assert outbound["settings"]["servers"][0]["password"] == "supersecret"
    assert outbound["streamSettings"]["network"] == "ws"
    assert outbound["streamSettings"]["tlsSettings"]["serverName"] == "cdn.example.org"
    assert outbound["streamSettings"]["wsSettings"]["path"] == "/feed"
    assert outbound["streamSettings"]["wsSettings"]["headers"] == {"Host": "cdn.example.org"}


@pytest.mark.parametrize("link", [
    "happ://encrypted", "https://example.com/sub", "vless://bad@node.example.org:443?security=tls",
    f"vless://{UID}@node.example.org:443?security=none",
    f"vless://{UID}@node.example.org:443?type=unsupported&security=tls",
    f"vless://{UID}@node.example.org:443?security=reality",
])
def test_unsupported_formats_fail_closed(link):
    with pytest.raises((XrayConfigError, ValueError)):
        parse_node_link(link)


def test_plain_and_base64_subscriptions_select_first_compatible_node():
    original = "happ://encrypted\n" + VLESS + "\n"
    assert first_compatible_node(original)["protocol"] == "vless"
    encoded = base64.b64encode(original.encode()).decode()
    assert first_compatible_node(encoded)["protocol"] == "vless"


@pytest.mark.parametrize("html", [
    '<!doctype html><html><title>Atlanta VPN</title></html>',
    '<html><body>Installation</body></html>',
    "vmess://unsupported-node",
])
def test_landing_page_and_unknown_subscription_do_not_create_proxy(html):
    with pytest.raises(XrayConfigError):
        first_compatible_node(html)


def test_direct_mode_if_subscription_is_absent(monkeypatch):
    monkeypatch.delenv("XRAY_SUBSCRIPTION_URL", raising=False)
    monkeypatch.delenv("XRAY_NODE_URL", raising=False)
    manager = XrayManager()
    async def run():
        assert await manager.start_from_env() is False
        assert manager.process is None
        assert manager.proxy_url is None
        await manager.stop()
    asyncio.run(run())


def test_invalid_subscription_keeps_game_direct_without_printing_secrets(monkeypatch, caplog):
    secret = "https://private.example.org/a-secret-subscription-token"
    monkeypatch.setenv("XRAY_SUBSCRIPTION_URL", secret)
    monkeypatch.delenv("XRAY_NODE_URL", raising=False)
    async def broken(_):
        raise XrayConfigError("Subscription did not return compatible Xray nodes")
    monkeypatch.setattr("xray_runtime.fetch_node", broken)
    manager = XrayManager()
    async def run():
        assert await manager.start_from_env() is False
        assert manager.proxy_url is None
        assert manager.process is None
        await manager.stop()
    asyncio.run(run())
    assert secret not in caplog.text
    assert "Xray not activated" in caplog.text


def test_invalid_upstream_refuses_unprotected_direct_egress():
    with pytest.raises(XrayConfigError):
        make_config({"protocol": "freedom", "tag": "bypass"})


def test_fetch_attempts_two_user_agents_and_ignores_browser_landing():
    class Response:
        status_code = 200
        content = b"<html>Atlanta VPN</html>"
        text = content.decode()
    class Client:
        def __init__(self):
            self.headers = []
        async def get(self, url, headers):
            self.headers.append(headers["User-Agent"])
            if len(self.headers) == 2:
                r = Response()
                r.content = VLESS.encode()
                r.text = VLESS
                return r
            return Response()
    client = Client()
    async def run():
        outbound = await fetch_node("https://subscription.example.org/secret", client=client)
        assert outbound["protocol"] == "vless"
    asyncio.run(run())
    assert len(client.headers) == 2


def test_game_polling_proxy_opt_in_only(monkeypatch):
    from luckyjet_signal import monitor
    class Capture:
        async def direct_targets(self):
            return ["https://game.example.org/history"], {}
        async def ingest(self, payload, source):
            return 1
    observed = []
    class Response:
        status_code = 200
        text = '{"round": 1}'
    class Client:
        def __init__(self, **kwargs):
            observed.append(kwargs)
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def get(self, url, headers):
            return Response()
    monkeypatch.setattr(monitor.httpx, "AsyncClient", Client)
    async def run():
        monkeypatch.setattr(monitor, "game_proxy", None, raising=False)
        assert await monitor.poll_direct_gateways(Capture()) == 1
        monkeypatch.setattr(monitor, "game_proxy", "socks5://127.0.0.1:10808", raising=False)
        assert await monitor.poll_direct_gateways(Capture()) == 1
    asyncio.run(run())
    assert "proxy" not in observed[0]
    assert observed[1]["proxy"] == "socks5://127.0.0.1:10808"


def test_failed_xray_subprocess_does_not_activate_proxy(monkeypatch):
    from xray_runtime import XrayManager
    monkeypatch.setenv("XRAY_NODE_URL", VLESS)
    monkeypatch.delenv("XRAY_SUBSCRIPTION_URL", raising=False)
    calls = []
    async def fake_subprocess(*args, **kwargs):
        calls.append(args)
        raise OSError("Binary unavailable")
    monkeypatch.setattr("xray_runtime.asyncio.create_subprocess_exec", fake_subprocess)
    manager = XrayManager(binary="/missing/xray")
    async def run():
        assert not await manager.start_from_env()
        assert manager.proxy_url is None
        assert manager.process is None
        await manager.stop()
    asyncio.run(run())
    assert len(calls) == 1
    assert calls[0][0] == "/missing/xray"
