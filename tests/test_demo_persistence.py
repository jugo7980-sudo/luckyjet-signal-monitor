import asyncio
import json
from demo_persistence import DemoPersistence
from luckyjet_signal.demo_ledger import DemoLedger


class Redis:
    def __init__(self): self.data = {}; self.writes = []; self.fail = False
    def pipeline(self):
        owner = self
        class Pipe:
            def __init__(self): self.ops = []
            def hset(self, key, uid, raw): self.ops.append((key, uid, raw))
            async def execute(self):
                if owner.fail: raise RuntimeError('offline')
                for key, uid, raw in self.ops:
                    owner.data[key, uid] = raw
                    owner.writes.append(key)
        return Pipe()
    async def hgetall(self, key): return {uid: raw for (k, uid), raw in self.data.items() if k == key}


def test_split_storage_migrates_legacy_history_and_restores_without_rewriting_cycles():
    async def run():
        redis = Redis()
        store = DemoPersistence()
        ledger = DemoLedger(enabled=True, trigger_streak=3)
        for value in ['1.2'] * 3 + ['2.0']: ledger.process_round(value)
        assert len(ledger.history) == 1
        await store.save(redis, 42, ledger)
        ledger.process_round('1.1')
        await store.save(redis, 42, ledger)
        assert redis.writes.count('luckyjet:demo_completed_cycles') == 1
        state = json.loads(redis.data['luckyjet:demo_sessions', '42'])
        assert 'history' not in state
        restored = {42: DemoLedger(**state)}
        await DemoPersistence().restore(redis, restored)
        assert restored[42].history == ledger.history
        assert restored[42].balance_cents == ledger.balance_cents
    asyncio.run(run())
