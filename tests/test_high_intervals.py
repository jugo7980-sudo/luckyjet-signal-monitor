import asyncio
from datetime import datetime
import fakeredis.aioredis
from coefficient_stats import CoefficientStats,TZ


def test_cross_midnight_dedup_restart_and_strict_threshold():
 async def run():
  r=fakeredis.aioredis.FakeRedis(decode_responses=True); s=CoefficientStats(r)
  start=datetime(2026,11,6,23,59,tzinfo=TZ).timestamp()
  for i,v in enumerate([150,2,100,120]):
   await s.record(v,round_id=str(i),timestamp=start+i*60)
  assert not await CoefficientStats(r).record(120,round_id='3',timestamp=start+180)
  rows=await r.lrange('luckyjet:coef:high:2026-11-07',0,-1)
  import json
  entry=json.loads(await r.hget('luckyjet:coef:high:entries',rows[0]))
  assert entry['rounds']==3 and entry['value']=='120'
  assert int(await r.hget('luckyjet:coef:high:state','since'))==0
  assert (await s.summary('2026-11-07'))['2']==2
  await r.aclose()
 asyncio.run(run())


def test_gap_unknown_then_new_anchor_restores_exact_count():
 async def run():
  r=fakeredis.aioredis.FakeRedis(decode_responses=True);s=CoefficientStats(r)
  await s.record(150,round_id='a',timestamp=100)
  await s.record(2,round_id='b',timestamp=101,gap=True)
  await s.record(200,round_id='c',timestamp=102)
  import json
  import hashlib
  key=lambda x:hashlib.sha256(x.encode()).hexdigest()[:16]
  assert json.loads(await r.hget('luckyjet:coef:high:entries',key('c')))['rounds'] is None
  await CoefficientStats(r).record(300,round_id='d',timestamp=103)
  assert json.loads(await r.hget('luckyjet:coef:high:entries',key('d')))['rounds']==1
  await s.record(250,round_id='old',timestamp=99)
  assert (await r.hget('luckyjet:coef:high:state','last_id'))=='d'
  await r.aclose()
 asyncio.run(run())


def test_history_screens_static_navigation_and_new_threshold_coverage():
 from test_intuitive_ui import setup,click,screen
 async def run():
  gate,tg=setup();r=fakeredis.aioredis.FakeRedis(decode_responses=True)
  gate.coefficient_stats=CoefficientStats(r)
  stamp=datetime(2026,11,6,12,tzinfo=TZ).timestamp()
  for i,v in enumerate([150,4,7,120]):await gate.coefficient_stats.record(v,round_id=str(i),timestamp=stamp+i)
  await click(gate,'demo_menu');mid=gate.menu_messages[42]
  await click(gate,'hi_days:0');await click(gate,'hi_day:2026-11-06:0')
  buttons=screen(tg)['reply_markup']['inline_keyboard']
  case=buttons[0][0]
  assert case['text'].startswith('3 раундов')
  await click(gate,case['callback_data'])
  assert 'Предыдущее:' in screen(tg)['text']
  assert gate.menu_messages[42]==mid and gate.menu_deadlines[42]==0
  await click(gate,'menu_settings')
  assert gate.menu_deadlines[42]>0
  text,_=await gate._coefficient_screen('coef_day:2026-11-06:10:3')
  assert '>2x: 4' in text and '>5x: 3' in text
  assert (await gate._coefficient_screen('coef_day:2026-11-06:10:3'))[1]['inline_keyboard'][0][0]['callback_data']=='coef_days:3'
  await gate.close();await r.aclose()
 asyncio.run(run())


def test_ingest_skipped_timestamps_never_claims_exact_gap():
 async def run():
  import json
  r=fakeredis.aioredis.FakeRedis(decode_responses=True);s=CoefficientStats(r)
  a={'id':'A','coefficient':150}
  await s.ingest([a])
  await s.ingest([{'id':'D','coefficient':200},{'id':'C','coefficient':2},{'id':'B','coefficient':2},a])
  ids=await r.lrange('luckyjet:coef:high:'+s.day(),0,-1)
  assert json.loads(await r.hget('luckyjet:coef:high:entries',ids[0]))['rounds'] is None
  await r.aclose()
 asyncio.run(run())


def test_ordered_ids_survive_equal_or_earlier_event_time():
 async def run():
  import json
  r=fakeredis.aioredis.FakeRedis(decode_responses=True);s=CoefficientStats(r)
  a={'id':'a','coefficient':150,'timestamp':100}
  b={'id':'b','coefficient':200,'timestamp':100}
  c={'id':'c','coefficient':250,'timestamp':99}
  await s.ingest([a]);await s.ingest([b,a]);await s.ingest([c,b,a])
  ids=await r.lrange('luckyjet:coef:high:1970-01-01',0,-1)
  assert len(ids)==3
  entry=json.loads(await r.hget('luckyjet:coef:high:entries',ids[0]))
  assert entry['value']=='250' and entry['rounds']==1
  await r.aclose()
 asyncio.run(run())
