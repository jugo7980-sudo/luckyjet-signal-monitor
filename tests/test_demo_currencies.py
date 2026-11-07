import asyncio
import pytest
from luckyjet_signal.demo_ledger import DemoLedger
from mini_app_history import _demo_stats, _cycle

@pytest.mark.parametrize('currency,balance,stake', [('USD', 100, 1), ('RUB', 500000, 100), ('KRW', 9000000, 50000)])
def test_manual_reset_restart_and_currency_history(currency, balance, stake):
    ledger = DemoLedger(enabled=True, trigger_streak=2, last_archived_round_id='old', last_archived_stream_id='10-0')
    ledger.reset(currency=currency, balance_cents=balance, base_stake_cents=stake)
    assert ledger.currency == currency
    assert _demo_stats(ledger)['since_reset_cents'] == 0
    assert ledger.last_archived_stream_id == '10-0'
    ledger = DemoLedger(**ledger.dump())
    assert ledger.initial_balance_cents == balance and ledger.base_stake_cents == stake
    ledger.process_round(1.2); ledger.process_round(1.2); ledger.process_round(2)
    assert ledger.balance_cents == balance + stake
    assert ledger.history[0]['currency'] == currency
    assert _cycle(ledger.history[0], 0)['currency'] == currency
    assert _demo_stats(ledger)['currency'] == currency
    assert _demo_stats(ledger)['since_reset_cents'] == stake

@pytest.mark.parametrize('currency,balance,stake', [('EUR',1000,100), ('KRW',101,100), ('RUB',100,1), ('USD',100,101), ('USD',0,1)])
def test_invalid_reset_leaves_old_account(currency,balance,stake):
    ledger=DemoLedger(); old=ledger.dump()
    with pytest.raises(ValueError): ledger.reset(currency=currency,balance_cents=balance,base_stake_cents=stake)
    assert ledger.dump() == old

def test_insufficient_balance_stops_without_changing_stake():
    ledger=DemoLedger(enabled=True,trigger_streak=2)
    ledger.reset(currency='KRW',balance_cents=1000,base_stake_cents=500)
    ledger.process_round(1.2); ledger.process_round(1.2)
    event=ledger.process_round(1.2)
    assert event['type']=='insufficient_balance'
    assert ledger.enabled is False and ledger.active is False
    assert ledger.base_stake_cents==500 and ledger.balance_cents==500

def test_legacy_account_keeps_dollars_and_original_baseline():
    ledger=DemoLedger(balance_cents=0)
    assert ledger.currency=='USD' and ledger.initial_balance_cents==1000000

from fakeredis.aioredis import FakeRedis
from test_mini_app_controls import signed
from mini_app_controls import controls
from luckyjet_reliable_gate import ReliableTelegramPasswordGate
import json


def test_reset_api_validation_idempotency_storage_and_isolation():
    async def run():
        gate=ReliableTelegramPasswordGate(None)
        gate.authorized.update({42,43}); gate.redis=FakeRedis(decode_responses=True)
        async def request(value):
            status,_,body=await controls(signed(),gate,'fixture','POST',json.dumps({'action':'demo_reset','confirm':True,'value':value}).encode())
            return status,json.loads(body)
        old=gate._demo_ledger(42).dump()
        for value in [None,{}, {'currency':'KRW','balance':'100.50','stake':'1','revision':'legacy'}]:
            assert (await request(value))[0]=='400 Bad Request'
            assert gate._demo_ledger(42).dump()==old
        payload={'currency':'RUB','balance':'50000','stake':'25,50','revision':'legacy'}
        status,data=await request(payload)
        assert status=='200 OK',data
        assert data['demo']['currency']=='RUB' and data['demo']['balance_cents']==5000000
        assert data['demo']['base_stake_cents']==2550
        assert (await request(payload))[0]=='409 Conflict'
        assert gate._demo_ledger(43).currency=='USD'
        raw=json.loads(await gate.redis.hget('luckyjet:demo_sessions','42'))
        assert raw['currency']=='RUB' and raw['initial_balance_cents']==5000000
        await gate.redis.aclose(); gate.redis=None; await gate.close()
    asyncio.run(run())


def test_reset_skips_old_archive_bets_but_preserves_source_statistics():
    ledger=DemoLedger(enabled=True,trigger_streak=2)
    ledger.reset(currency='RUB',balance_cents=100000,base_stake_cents=100)
    ledger.demo_after_stream_id='50-0'
    for i in range(1,5):
        ledger.process_archived_round(str(i),1.2,stream_id=f'{i}-0')
    assert ledger.balance_cents==100000 and not ledger.active
    assert ledger.consecutive_below_two==4
    ledger.process_archived_round('51',1.2,stream_id='51-0')
    ledger.process_archived_round('52',1.2,stream_id='52-0')
    assert ledger.active


def test_reset_survives_pending_round_batch_save():
    async def run():
        gate = ReliableTelegramPasswordGate(None)
        gate.authorized.add(42)
        gate.redis = FakeRedis(decode_responses=True)
        processed, resume = asyncio.Event(), asyncio.Event()
        handle = gate._handle_demo_round

        async def pause_after_round(*args, **kwargs):
            result = await handle(*args, **kwargs)
            processed.set()
            await resume.wait()
            return result

        gate._handle_demo_round = pause_after_round
        pending = asyncio.create_task(gate.on_round(
            1.2, round_id='r1', stream_id='10-0', silent=True))
        try:
            await asyncio.wait_for(processed.wait(), 2)
            payload = {'action': 'demo_reset', 'confirm': True, 'value': {
                'currency': 'RUB', 'balance': '500', 'stake': '1',
                'revision': 'legacy'}}
            status, _, body = await controls(
                signed(), gate, 'fixture', 'POST', json.dumps(payload).encode())
            assert status == '200 OK', body
            revision = gate._demo_ledger(42).reset_revision
            resume.set()
            await asyncio.wait_for(pending, 2)
            raw = json.loads(await gate.redis.hget('luckyjet:demo_sessions', '42'))
            assert raw['currency'] == 'RUB'
            assert raw['balance_cents'] == 50000
            assert raw['reset_revision'] == revision
            assert json.loads(await gate.redis.hget(
                'luckyjet:demo_completed_cycles', '42')) == []
        finally:
            resume.set()
            await pending
            await gate.redis.aclose()
            gate.redis = None
            await gate.close()
    asyncio.run(run())
