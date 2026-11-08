"""Independent Strategy 4: entry signal, fixed autobet, safe loss cap, persistence."""
import asyncio
import json
from datetime import datetime, timezone
from fakeredis.aioredis import FakeRedis

from luckyjet_signal.demo_ledger import DemoLedger
from demo_persistence import DemoPersistence, ARCHIVE_CYCLES, RECOVERY_COMPLETED
from luckyjet_reliable_gate import ReliableTelegramPasswordGate
from mini_app_controls import controls
from mini_app_history import history
from mini_app_finance import demo_finance
from recovery_streak_notifications import normalize, changed_events, format_event
from test_archive_four_tabs import signed

NOW = datetime(2026, 11, 8, tzinfo=timezone.utc)


def tick(ledger, n, x='1.10'):
    result, changed = ledger.process_archived_round(
        f'r-{n}', str(x), now=NOW, stream_id=f'{n * 1000}-0')
    assert changed
    return result


def new_ledger(*, x='1.32', stake=150, balance=70185, trigger=3, safe=2):
    return DemoLedger(currency='KRW', initial_balance_cents=balance*100,
        balance_cents=balance*100, base_stake_cents=10000,
        recovery_streak={'enabled': True, 'target': x, 'trigger': trigger,
                         'stake_cents': stake*100, 'phase': 'waiting', 'safe_losses':safe})


def test_two_losses_safe_closes_session_and_new_entry_is_required():
    ledger = new_ledger(stake=150, balance=1000, trigger=3, safe=2)
    for n in (1, 2, 3):
        tick(ledger,n)
    assert ledger.recovery_streak.phase == 'betting'
    assert not ledger.recovery_streak.rounds  # no bet on signal round
    tick(ledger,4,'1.10')
    assert ledger.recovery_streak.phase == 'betting'  # auto again next round
    assert ledger.recovery_streak.public('KRW')['losses'] == 1
    assert ledger.balance_cents == 850*100
    tick(ledger,5,'1.12')
    assert ledger.recovery_streak.phase == 'waiting'
    assert ledger.recovery_streak.enabled   # safe is session reset, not OFF
    assert ledger.recovery_streak.streak == 0
    assert not ledger.recovery_streak.rounds
    first = ledger.recovery_history[0]
    assert first['outcome'] == 'safe_limit'
    assert first['safe_losses'] == 2
    assert first['net_cents'] == -300*100
    assert first['total_stake_cents'] == 300*100
    assert [r['stake_cents'] for r in first['rounds']] == [150*100]*2
    tick(ledger,6,'1.20')
    tick(ledger,7,'1.40')  # interrupted entry signal, no bet
    assert ledger.recovery_streak.streak == 0
    tick(ledger,8,'1.10')
    tick(ledger,9,'1.10')
    assert len(ledger.recovery_history) == 1
    tick(ledger,10,'1.10')
    assert ledger.recovery_streak.phase == 'betting'
    tick(ledger,11,'1.32')
    assert ledger.recovery_history[0]['outcome'] == 'recovered'
    assert ledger.recovery_history[0]['net_cents'] == 48*100
    assert ledger.recovery_streak.enabled
    assert ledger.balance_cents == 748*100
    assert DemoLedger(**ledger.dump()).balance_cents == ledger.balance_cents

def test_each_stake_is_fixed_with_auto_bet_even_when_net_loss_is_not_recovered():
    for stake in (1, 150, 300, 1000):
        ledger = new_ledger(stake=stake, balance=10_000_000, safe=2)
        for n in (1, 2, 3):
            tick(ledger,n)
        tick(ledger,4,'1.10')
        assert ledger.recovery_streak.phase == 'betting'
        tick(ledger,5,'1.32')
        cycle = ledger.recovery_history[0]
        payout = ledger.recovery_streak.payout(stake*100,'KRW')
        assert cycle['outcome'] == 'recovered'
        assert cycle['session_mode'] == 'autobet'
        assert cycle['stake_mode'] == 'fixed'
        assert [r['stake_cents'] for r in cycle['rounds']] == [stake*100]*2
        assert cycle['net_cents'] == payout - 2*stake*100
        assert cycle['payout_cents'] == cycle['total_stake_cents'] + cycle['net_cents']
        assert ledger.balance_cents == 10_000_000*100 + cycle['net_cents']
        assert ledger.recovery_streak.phase == 'waiting'

