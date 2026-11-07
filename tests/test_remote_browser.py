"""Remote owner browser: deny anonymous access, keep secrets out, save only confirmed login."""
import asyncio
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

from mini_app_remote import RemoteSession, remote_browser


def signed(uid=99):
    fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid})}
    signature_input = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", b"fixture", hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, signature_input.encode(), hashlib.sha256).hexdigest()
    return "tma " + urlencode(fields)


def decoded(response):
    return response[0], json.loads(response[2])


class FakeMouse:
    def __init__(self):
        self.clicks = []
        self.events = []
    async def click(self, x, y):
        self.clicks.append((x, y))
    async def move(self, x, y):
        self.events.append(("move", x, y))
    async def down(self):
        self.events.append(("down",))
    async def up(self):
        self.events.append(("up",))
    async def wheel(self, x, y):
        return None


class FakeKeyboard:
    def __init__(self):
        self.secrets = []
    async def insert_text(self, text):
        self.secrets.append(text)
    async def press(self, key):
        return None


class FakeField:
    def __init__(self):
        self.value = None
        self.pressed = []
    async def fill(self, value, **kwargs):
        self.value = value
    async def press(self, key, **kwargs):
        self.pressed.append(key)


class FakePage:
    def __init__(self):
        self.url = "https://example.test/login"
        self.mouse = FakeMouse()
        self.keyboard = FakeKeyboard()
        self.masked = False
    async def goto(self, url, **kwargs):
        self.url = url
    async def reload(self, **kwargs):
        return None
    def locator(self, selector):
        assert "input" in selector
        return object()
    async def screenshot(self, **kwargs):
        self.masked = bool(kwargs.get("mask")) and kwargs["type"] == "jpeg"
        return b"JPEG-screen-bytes"


class FakeContext:
    async def storage_state(self, **kwargs):
        assert kwargs["indexed_db"]
        return {"cookies": [{"name": "test", "value": "secret"}], "origins": []}


class FakeDriver:
    def __init__(self):
        self.page = FakePage()
        self.context = FakeContext()
        self.login_url = "https://example.test/login"
        self.logged_in = False
        self.closed = False
        self.fields = {"email": FakeField(), "password": FakeField()}
    async def _field(self, name):
        return self.fields.get(name)
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        self.closed = True
    async def _success(self):
        return self.logged_in


class Store:
    def __init__(self):
        self.value = None
    def load(self):
        return self.value
    def save(self, data):
        self.value = data


class Account:
    def __init__(self):
        self.owner_id = 99
        self.busy = False
        self.field = None
        self.store = Store()
        self.status = "disconnected"
        self.generation = 0
        self.last_driver = None
    def driver_factory(self):
        self.last_driver = FakeDriver()
        return self.last_driver


class Gate:
    def __init__(self):
        self.site_account = Account()
    async def is_authorized(self, uid):
        return uid in (99, 42)
    def is_admin(self, uid):
        return uid == 99


