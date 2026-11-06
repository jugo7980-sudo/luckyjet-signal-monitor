import asyncio
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]

from luckyjet_password_gate import TelegramPasswordGate
from test_password_gate import FakeRequest, private_message, callback


class Account:
    def __init__(self):
        self.messages = []
        self.callbacks = []

    async def handle_message(self, msg):
        self.messages.append(msg)
        return msg.get('text') == 'site-password'

    async def handle_callback(self, cb):
        self.callbacks.append(cb)
        return cb.get('data', '').startswith('site_')


def test_gate_routes_secret_before_other_handlers_and_owner_menu_only():
    async def run():
        request = FakeRequest()
        gate = TelegramPasswordGate(request, admin_chat_id=42)
        gate.site_account = Account()
        gate.authorized.update({42, 43})
        await gate.handle_update(private_message(42, 'site-password'))
        await gate.handle_callback(callback(42, 'site_login'))
        assert len(gate.site_account.messages) == 1
        assert len(gate.site_account.callbacks) == 1
        assert not request.calls
        buttons = lambda uid: [b['callback_data'] for row in gate.menu(uid)['inline_keyboard'] for b in row if 'callback_data' in b]
        assert 'site_status' in buttons(42)
        assert 'site_status' not in buttons(43)
        await gate.close()
    asyncio.run(run())


def test_monitor_reloads_saved_session_and_forgets_old_capture(monkeypatch):
    from types import SimpleNamespace
    from luckyjet_signal import direct_only

    async def run():
        account = SimpleNamespace(generation=0, busy=False,
            storage_state=lambda: {'cookies': [], 'origins': []})
        opened = []
        closed = []
        holder = {}
        class Page:
            def is_closed(self): return False
            async def close(self): closed.append('page')
        class Context:
            async def close(self): closed.append('context')
        class Browser:
            async def new_context(self, **kwargs):
                opened.append(kwargs)
                return Context()
            async def close(self): closed.append('browser')
        class Chromium:
            async def launch(self, **kwargs): return Browser()
        class Manager:
            async def __aenter__(self): return SimpleNamespace(chromium=Chromium())
            async def __aexit__(self, *args): pass
        class Capture:
            async def snapshot(self): return [], ''
        async def open_game(context, config, capture): return Page()
        async def no_issue(page): return None
        async def send(*args): pass
        count = 0
        async def refresh(*args):
            nonlocal count
            count += 1
            if count == 1:
                account.generation += 1
                account.storage_state = lambda: None
            else:
                holder['task'].cancel()
            return {'worked': True, 'ok': True, 'changed': True}
        module = SimpleNamespace(async_playwright=Manager, LiveCapture=Capture,
            browser_lock=asyncio.Lock(), site_account=account,
            open_game=open_game, page_issue=no_issue, send=send, history_refresh=None,
            diagnose_page=send)
        monkeypatch.setattr(direct_only, 'refresh_live_history', refresh)
        config = SimpleNamespace(headless=True, poll_seconds=.01, error_notify_cooldown=300)
        holder['task'] = asyncio.create_task(direct_only.run_monitor(module, config, state=object()))
        await asyncio.gather(holder['task'], return_exceptions=True)
        assert len(opened) == 2
        assert opened[0]['storage_state'] == {'cookies': [], 'origins': []}
        assert 'storage_state' not in opened[1]
        assert closed.count('browser') == 2
        assert not module.browser_lock.locked()
    asyncio.run(run())


def test_monitor_yields_browser_lock_for_account_login(monkeypatch):
    from types import SimpleNamespace
    from luckyjet_signal import direct_only

    async def run():
        account = SimpleNamespace(generation=0, busy=True, storage_state=lambda: None)
        class Manager:
            async def __aenter__(self): return SimpleNamespace()
            async def __aexit__(self, *args): pass
        module = SimpleNamespace(async_playwright=Manager, site_account=account,
            browser_lock=asyncio.Lock(), history_refresh=None)
        task = asyncio.create_task(direct_only.run_monitor(module,
            SimpleNamespace(poll_seconds=.01), state=object()))
        await asyncio.sleep(.03)
        assert not task.done()
        assert not module.browser_lock.locked()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())
