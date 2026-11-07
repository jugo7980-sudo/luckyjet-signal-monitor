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
 a=event_time({'finishedAt':'2026-11-02T13:59:59Z'})
 b=event_time({'finishedAt':'2026-11-02T14:00:00Z'})
 assert a.strftime('%Y-%m-%d')=='2026-11-02'
 assert b.strftime('%Y-%m-%d')=='2026-11-03'
 assert event_time({'timestamp':'invalid'}) is None


def test_invalid_buttons_rejected():
 gate=ReliableTelegramPasswordGate(None)
 for data in ['coef_day:0','coef_day:2026-99-99:10:0','coef_filter:0','coef_days:-1']:
  assert not gate._supported_callback(data,1)
 assert gate._supported_callback('coef_days:2',1)
 assert gate._supported_callback('coef_day:2026-11-03:10:0',1)


def test_day_page_uses_one_transaction_and_preserves_page_bounds():
 class Pipeline:
  def __init__(self): self.commands=[]; self.executions=0
  def zcard(self,*args): self.commands.append(('count',args))
  def zrevrange(self,*args): self.commands.append(('page',args))
  async def execute(self):
   self.executions+=1
   return [21, [b'2026-11-02', b'2026-11-01']]
 class Store:
  def __init__(self): self.pipe=Pipeline()
  def pipeline(self): return self.pipe
 async def run():
  store=Store()
  days,total=await CoefficientStats(store).days(1)
  assert days==['2026-11-02','2026-11-01'] and total==21
  assert store.pipe.executions==1
  assert [name for name,_ in store.pipe.commands]==['count','page']
  assert store.pipe.commands[-1][1]==('luckyjet:coef:days',10,19)
 asyncio.run(run())


def test_less_than_two_counter_and_screen():
 from coefficient_stats import SCRIPT
 increment="if tonumber(ARGV[1]) < 2 then redis.call('HINCRBY', KEYS[2], 'lt2', 1) end"
 assert SCRIPT.count(increment) == 1
 assert "lt2_tracking" in SCRIPT
 assert "lt2_partial" in SCRIPT

 class Stats:
  redis=object()
  def __init__(self, summary): self.value=summary
  async def summary(self, day): return self.value

 async def render(summary):
  gate=ReliableTelegramPasswordGate(None)
  gate.coefficient_stats=Stats(summary)
  text,_=await gate._coefficient_screen('coef_day:2026-11-03:10:0')
  return text

 def under2_lines(text):
  return [line for line in text.splitlines() if line.startswith('<2x')]

 async def run():
  partial=await render({'total':100,'lt2':42,'lt2_tracking':1,'lt2_partial':1,'10':7})
  assert under2_lines(partial) == ['<2x с обновления: 42']
  full=await render({'total':100,'lt2':42,'lt2_tracking':1,'10':7})
  assert under2_lines(full) == ['<2x: 42']
  legacy=await render({'total':100,'10':7})
  assert under2_lines(legacy) == ['<2x: нет данных']
 asyncio.run(run())


def test_bootstrap_repairs_initial_double_count_once():
 from coefficient_stats import UNDER2_DOUBLE_FIX, UNDER2_FIX_DAY, UNDER2_FIX_MARKER
 assert "math.floor(count / 2)" in UNDER2_DOUBLE_FIX
 assert "EXISTS', KEYS[1]" in UNDER2_DOUBLE_FIX

 class Pipeline:
  def hget(self,*args): pass
  async def execute(self): return [None] * 11
 class Store:
  def __init__(self): self.eval_calls=[]
  async def eval(self,script,n,*args):
   self.eval_calls.append((script,n,args)); return 1
  def pipeline(self): return Pipeline()
  async def zadd(self,*args,**kwargs): raise AssertionError('zadd should not be called')
 async def run():
  store=Store()
  await CoefficientStats(store).bootstrap()
  assert store.eval_calls == [(UNDER2_DOUBLE_FIX, 2, (UNDER2_FIX_MARKER, f'luckyjet:coef:{UNDER2_FIX_DAY}'))]
 asyncio.run(run())


def test_daily_statistics_and_seen_rounds_no_longer_expire():
 from coefficient_stats import SCRIPT, UNDER2_DOUBLE_FIX
 assert "7948800" not in SCRIPT
 assert "7948800" not in UNDER2_DOUBLE_FIX
 assert "EXPIRE', KEYS[2]" not in SCRIPT
