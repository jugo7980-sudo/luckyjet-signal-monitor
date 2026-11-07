import asyncio
from notification_queue import NotificationQueue
from luckyjet_reliable_gate import ReliableTelegramPasswordGate


def test_slow_notices_do_not_block_rounds_and_preserve_streak_snapshots():
    async def run():
        gate=ReliableTelegramPasswordGate(None)
        gate.authorized.add(1)
        started=asyncio.Event();release=asyncio.Event();seen=[]
        async def update(uid,ending=None):
            started.set();await release.wait()
            seen.append((list(gate.current_streak),ending))
        gate._update_streak_notice=update
        gate.notification_queue=NotificationQueue(gate)
        await gate.on_round('1.2x')
        await started.wait()
        await asyncio.wait_for(gate.on_round('2.0x'),.2)
        assert gate.current_streak==[]
        release.set();await gate.notification_queue.queue.join()
        assert seen==[(['1.20x'],None),(['1.20x'],'2.00x')]
        await gate.close()
    asyncio.run(run())


def test_slow_recipient_does_not_delay_other_recipient():
    async def run():
        gate = ReliableTelegramPasswordGate(None)
        gate.authorized.update({1, 2})
        blocked = asyncio.Event()
        delivered = asyncio.Event()
        async def update(uid, ending=None):
            if uid == 1:
                await blocked.wait()
            else:
                delivered.set()
        gate._update_streak_notice = update
        queue = NotificationQueue(gate)
        try:
            await queue.submit([1, 2], ['1.2x'] * 4)
            await asyncio.wait_for(delivered.wait(), .2)
        finally:
            blocked.set()
            await queue.close()
    asyncio.run(run())


def test_notification_is_submitted_before_slow_demo_processing():
    async def run():
        gate = ReliableTelegramPasswordGate(None)
        gate.authorized.add(1)
        delivered = asyncio.Event()
        release = asyncio.Event()
        async def update(uid, ending=None):
            delivered.set()
        async def demo(uid, value):
            await release.wait()
        gate._update_streak_notice = update
        gate._handle_demo_round = demo
        gate.notification_queue = NotificationQueue(gate)
        task = asyncio.create_task(gate.on_round('1.2x'))
        try:
            await asyncio.wait_for(delivered.wait(), .2)
        finally:
            release.set()
            await task
            await gate.close()
    asyncio.run(run())
