import asyncio
from datetime import datetime
from coefficient_stats import CoefficientStats, event_time,TZ
from luckyjet_reliable_gate import ReliableTelegramPasswordGate

class Redis:
 def __init__(self):self.seen=set();self.count=0;self.calls=0
 async def eval(self,script,n,*args):
  self.calls+=1
  if args[0] in self.seen:return 0
  self.seen.add(args[0]);self.count+=1;return 1


def test_repeated_round_id_not_counted_twice_even_after_collector_restart():
 async def run():
  r=Redis();a=CoefficientStats(r)
  stamp=datetime(2026,11,3,tzinfo=TZ).timestamp()
  assert await a.record(100,round_id='abc',timestamp=stamp)
  assert not await CoefficientStats(r).record(100,round_id='abc',timestamp=stamp)
  assert r.count==1
 asyncio.run(run())


def test_event_time_and_midnight():
 a=event_time({'finishedAt':'2026-11-02T16:59:59Z'})
 b=event_time({'finishedAt':'2026-11-02T17:00:00Z'})
 assert a.strftime('%Y-%m-%d')=='2026-11-02'
 assert b.strftime('%Y-%m-%d')=='2026-11-03'
 assert event_time({'timestamp':'invalid'}) is None


def test_invalid_buttons_rejected():
 gate=ReliableTelegramPasswordGate(None)
 for data in ['coef_day:0','coef_day:2026-99-99:10:0','coef_filter:0','coef_days:-1']:
  assert not gate._supported_callback(data,1)
 assert gate._supported_callback('coef_days:2',1)
 assert gate._supported_callback('coef_day:2026-11-03:10:0',1)