def test_remote_owner_access_and_manual_actions_are_not_public():
    async def run():
        gate = Gate()
        async def call(uid=99, action=None, **kwargs):
            data = {"action": action, **kwargs} if action else None
            return decoded(await remote_browser(
                signed(uid), gate, "fixture",
                "POST" if data else "GET",
                json.dumps(data).encode() if data else None,
            ))
        assert decoded(await remote_browser("", gate, "fixture"))[0] == "401 Unauthorized"
        assert (await call(uid=42))[0] == "403 Forbidden"
        assert (await call(uid=42, action="start"))[0] == "403 Forbidden"
        assert (await call(action="start"))[0] == "200 OK"
        status, data = await call()
        assert status == "200 OK" and data["active"] and data["image"]
        assert gate.site_account.last_driver.page.masked
        assert (await call(action="tap", x=.5, y=.5))[0] == "200 OK"
        assert gate.site_account.last_driver.page.mouse.clicks[-1] == (215, 466)
        assert (await call(action="drag", points=[[.1, .8], [.3, .8], [.7, .8]],
                           duration_ms=300))[0] == "200 OK"
        events = gate.site_account.last_driver.page.mouse.events
        assert events[0] == ("move", 43, 745.6)
        assert events[1] == ("down",)
        assert events[-1] == ("up",)
        assert events[-2] == ("move", 301, 745.6)
        events.clear()
        assert (await call(action="pointer", phase="down", gesture=11, x=.1, y=.8))[0] == "200 OK"
        assert (await call(action="pointer", phase="move", gesture=11, x=.3, y=.8))[0] == "200 OK"
        assert gate.site_account._remote_browser_session.pointer_active
        assert events == [("move", 43.0, 745.6), ("down",), ("move", 129.0, 745.6)]
        assert (await call(action="pointer", phase="move", gesture=12, x=.5, y=.8))[0] == "400 Bad Request"
        assert (await call(action="tap", x=.4, y=.8))[0] == "400 Bad Request"
        assert (await call(action="pointer", phase="up", gesture=11, x=.7, y=.8))[0] == "200 OK"
        assert events[-2:] == [("move", 301.0, 745.6), ("up",)]
        assert not gate.site_account._remote_browser_session.pointer_active
        assert (await call(action="pointer", phase="up", gesture=11, x=.8, y=.8))[0] == "400 Bad Request"
        assert (await call(action="pointer", phase="down", gesture=3, x=1.2, y=.3))[0] == "400 Bad Request"
        assert (await call(action="drag", points=[[.1, .8], [1.3, .8]],
                           duration_ms=300))[0] == "400 Bad Request"
        assert (await call(action="drag", points=[[.1, .8], [.3, .8]],
                           duration_ms=50000))[0] == "400 Bad Request"
        assert (await call(action="drag", points=[[.1, .8], [.101, .8]],
                           duration_ms=300))[0] == "400 Bad Request"
        assert (await call(action="fill_email", text="example@test.example"))[0] == "200 OK"
        assert (await call(action="fill_password", text="different-private-password"))[0] == "200 OK"
        assert gate.site_account.last_driver.fields["email"].value == "example@test.example"
        assert gate.site_account.last_driver.fields["password"].value == "different-private-password"
        assert gate.site_account.last_driver.fields["email"].pressed == ["Tab"]
        assert gate.site_account.last_driver.fields["password"].pressed == ["Tab"]
        assert (await call(action="fill_password", text="corrected-private-password"))[0] == "200 OK"
        assert gate.site_account.last_driver.fields["password"].value == "corrected-private-password"
        assert gate.site_account.last_driver.fields["email"].value == "example@test.example"
        assert (await call(action="type", text="one-time-secret"))[0] == "200 OK"
        assert (await call(action="key", key="Control+Shift+I"))[0] == "400 Bad Request"
        assert (await call(action="save"))[0] == "400 Bad Request"
        assert gate.site_account.store.load() is None
        gate.site_account.last_driver.logged_in = True
        code, result = await call(action="save")
        assert code == "200 OK" and result["saved"]
        assert gate.site_account.store.load()["cookies"]
        assert gate.site_account.generation == 1
        assert gate.site_account.last_driver.closed
        assert "one-time-secret" not in json.dumps(result)
        assert "different-private-password" not in json.dumps(result)
        assert "corrected-private-password" not in json.dumps(result)
    asyncio.run(run())


def test_remote_stop_closes_browser_and_blocks_interactions():
    async def run():
        account = Account()
        session = RemoteSession(account)
        await session.start()
        assert (await session.status())["active"]
        assert (await session.action({"action": "tap", "x": -1, "y": 0}))["error"]
        await session.close()
        assert (await session.status())["active"] is False
        assert account.last_driver.closed
    asyncio.run(run())


