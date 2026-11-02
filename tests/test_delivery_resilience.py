import asyncio
import json
import time
import httpx
from telegram_delivery import TelegramDelivery, interactive
from notification_queue import NotificationQueue, KEY
from demo_persistence import DemoPersistence
from luckyjet_reliable_gate import ReliableTelegramPasswordGate
from luckyjet_signal.demo_ledger import DemoLedger


def test_retry_spacing_and_cache_bound():
    async def run():
        attempts={};times=[]
        async def request(m,p):
            uid=p['chat_id'];attempts[uid]=attempts.get(uid,0)+1
            if attempts[uid]==1:
                httpx.Response(429,json={'parameters':{'retry_after':.01}},request=httpx.Request('POST','https://test')).raise_for_status()
            times.append(time.monotonic());return {'ok':True}
        d=TelegramDelivery(request,interval=.005)
        await asyncio.gather(*(d('editMessageText',{'chat_id':i,'message_id':1}) for i in range(5)))
        assert all(b-a>=.004 for a,b in zip(times,times[1:]))
        async def ok(m,p):return {'ok':True}
        d=TelegramDelivery(ok,interval=0)
        for uid in range(2100):await d('editMessageText',{'chat_id':uid,'message_id':1})
        assert len(d._cache)==2000
    asyncio.run(run())


def test_click_priority_over_background():
    async def run():
        order=[]
        async def req(m,p):order.append(p['chat_id']);return {'ok':True}
        d=TelegramDelivery(req,interval=.01)
        tasks=[asyncio.create_task(d('editMessageText',{'chat_id':i,'message_id':i})) for i in range(30)]
        await asyncio.sleep(.003)
        token=interactive.set(True)
        try:await d('editMessageText',{'chat_id':999,'message_id':999})
        finally:interactive.reset(token)
        assert order.index(999)<5
        await asyncio.gather(*tasks)
    asyncio.run(run())


class Redis:
    def __init__(self):self.values={}
    def pipeline(self):
        owner=self
        class Pipe:
            def __init__(self):self.ops=[]
            def hset(self,key,uid,raw):self.ops.append((key,uid,raw))
            async def execute(self):
                for key,uid,raw in self.ops:owner.values[key,uid]=raw
        return Pipe()
    async def hgetall(self,key):return {uid:raw for (k,uid),raw in self.values.items() if k==key}
    async def hdel(self,key,uid):self.values.pop((key,uid),None)


def test_notice_backlog_coalesces_and_survives_restart():
    async def run():
        redis=Redis();gate=ReliableTelegramPasswordGate(None);gate.redis=redis;gate.authorized.add(1)
        release=asyncio.Event();started=asyncio.Event()
        async def stuck(*a,**kw):started.set();await release.wait()
        gate._update_streak_notice=stuck
        q=NotificationQueue(gate)
        await q.submit([1],['1.1x']);await started.wait()
        for i in range(200):await asyncio.wait_for(q.submit([1],[str(i)]),.2)
        assert len(q.pending)==1 and q.queue.qsize()<=1
        await q.close()
        seen=[]
        async def notice(uid,ending=None):seen.append(list(gate.current_streak))
        gate._update_streak_notice=notice
        q=NotificationQueue(gate);await q.restore();await q.queue.join()
        assert seen==[['199']]
        assert not await redis.hgetall(KEY)
        await q.close()
    asyncio.run(run())


def test_corrupt_history_does_not_block_other_users():
    async def run():
        redis=Redis();redis.values['luckyjet:demo_completed_cycles','1']='broken'
        redis.values['luckyjet:demo_completed_cycles','2']=json.dumps([{'outcome':'recovered'}])
        ledgers={1:DemoLedger(),2:DemoLedger()}
        await DemoPersistence().restore(redis,ledgers)
        assert ledgers[2].history==[{'outcome':'recovered'}]
    asyncio.run(run())
