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
