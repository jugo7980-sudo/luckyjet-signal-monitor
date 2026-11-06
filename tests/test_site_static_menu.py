from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import asyncio
import pytest
from site_account import SiteAccount, SessionStore
from luckyjet_reliable_gate import ReliableTelegramPasswordGate
from test_static_chat import Telegram


def setup(tmp_path):
    tg = Telegram()
    gate = ReliableTelegramPasswordGate(tg, admin_chat_id=42)
    gate.authorized.add(42)
    gate.chats[42] = 42
    account = SiteAccount(42, tg, SessionStore(tmp_path / 'session.json', 'https://example.test'), None, asyncio.Lock())
    gate.site_account = account
    return gate, account, tg


async def click(gate, action):
    await gate.handle_callback({'id': action, 'data': action, 'from': {'id': 42},
        'message': {'message_id': gate.menu_messages.get(42, 10), 'chat': {'id': 42, 'type': 'private'}}})


def test_account_navigation_uses_one_message(tmp_path):
    async def run():
        gate, account, tg = setup(tmp_path)
        await click(gate, 'menu_open')
        for action in ['site_status', 'site_status', 'site_login', 'site_cancel', 'site_forget', 'site_status', 'menu_main']:
            await click(gate, action)
        assert sum(m == 'sendMessage' for m,p in tg.calls) == 1
        await account.close(); await gate.close()
    asyncio.run(run())


@pytest.mark.parametrize('action', ['menu_main', 'menu_close', 'demo_menu', 'menu_settings'])
def test_navigation_cancels_account_input(tmp_path, action):
    async def run():
        gate, account, tg = setup(tmp_path)
        await click(gate, 'menu_open'); await click(gate, 'site_login')
        await click(gate, action)
        assert account.field is None
        await account.close(); await gate.close()
    asyncio.run(run())


def test_old_forget_confirmation_cannot_delete_session(tmp_path):
    async def run():
        gate, account, tg = setup(tmp_path)
        account.store.save({'cookies': [], 'origins': []})
        await click(gate, 'menu_open'); await click(gate, 'site_forget')
        await click(gate, 'menu_main'); await click(gate, 'site_forget_confirm')
        assert account.store.load() is not None
        await account.close(); await gate.close()
    asyncio.run(run())


def test_timeout_cancels_input_without_new_messages(tmp_path):
    async def run():
        gate, account, tg = setup(tmp_path)
        await click(gate, 'menu_open'); await click(gate, 'site_login')
        deadline = gate.menu_deadlines[42]
        await gate._close_menu(42, expected_deadline=deadline, now=deadline + 1)
        assert account.field is None
        assert 42 not in gate.menu_messages
        assert sum(m == 'sendMessage' for m,p in tg.calls) == 1
        await account.close(); await gate.close()
    asyncio.run(run())


def test_failed_secret_deletion_is_retried_and_persisted(tmp_path):
    import fakeredis.aioredis
    import httpx
    async def run():
        gate, account, tg = setup(tmp_path)
        gate.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        failures = [True]
        async def request(method, payload):
            if method == 'deleteMessage' and failures[0]:
                raise httpx.ConnectError('offline')
            return await tg(method, payload)
        account.request = request
        await account._delete({'message_id': 77})
        pending = await gate.redis.hgetall('luckyjet:site_cleanup:42')
        assert '77' in pending
        failures[0] = False
        await account.cleanup.flush()
        assert not await gate.redis.hgetall('luckyjet:site_cleanup:42')
        assert any(m == 'deleteMessage' and p['message_id'] == 77 for m,p in tg.calls)
        await account.close(); await gate.close()
    asyncio.run(run())


def test_closing_running_login_cannot_reopen_menu(tmp_path):
    async def run():
        gate, account, tg = setup(tmp_path)
        started = asyncio.Event()
        class Driver:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def login(self, *args):
                started.set()
                await asyncio.Event().wait()
        account.driver_factory = Driver
        await click(gate, 'menu_open'); await click(gate, 'site_login')
        def message(text): return {'message_id': 20, 'text': text, 'from': {'id':42}, 'chat': {'id':42, 'type':'private'}}
        await gate.handle_update({'message':message('owner@example.test')})
        await gate.handle_update({'message':message('secret')})
        await started.wait()
        await click(gate, 'menu_close')
        count = len(tg.calls)
        await asyncio.sleep(0)
        assert not account.busy and account.field is None
        assert 42 not in gate.menu_messages
        assert len(tg.calls) == count
        await account.close(); await gate.close()
    asyncio.run(run())


