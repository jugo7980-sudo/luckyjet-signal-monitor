import asyncio

import pytest

from luckyjet_reliable_gate import ReliableTelegramPasswordGate


class Redis:
    def __init__(self):
        self.data = {}
        self.writes = 0
        self.fail = False

    def pipeline(self):
        owner = self

        class Pipeline:
            def __init__(self):
                self.ops = []

            def hset(self, key, uid, value):
                self.ops.append((key, uid, value))

            def hdel(self, key, uid):
                self.ops.append((key, uid, None))

            async def execute(self):
                owner.writes += 1
                await asyncio.sleep(0)
                if owner.fail:
                    raise RuntimeError('unavailable')
                for key, uid, value in self.ops:
                    if value is None:
                        owner.data.pop((key, uid), None)
                    else:
                        owner.data[key, uid] = value

        return Pipeline()


def make_gate():
    gate = ReliableTelegramPasswordGate(None)
    gate.redis = Redis()
    gate.menu_messages[42] = 55
    gate.menu_deadlines[42] = 100.0
    return gate


def test_unchanged_windows_are_not_written_repeatedly():
    async def run():
        gate = make_gate()
        for _ in range(100):
            await gate._persist_runtime_user(42)
        assert gate.redis.writes == 1
        assert gate.redis.data['luckyjet:menu_messages', '42'] == '55'
        gate.menu_deadlines[42] = 200.0
        await gate._persist_runtime_user(42)
        assert gate.redis.data['luckyjet:menu_deadlines', '42'] == '200.0'
        gate.menu_messages.pop(42)
        await gate._persist_runtime_user(42)
        assert ('luckyjet:menu_messages', '42') not in gate.redis.data
        assert ('luckyjet:menu_deadlines', '42') not in gate.redis.data
    asyncio.run(run())


def test_failed_write_is_retried():
    async def run():
        gate = make_gate()
        gate.redis.fail = True
        with pytest.raises(RuntimeError):
            await gate._persist_runtime_user(42)
        gate.redis.fail = False
        await gate._persist_runtime_user(42)
        assert gate.redis.data['luckyjet:menu_messages', '42'] == '55'
        assert gate.redis.writes == 2
    asyncio.run(run())


def test_concurrent_saves_coalesce_and_keep_changes_during_write():
    async def run():
        gate = make_gate()
        await asyncio.gather(*(gate._persist_runtime_user(42) for _ in range(20)))
        assert gate.redis.writes == 1
        gate.menu_messages[42] = 66
        task = asyncio.create_task(gate._persist_runtime_user(42))
        await asyncio.sleep(0)
        gate.menu_messages[42] = 77
        await gate._persist_runtime_user(42)
        await task
        assert gate.redis.data['luckyjet:menu_messages', '42'] == '77'
    asyncio.run(run())
