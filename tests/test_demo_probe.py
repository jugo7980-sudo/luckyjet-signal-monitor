import asyncio
from luckyjet_password_gate import TelegramPasswordGate


def test_demo_probe_is_available_only_to_authorized_admin():
    async def run():
        sent = []
        async def request(method, payload):
            sent.append((method, payload))
            return {'result': {'message_id': 99}}
        gate = TelegramPasswordGate(request, admin_chat_id=42)
        gate.authorized.update({42, 43})
        class Probe:
            def start(self):
                sent.append(('probe', {}))
            def text(self):
                return 'Диагностика деморежима'
        gate.demo_probe = Probe()
        for uid in (43, 42):
            await gate.handle_callback({'id': 'cb', 'from': {'id': uid},
                'message': {'message_id': 1, 'chat': {'id': uid}}, 'data': 'demo_probe'})
            assert sum(m == 'probe' for m, _ in sent) == (1 if uid == 42 else 0)
        assert any(m == 'sendMessage' and 'Диагностика' in p.get('text', '') for m, p in sent)
    asyncio.run(run())


def test_page_classification_does_not_treat_history_or_demo_button_as_active_demo():
    from demo_probe import classify_page
    assert classify_page('1.2x 3.4x Play demo') == 'demo_available'
    assert classify_page('1.2x 3.4x') == 'unconfirmed'
    assert classify_page('The site is not available in your region. Play demo') == 'region_blocked'
    assert classify_page('Verify you are human') == 'verification'


def test_probe_waits_until_monitor_releases_browser_memory():
    from demo_probe import DemoProbe
    async def run():
        lock = asyncio.Lock()
        await lock.acquire()
        probe = DemoProbe(browser_lock=lock)
        entered = asyncio.Event()
        async def inspect():
            entered.set()
            return 'unconfirmed'
        probe._inspect = inspect
        task = probe.start()
        await asyncio.sleep(0)
        assert not entered.is_set()
        lock.release()
        await task
        assert entered.is_set()
        assert not lock.locked()
    asyncio.run(run())