def test_menu_command_cancels_browser_even_between_input_steps(tmp_path):
    async def run():
        gate, account, tg = setup(tmp_path)
        await click(gate, 'menu_open')
        account.visible = True
        account._task = asyncio.create_task(asyncio.Event().wait())
        assert account.field is None and account.busy
        await gate.handle_update({'message': {'message_id': 20, 'text': '/menu',
            'from': {'id':42}, 'chat': {'id':42, 'type':'private'}}})
        assert not account.busy and not account.visible
        await account.close(); await gate.close()
    asyncio.run(run())


def test_site_command_from_history_starts_menu_timeout(tmp_path):
    async def run():
        gate, account, tg = setup(tmp_path)
        await click(gate, 'menu_open'); await click(gate, 'demo_history')
        await gate.handle_update({'message': {'message_id':20, 'text':'/site',
            'from':{'id':42}, 'chat':{'id':42,'type':'private'}}})
        assert 42 not in gate.demo_history_pages
        assert 42 in gate.menu_deadlines
        await account.close(); await gate.close()
    asyncio.run(run())


def test_forget_confirmation_is_unique_to_each_prompt(tmp_path):
    async def run():
        gate, account, tg = setup(tmp_path)
        account.store.save({'cookies': [], 'origins': []})
        await click(gate, 'menu_open'); await click(gate, 'site_forget')
        def confirm():
            payload = [p for m,p in tg.calls if m in {'sendMessage', 'editMessageText'}][-1]
            return payload['reply_markup']['inline_keyboard'][0][0]['callback_data']
        old = confirm()
        await click(gate, 'site_status'); await click(gate, 'site_forget')
        current = confirm()
        await click(gate, old)
        assert account.store.load() is not None
        await click(gate, current)
        assert account.store.load() is None
        await account.close(); await gate.close()
    asyncio.run(run())


def test_cleanup_restores_after_restart_without_secret_content(tmp_path):
    import fakeredis.aioredis
    import httpx
    async def run():
        gate, account, tg = setup(tmp_path)
        db = fakeredis.aioredis.FakeRedis(decode_responses=True)
        gate.redis = db
        async def offline(*args): raise httpx.ConnectError('offline')
        account.request = offline
        await account._delete({'message_id': 77, 'text': 'private-password'})
        await account.close()
        gate.redis = None
        await gate.close()
        restored, second, transport = setup(tmp_path)
        restored.redis = db
        await second.cleanup.restore()
        await second.cleanup.flush(force=True)
        assert not await db.hgetall(second.cleanup.key)
        assert any(m == 'deleteMessage' and p['message_id'] == 77 for m,p in transport.calls)
        await second.close(); await restored.close()
    asyncio.run(run())


def test_stale_timeout_waiting_for_input_does_not_cancel_new_deadline(tmp_path):
    async def run():
        gate, account, tg = setup(tmp_path)
        await click(gate, 'menu_open'); await click(gate, 'site_login')
        old = gate.menu_deadlines[42]
        async with account._control:
            closing = asyncio.create_task(gate._close_menu(42, expected_deadline=old, now=old + 1))
            await asyncio.sleep(0)
            gate.menu_deadlines[42] = old + 60
        assert not await closing
        assert account.field == 'email' and account.visible
        await account.close(); await gate.close()
    asyncio.run(run())


def test_expired_input_does_not_extend_or_reopen_menu(tmp_path):
    async def run():
        gate, account, tg = setup(tmp_path)
        await click(gate, 'menu_open'); await click(gate, 'site_login')
        gate.menu_deadlines[42] = 1
        count = sum(m in {'sendMessage', 'editMessageText'} for m,p in tg.calls)
        await gate.handle_update({'message': {'message_id':20,'text':'owner@example.test',
            'from':{'id':42},'chat':{'id':42,'type':'private'}}})
        assert gate.menu_deadlines[42] == 1
        assert sum(m in {'sendMessage', 'editMessageText'} for m,p in tg.calls) == count
        await account.close(); await gate.close()
    asyncio.run(run())


def test_error_screenshot_is_removed_when_menu_closes(tmp_path):
    from site_account import LoginProblem
    async def run():
        gate, account, tg = setup(tmp_path)
        async def transport(method, payload):
            if method == 'sendPhoto':
                tg.calls.append((method, payload))
                return {'result': {'message_id': 88}}
            return await tg(method, payload)
        account.request = transport
        class Driver:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def login(self, *args):
                problem = LoginProblem('Страница входа недоступна')
                problem.page_screenshot = b'fixture'
                raise problem
        account.driver_factory = Driver
        await click(gate, 'menu_open'); await click(gate, 'site_login')
        account._task = asyncio.create_task(account._login('owner@example.test', 'private-password'))
        await account._task
        assert 88 in account.cleanup.pending
        await click(gate, 'menu_close')
        assert 88 not in account.cleanup.pending
        assert any(m == 'deleteMessage' and p['message_id'] == 88 for m,p in tg.calls)
        await account.close(); await gate.close()
    asyncio.run(run())
