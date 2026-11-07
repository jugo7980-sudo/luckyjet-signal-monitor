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
    async def click(self, x, y):
        self.clicks.append((x, y))
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