def test_frame_capture_does_not_block_live_pointer_events():
    async def run():
        account = Account()
        session = RemoteSession(account)
        await session.start()
        screenshot_started = asyncio.Event()
        allow_screenshot = asyncio.Event()

        async def slow_screenshot(**kwargs):
            screenshot_started.set()
            await allow_screenshot.wait()
            return b"small-jpeg"

        account.last_driver.page.screenshot = slow_screenshot
        frame_task = asyncio.create_task(session.status(capture=True))
        await asyncio.wait_for(screenshot_started.wait(), 1)
        try:
            result = await asyncio.wait_for(session.action({
                "action": "pointer", "phase": "down", "gesture": 14,
                "x": .12, "y": .75,
            }), .5)
            assert result["message"] == "Движение передано."
            assert session.pointer_active
            result = await asyncio.wait_for(session.action({
                "action": "pointer", "phase": "up", "gesture": 14,
                "x": .71, "y": .75,
            }), .5)
            assert result["message"] == "Движение передано."
        finally:
            allow_screenshot.set()
        assert (await frame_task)["image"]
        await session.close()
    asyncio.run(run())


def test_remote_pointer_released_if_drag_goes_stale():
    from mini_app_remote import POINTER_IDLE_SECONDS

    async def run():
        account = Account()
        session = RemoteSession(account)
        await session.start()
        await session.action({
            "action": "pointer", "phase": "down", "gesture": 19,
            "x": .12, "y": .75,
        })
        assert session.pointer_active
        session.pointer_last -= POINTER_IDLE_SECONDS + 2
        await asyncio.sleep(1.1)
        assert not session.pointer_active
        assert account.last_driver.page.mouse.events[-1] == ("up",)
        await session.close()
    asyncio.run(run())


def test_proxy_ip_check_fail_closed_and_fallback(monkeypatch):
    import httpx
    from mini_app_remote import check_japanese_egress

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["proxy"] == "socks5://127.0.0.1:10808"
            assert kwargs["trust_env"] is False
            self.calls = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def get(self, url):
            self.calls.append(url)
            if "country.is" in url:
                return httpx.Response(503, request=httpx.Request("GET", url))
            return httpx.Response(200, json={"country_code": "JP", "ip": "8.8.8.8"},
                                  request=httpx.Request("GET", url))

    clients = []
    def factory(**kwargs):
        client = Client(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr("mini_app_remote.httpx.AsyncClient", factory)
    async def run():
        import pytest
        with pytest.raises(ValueError, match="No verified local SOCKS"):
            await check_japanese_egress(None)
        with pytest.raises(ValueError, match="No verified local SOCKS"):
            await check_japanese_egress("http://example.test")
        result = await check_japanese_egress("socks5://127.0.0.1:10808")
        assert result == {"ip": "8.8.8.8", "country": "JP", "japan_verified": True}
        assert clients[-1].calls == ["https://api.country.is/", "https://ipwho.is/"]
    asyncio.run(run())


def test_remote_ip_check_requires_owner_and_active_proxy(monkeypatch):
    from types import SimpleNamespace
    gate = Gate()

    async def fake_probe(proxy_url):
        assert proxy_url == "socks5://127.0.0.1:10808"
        return {"ip": "1.1.1.1", "country": "JP", "japan_verified": True}

    monkeypatch.setattr("mini_app_remote.check_japanese_egress", fake_probe)

    async def call(auth, action):
        return decoded(await remote_browser(auth, gate, "fixture", "POST",
                                             json.dumps({"action": action}).encode()))

    async def run():
        assert (await call("", "check_ip"))[0] == "401 Unauthorized"
        assert (await call(signed(42), "check_ip"))[0] == "403 Forbidden"
        status, body = await call(signed(99), "check_ip")
        assert status == "503 Service Unavailable"
        assert "Прямой запрос запрещён" in body["error"]
        assert "ip" not in body
        gate.site_account.driver_factory = lambda: SimpleNamespace(
            proxy_url="socks5://127.0.0.1:10808")
        status, body = await call(signed(99), "check_ip")
        assert status == "200 OK"
        assert body == {"ip": "1.1.1.1", "country": "JP", "japan_verified": True}
        assert gate.site_account.store.load() is None
        assert gate.site_account.generation == 0
    asyncio.run(run())
