import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from datetime import datetime, timezone
from luckyjet_signal.demo_ledger import DemoLedger


def test_daily_max_survives_cycle_end_restart_and_reset():
    ledger=DemoLedger(enabled=True, trigger_streak=2)
    for value in [1,1,1,1,2]: ledger.process_round(value)
    day=datetime.now(timezone.utc).astimezone(__import__('datetime').timezone(__import__('datetime').timedelta(hours=7))).date().isoformat()
    assert ledger.daily_max_steps[day] == 3
    restored=DemoLedger(**ledger.dump())
    assert restored.daily_max_steps == ledger.daily_max_steps
    restored.reset()
    assert restored.daily_max_steps == {}


def test_midnight_uses_thai_day_and_full_session_step_number():
    ledger=DemoLedger(enabled=True,active=True)
    ledger.process_round(1, now=datetime(2026,11,6,16,59,tzinfo=timezone.utc))
    ledger.process_round(2, now=datetime(2026,11,6,17,1,tzinfo=timezone.utc))
    assert ledger.daily_max_steps == {'2026-11-06':2,'2026-11-07':2}


def test_navigation_date_input_reset_and_persistence():
    import asyncio
    import fakeredis.aioredis
    from test_intuitive_ui import setup, click, screen
    from demo_persistence import DemoPersistence
    async def run():
        gate, request = setup()
        ledger=gate._demo_ledger(42)
        ledger.daily_max_low_streak['2026-01-02']=7
        ledger.daily_max_steps['2026-01-02']=99  # Old DEMO metric must not appear in source streaks.
        await click(gate,'demo_menu')
        mid=gate.menu_messages[42]
        await click(gate,'demo_daily:2026-01-02')
        assert 'Максимальный шаг: 7' in screen(request)['text']
        assert '$' not in screen(request)['text']
        await click(gate,'demo_daily_date')
        await gate._demo_input(42,42,'02.01.2026',20)
        assert 'Максимальный шаг: 7' in screen(request)['text']
        assert gate.menu_messages[42] == mid
        redis=fakeredis.aioredis.FakeRedis(decode_responses=True)
        storage=DemoPersistence()
        await storage.save(redis,42,ledger)
        import json
        restored=DemoLedger(**json.loads(await redis.hget('luckyjet:demo_sessions','42')))
        assert restored.daily_max_low_streak['2026-01-02']==7
        assert restored.daily_max_steps['2026-01-02']==99
        await click(gate,'demo_reset'); await click(gate,'demo_reset_confirm')
        assert ledger.daily_max_steps == {}
        assert ledger.daily_max_low_streak['2026-01-02'] == 7
        await redis.aclose(); await gate.close()
    asyncio.run(run())


def test_legacy_active_and_stopped_cycles_keep_reached_step():
    now=datetime.now(timezone.utc).isoformat()
    ledger=DemoLedger(active=True,cycle_started_at=now,cycle_rounds=[{},{}])
    assert max(ledger.daily_max_steps.values()) == 3
    stopped=DemoLedger(history=[{'started_at':now,'ended_at':now,'rounds':[{},{}],'outcome':'stopped_by_user'}])
    assert max(stopped.daily_max_steps.values()) == 3


def test_legacy_backfill_reads_all_available_history_before_truncation():
    entry={'started_at':'2026-01-01T01:00:00+00:00','ended_at':'2026-01-01T02:00:00+00:00','rounds':[{}]}
    ledger=DemoLedger(history=[{}]*100+[entry])
    assert ledger.daily_max_steps['2026-01-01']==1
