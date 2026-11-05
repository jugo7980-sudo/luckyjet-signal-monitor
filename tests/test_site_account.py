import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]


def message(text, user=42, chat_type='private'):
    return {'message_id': 9, 'text': text, 'from': {'id': user},
            'chat': {'id': user, 'type': chat_type}}


class Telegram:
    def __init__(self):
        self.calls = []

    async def __call__(self, method, payload):
        self.calls.append((method, payload))
        return {'ok': True, 'result': {'message_id': len(self.calls)}}


class LoginDriver:
    def __init__(self, otp=False, fail=False):
        self.otp = otp
        self.fail = fail
        self.credentials = None
        self.received_code = None
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def login(self, email, password, request_code):
        self.credentials = (email, password)
        if self.otp:
            self.received_code = await request_code(False)
        if self.fail:
            raise RuntimeError('password=secret code=123456')
        return {'cookies': [{'name': 'session', 'value': 'test-cookie'}], 'origins': []}


async def settle(predicate):
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError('task did not reach expected state')


def account(tmp_path, driver=None):
    from site_account import SiteAccount, SessionStore
    tg = Telegram()
    driver = driver or LoginDriver()
    store = SessionStore(tmp_path / 'session.json', 'https://example.test/game')
    obj = SiteAccount(42, tg, store, lambda: driver, asyncio.Lock(), input_timeout=.5)
    return obj, tg, driver


def test_owner_only_private_flow_and_secret_messages_are_deleted(tmp_path):
    async def run():
        obj, tg, driver = account(tmp_path)
        assert not await obj.handle_message(message('/site', user=43))
        assert not await obj.handle_message(message('/site', chat_type='group'))
        assert obj.field is None
        await obj.start_login()
        await obj.handle_message(message('owner@example.test'))
        await obj.handle_message(message('  secret  '))
        await settle(lambda: not obj.busy)
        assert driver.credentials == ('owner@example.test', '  secret  ')
        assert obj.status == 'connected'
        assert obj.generation == 1
        assert driver.closed
        assert len([p for m, p in tg.calls if m == 'deleteMessage']) == 2
        rendered = json.dumps([p for m, p in tg.calls if m == 'sendMessage'])
        assert 'secret' not in rendered
        assert 'secret' not in obj.store.path.read_text()
        assert obj.field is None
        await obj.close()
    asyncio.run(run())


def test_otp_goes_to_waiting_browser_and_is_not_persisted(tmp_path):
    async def run():
        obj, tg, driver = account(tmp_path, LoginDriver(otp=True))
        await obj.start_login()
        await obj.handle_message(message('owner@example.test'))
        await obj.handle_message(message('secret'))
        await settle(lambda: obj.field == 'code')
        assert await obj.handle_message(message('/site'))
        assert obj.field == 'code'
        await obj.handle_message(message('123456'))
        await settle(lambda: not obj.busy)
        assert driver.received_code == '123456'
        assert obj.status == 'connected'
        assert '123456' not in obj.store.path.read_text()
        await obj.close()
    asyncio.run(run())


def test_cancel_and_logout_cannot_save_an_old_attempt(tmp_path):
    async def run():
        obj, tg, driver = account(tmp_path, LoginDriver(otp=True))
        await obj.start_login()
        await obj.handle_message(message('owner@example.test'))
        await obj.handle_message(message('secret'))
        await settle(lambda: obj.field == 'code')
        await obj.forget()
        assert not obj.busy and obj.field is None
        assert driver.closed and not obj.store.path.exists()
        assert obj.status == 'disconnected'
        await obj.handle_message(message('123456'))
        assert not obj.store.path.exists()
        await obj.close()
    asyncio.run(run())


def test_input_timeout_clears_email_and_requests_fresh_login(tmp_path):
    async def run():
        obj, tg, driver = account(tmp_path)
        obj.input_timeout = .01
        await obj.start_login()
        await obj.handle_message(message('owner@example.test'))
        await asyncio.sleep(.04)
        assert obj.field is None and obj.email is None
        assert not obj.busy
        await obj.close()
    asyncio.run(run())


def test_failure_never_echoes_exception_or_credentials(tmp_path, caplog):
    async def run():
        obj, tg, driver = account(tmp_path, LoginDriver(fail=True))
        await obj.start_login()
        await obj.handle_message(message('owner@example.test'))
        await obj.handle_message(message('secret'))
        await settle(lambda: not obj.busy)
        assert obj.status == 'error'
        assert not obj.store.path.exists()
        rendered = json.dumps(tg.calls) + caplog.text
        assert 'password=secret' not in rendered and '123456' not in rendered
        await obj.close()
    asyncio.run(run())


def test_session_store_is_atomic_bound_to_site_and_corruption_is_safe(tmp_path):
    from site_account import SessionStore
    path = tmp_path / 'session.json'
    store = SessionStore(path, 'https://example.test/game')
    state = {'cookies': [], 'origins': []}
    store.save(state)
    assert store.load() == state
    assert SessionStore(path, 'https://other.test/game').load() is None
    path.write_text('{bad')
    assert store.load() is None
    store.clear()
    assert not path.exists()


def test_owner_callback_rejects_forged_group_or_non_owner(tmp_path):
    async def run():
        obj, tg, driver = account(tmp_path)
        for user, kind in [(43, 'private'), (42, 'group')]:
            cb = {'id': 'c', 'data': 'site_login', 'from': {'id': user},
                  'message': {'chat': {'id': user, 'type': kind}}}
            assert await obj.handle_callback(cb)
            assert obj.field is None
        assert not [p for m,p in tg.calls if m == 'sendMessage']
        await obj.close()
    asyncio.run(run())


def test_slash_prefixed_password_is_passed_verbatim(tmp_path):
    async def run():
        obj, tg, driver = account(tmp_path)
        await obj.start_login()
        await obj.handle_message(message('owner@example.test'))
        await obj.handle_message(message('/validPassword123!'))
        await settle(lambda: not obj.busy)
        assert driver.credentials == ('owner@example.test', '/validPassword123!')
        assert obj.status == 'connected'
        await obj.close()
    asyncio.run(run())