def test_first_win_is_only_stake_times_xminusone():
    ledger = new_ledger(stake=150)
    for n in (1,2,3):
        tick(ledger,n)
    tick(ledger,4,'1.32')
    assert ledger.recovery_history[0]['net_cents'] == 48*100
    assert ledger.balance_cents == (70185+48)*100


def test_streak_is_strictly_below_selected_x_and_entry_next_round():
    ledger = new_ledger(x='1.43', stake=300)
    tick(ledger,1,'1.42')
    tick(ledger,2,'1.43')  # Exact X resets the entry streak.
    assert ledger.recovery_streak.streak == 0
    tick(ledger,3,'1.10')
    tick(ledger,4,'1.42')
    assert ledger.recovery_streak.streak == 2
    tick(ledger,5,'1.30')
    assert ledger.recovery_streak.phase == 'betting'
    assert not ledger.recovery_streak.rounds
    tick(ledger,6,'1.43')
    assert ledger.recovery_history[0]['net_cents'] == 129*100
    assert len(ledger.recovery_history[0]['rounds']) == 1


def test_x138_step_four_fixed_1000_auto_win_on_immediate_following_round():
    ledger = new_ledger(x='1.38',stake=1000,balance=100000,trigger=4,safe=2)
    for n in (1,2,3,4):
        tick(ledger,n,'1.20')
    assert ledger.recovery_streak.phase == 'betting'
    tick(ledger,5,'1.20')
    assert ledger.recovery_streak.next_stake_cents == 1000*100
    assert ledger.recovery_streak.phase == 'betting'
    restored = DemoLedger(**ledger.dump())
    assert restored.recovery_streak.phase == 'betting'
    assert restored.recovery_streak.public('KRW')['losses'] == 1
    tick(restored,6,'1.38')
    cycle = restored.recovery_history[0]
    assert [r['stake_cents']//100 for r in cycle['rounds']] == [1000,1000]
    assert cycle['net_cents'] == -620*100
    assert restored.balance_cents == 99380*100
    assert restored.recovery_streak.phase == 'waiting'

def test_strategy4_accepts_x_above_149_and_legacy_session_migrates_without_deletion():
    ledger = new_ledger(x='3.00',stake=1000,trigger=4)
    for n in range(1,5):
        tick(ledger,n,'1.20')
    tick(ledger,5,'3.00')
    assert ledger.recovery_history[0]['net_cents'] == 2000*100

    migrated = new_ledger(x='1.38',stake=1000).dump()
    migrated['recovery_streak'].update(
        phase='waiting', rounds=[{'stake_cents':100000,'payout_cents':0}],
        total_stake_cents=100000,next_stake_cents=363200)
    migrated['recovery_streak'].pop('session_autobet',None)
    restored = DemoLedger(**migrated)
    assert restored.recovery_streak.phase == 'betting'  # new autobet
    assert restored.recovery_streak.streak == 0
    assert restored.recovery_streak.next_stake_cents == 100000
    assert restored.recovery_streak.rounds[0]['stake_cents'] == 100000
    assert not restored.recovery_streak.session_autobet

def test_four_strategies_simultaneously_share_one_balance_without_double_spending():
    params = dict(
        currency='KRW', balance_cents=200000, initial_balance_cents=200000,
        enabled=True, active=True, next_stake_cents=10000,
        base_stake_cents=10000,
        high_gap={'enabled':True,'phase':'betting','stake_cents':10000,
                  'reference':'100','target':'100'},
        target_streak={'enabled':True,'phase':'betting','stake_cents':10000,
                       'target':'1.32','next_stake_cents':10000,'trigger':3},
        recovery_streak={'enabled':True,'phase':'betting','stake_cents':15000,
                         'target':'1.32','next_stake_cents':15000,'trigger':3},
    )
    ledger = DemoLedger(**params)
    tick(ledger,1,'1.10')
    assert ledger.balance_cents == 200000 - 45000
    assert len(ledger.cycle_rounds) == 1
    assert len(ledger.high_gap.rounds) == 1
    assert len(ledger.target_streak.rounds) == 1
    assert len(ledger.recovery_streak.rounds) == 1
    assert ledger.balance_cents >= 0
    # While #3 is waiting, it must NOT reserve capital needed by #4.
    waiting = dict(params, balance_cents=120000, initial_balance_cents=120000,
                   enabled=False,
                   high_gap={'enabled':False},
                   target_streak={'enabled':True,'phase':'waiting','stake_cents':10000,
                                  'next_stake_cents':120000,'target':'1.32','trigger':3})
    waiting['recovery_streak']={'enabled':True,'phase':'betting',
                                'stake_cents':15000,'target':'1.32',
                                'next_stake_cents':15000}
    second = DemoLedger(**waiting)
    tick(second,1,'1.10')
    assert len(second.recovery_streak.rounds) == 1
    assert second.balance_cents == 105000


def test_funding_priority_preserves_existing_three_strategies():
    ledger = DemoLedger(
        currency='KRW', balance_cents=40000, enabled=True, active=True,
        next_stake_cents=10000, base_stake_cents=10000,
        high_gap={'enabled':True,'phase':'betting','stake_cents':10000,'target':'100'},
        target_streak={'enabled':True,'phase':'betting','stake_cents':10000,
                       'next_stake_cents':10000,'target':'1.32'},
        recovery_streak={'enabled':True,'phase':'betting','stake_cents':15000,
                         'next_stake_cents':15000,'target':'1.32'},
    )
    tick(ledger,1)
    assert ledger.balance_cents == 10000
    # Nothing was wagered by #4: no empty cycle may be archived.
    assert ledger.recovery_history == []
    assert not ledger.recovery_streak.enabled
    assert ledger.recovery_streak.phase == 'paused_funds'


def test_dedup_reset_archived_history_and_finance_no_reset_incomes():
    async def run():
        redis = FakeRedis(decode_responses=True)
        ledger = new_ledger(safe=2)
        for n in (1,2,3):
            tick(ledger,n)
        tick(ledger,4,'1.10')
        tick(ledger,5,'1.10')  # safe closes first session
        assert ledger.recovery_history[0]['outcome'] == 'safe_limit'
        for n in (6,7,8):
            tick(ledger,n,'1.10')
        tick(ledger,9,'1.32')  # next session wins
        assert len(ledger.recovery_history) == 2
        store = DemoPersistence()
        await store.save(redis,42,ledger)
        assert len(json.loads(await redis.hget(RECOVERY_COMPLETED,'42'))) == 2
        assert await redis.zcard(ARCHIVE_CYCLES+'42') == 2
        state = json.loads(await redis.hget('luckyjet:demo_sessions','42'))
        restored = {42:DemoLedger(**state)}
        await DemoPersistence().restore(redis,restored)
        assert restored[42].recovery_history == ledger.recovery_history
        before = await demo_finance(redis,42,restored[42])
        assert before['total'] == 3
        assert before['totals'][0]['net_cents'] == -25200
        restored[42].reset(currency='RUB',balance_cents=10000000,base_stake_cents=10000)
        await store.save(redis,42,restored[42])
        after = await demo_finance(redis,42,restored[42])
        assert after['total'] == 3
        assert after['totals'] == before['totals']
        assert await redis.zcard(ARCHIVE_CYCLES+'42') == 2
        assert restored[42].recovery_streak.enabled
        assert restored[42].recovery_streak.safe_losses == 2
        await redis.aclose()
    asyncio.run(run())

def test_controls_history_notices_and_auth_isolation():
    async def run():
        gate=ReliableTelegramPasswordGate(None)
        gate.redis=FakeRedis(decode_responses=True)
        gate.authorized.add(42)
        gate.authorized.add(43)
        ledger=new_ledger()
        gate.demo_ledgers[42]=ledger
        ledger.recovery_streak.enabled=False
        ledger.last_archived_stream_id='5000-0'
        for bad_x in ('1.00','10000.01','NaN'):
            bad=await controls(signed(42),gate,'fixture','POST',json.dumps({
                'action':'recovery_streak_config','value':{
                    'target':bad_x,'trigger':3,'stake':'150'}}).encode())
            assert bad[0] != '200 OK'
        good=await controls(signed(42),gate,'fixture','POST',json.dumps({
            'action':'recovery_streak_config',
            'value':{'target':'2.00','trigger':3,'stake':'150'}}).encode())
        assert good[0]=='200 OK'
        assert json.loads(good[2])['recovery_streak']['target']=='2.00'
        enabled=await controls(signed(42),gate,'fixture','POST',
                               json.dumps({'action':'recovery_streak_toggle'}).encode())
        assert enabled[0]=='200 OK'
        assert ledger.recovery_streak.enabled
        notice=await controls(signed(42),gate,'fixture','POST',
                              json.dumps({'action':'recovery_notice_event','value':'loss'}).encode())
        assert notice[0]=='200 OK'
        assert json.loads(notice[2])['recovery_streak_notifications']['events']['loss']
        assert await gate.redis.hexists('luckyjet:recovery_streak_notices:v1','42')
        ledger.recovery_streak.reset_progress()
        for n in (6,7,8):
            tick(ledger,n)
        tick(ledger,9,'2.00')
        result=await history(signed(42),gate,'fixture','view=recovery_cycles')
        assert result[0]=='200 OK'
        data=json.loads(result[2])
        assert data['stats']['total']==1
        assert data['items'][0]['strategy']=='recovery_streak'
        detailed=await history(signed(42),gate,'fixture','view=recovery_cycle&index=0')
        assert json.loads(detailed[2])['item']['rounds']
        isolation=await history(signed(43),gate,'fixture','view=recovery_cycles')
        assert json.loads(isolation[2])['total']==0
        await gate.close()
    asyncio.run(run())


def test_source_gap_pauses_strategy4_and_marks_balance_unverified():
    async def run():
        gate=ReliableTelegramPasswordGate(None)
        gate.redis=FakeRedis(decode_responses=True)
        gate.authorized.add(42)
        gate.demo_ledgers[42]=new_ledger()
        for n in (1,2,3,4):
            await gate._handle_demo_round(42,'1.10',round_id=str(n),stream_id=f'{n}-0')
        assert gate.demo_ledgers[42].recovery_streak.rounds
        await gate.on_history_gap(stream_id='5-0',silent=True)
        ledger=gate.demo_ledgers[42]
        assert not ledger.recovery_streak.enabled
        assert ledger.recovery_streak.phase=='paused_gap'
        assert ledger.recovery_history[0]['net_cents'] is None
        assert not ledger.balance_verified
        await gate.close()
    asyncio.run(run())


def test_notifications_are_independent_from_strategy3():
    prefs=normalize()
    assert prefs['events']=={'armed':True,'betting':True,'loss':False,
                              'goal':True,'safe':True,'stopped':True}
    before={'enabled':True,'phase':'waiting','steps':0}
    after={'enabled':True,'phase':'betting','steps':0}
    assert changed_events(before,after)==['armed']
    assert 'Стратегия №4' in format_event('armed',{'target':'1.32',
         'trigger':3,'stake_cents':15000},'KRW')
    first_loss={'enabled':True,'phase':'betting','steps':1}
    assert changed_events(after,first_loss)==['betting','loss']
    safe_cycle={'outcome':'safe_limit','net_cents':-30000,'losses':2,
                'rounds':[{'stake_cents':15000,'payout_cents':0},
                          {'stake_cents':15000,'payout_cents':0}]}
    assert changed_events(first_loss,{'enabled':True,'phase':'waiting','steps':0},
                          safe_cycle)==['loss','safe']
    assert 'сейф сработал' in format_event('safe',
        {'target':'1.32','trigger':3,'stake_cents':15000},'KRW',safe_cycle)

def test_auto_1000_krw_five_entry_safe_two_then_new_session():
    ledger = new_ledger(x='1.33',stake=1000,balance=10000,trigger=5,safe=2)
    for n in range(1,6):
        tick(ledger,n,'1.20')
    assert ledger.recovery_streak.phase == 'betting'
    assert ledger.balance_cents == 10000*100
    tick(ledger,6,'1.15')
    assert ledger.recovery_streak.phase == 'betting'
    tick(ledger,7,'1.20')
    safe_cycle = ledger.recovery_history[0]
    assert safe_cycle['outcome'] == 'safe_limit'
    assert safe_cycle['net_cents'] == -2000*100
    assert [r['stake_cents'] for r in safe_cycle['rounds']] == [1000*100]*2
    assert ledger.recovery_streak.enabled
    assert ledger.recovery_streak.phase == 'waiting'
    assert ledger.recovery_streak.streak == 0
    # A high round interrupts the NEW first-entry count.
    tick(ledger,8,'1.40')
    for n in range(9,14):
        tick(ledger,n,'1.10')
    assert ledger.recovery_streak.phase == 'betting'
    assert len(ledger.recovery_history) == 1
    tick(ledger,14,'1.33')
    win_cycle = ledger.recovery_history[0]
    assert win_cycle['outcome'] == 'recovered'
    assert win_cycle['net_cents'] == 330*100
    assert win_cycle['rounds'][0]['stake_cents'] == 1000*100
    assert ledger.balance_cents == (10000-2000+330)*100
    assert sum(x['net_cents'] for x in ledger.recovery_history) == -1670*100

def test_existing_active_old_raised_stake_migrates_and_safe_closes_previously_lost_bets():
    old_first = 10000*100
    old_raised = 22141*100
    initial = new_ledger(x='1.38',stake=10000,balance=60000,trigger=2,safe=2)
    snap = initial.dump()
    snap['recovery_streak'].update({
        'phase':'waiting',
        'rounds':[{'round_id':'old-1','coefficient':'1.10',
                   'stake_cents':old_first,'payout_cents':0},
                  {'round_id':'old-2','coefficient':'1.10',
                   'stake_cents':old_raised,'payout_cents':0}],
        'total_stake_cents':old_first+old_raised,
        'next_stake_cents':99999*100,
    })
    snap['recovery_streak'].pop('session_autobet',None)
    migrated = DemoLedger(**snap)
    strategy = migrated.recovery_streak
    assert strategy.enabled
    assert strategy.phase == 'betting'
    assert strategy.next_stake_cents == old_first
    assert [r['stake_cents'] for r in strategy.rounds] == [old_first,old_raised]
    restored = DemoLedger(**migrated.dump())
    tick(restored,1,'1.10')  # close prior session, do NOT spend
    assert restored.recovery_history[0]['outcome'] == 'safe_limit'
    assert restored.recovery_history[0]['stake_mode'] == 'legacy_variable'
    assert restored.recovery_history[0]['session_mode'] == 'legacy_migrated'
    assert [r['stake_cents'] for r in restored.recovery_history[0]['rounds']] == [
        old_first,old_raised]
    assert restored.recovery_streak.enabled
    assert restored.recovery_streak.phase == 'waiting'
    tick(restored,2,'1.10')
    tick(restored,3,'1.10')
    tick(restored,4,'1.38')
    assert restored.recovery_history[0]['rounds'][0]['stake_cents'] == old_first
    assert restored.recovery_history[0]['session_mode'] == 'autobet'

def test_korean_payout_exact_and_no_wager_on_trigger_round():
    ledger = new_ledger(x='1.38', stake=10000, balance=10000, trigger=2)
    tick(ledger,1,'1.05')
    tick(ledger,2,'1.05')
    assert ledger.balance_cents == 10000 * 100
    assert ledger.recovery_streak.rounds == []
    tick(ledger,3,'1.38')
    assert ledger.recovery_history[0]['rounds'][0]['stake_cents'] == 10000 * 100
    assert ledger.recovery_history[0]['payout_cents'] == 13800 * 100
    assert ledger.recovery_history[0]['net_cents'] == 3800 * 100
