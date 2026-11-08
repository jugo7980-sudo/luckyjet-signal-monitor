"""Regression coverage for isolated, source-backed Strategy 2 Telegram notices."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from fakeredis.aioredis import FakeRedis

from high_gap_notifications import changed_events, format_event, normalize
from luckyjet_password_gate import TelegramPasswordGate
from luckyjet_signal.high_gap_demo import HighGapStrategy


def test_strategy2_notice_events_follow_real_virtual_bets_only():
    strategy = HighGapStrategy(min_stake=1000, default_stake=1000)
    assert strategy.configure('100', 2, '100', 1000, min_stake=1000)
    strategy.enabled = True
    balance = 1_000_000

    def play(round_id, multiplier):
        nonlocal balance
        before = strategy.dump()
        balance, completed = strategy.on_round(multiplier, round_id=round_id,
                                                currency='USD', balance=balance)
        return changed_events(before, strategy.dump(), completed), completed

    events, done = play('anchor', '110')
    assert events == ['reference'] and done is None
    assert play('wait-one', '1.20')[0] == []
    assert play('wait-two', '1.30')[0] == ['threshold']
    assert strategy.phase == 'betting' and strategy.rounds == []

    started, done = play('first-bet', '2.00')
    assert started == ['betting'] and done is None
    assert len(strategy.rounds) == 1

    won, cycle = play('winner', '125.00')
    assert won == ['goal']
    assert cycle['outcome'] == 'recovered'
    assert len(cycle['rounds']) == 2
    assert cycle['net_cents'] == 98_000
    assert '125.00' in format_event('goal', strategy.dump(), 'USD', cycle)
    assert '$980.00' in format_event('goal', strategy.dump(), 'USD', cycle)
    assert '100' in format_event('threshold', strategy.dump(), 'USD')


def test_first_virtual_wager_wins_without_losing_start_notice():
    strategy = HighGapStrategy()
    assert strategy.configure('100', 0, '100', 1000, min_stake=1000)
    strategy.enabled = True
    before = strategy.dump()
    _, done = strategy.on_round('120', round_id='anchor', currency='KRW',
                                balance=100_000_00)
    assert done is None
    assert changed_events(before, strategy.dump()) == ['reference', 'threshold']
    before = strategy.dump()
    _, done = strategy.on_round('120', round_id='win', currency='KRW',
                                balance=100_000_00)
    assert done['outcome'] == 'recovered'
    assert changed_events(before, strategy.dump(), done) == ['betting', 'goal']
    assert '₩' in format_event('betting', strategy.dump(), 'KRW', done)


def test_notice_defaults_and_independence():
    assert normalize()['events'] == {
        'reference': False, 'threshold': True, 'betting': True, 'goal': True,
    }
    assert normalize({'events': {'goal': False}, 'sound': False})['events']['goal'] is False
    assert normalize({'delete_delay': -9})['delete_delay'] == 10
    assert normalize({'delete_delay': True})['delete_delay'] == 10
    assert normalize({'enabled': 'false'})['enabled'] is True


def test_actual_delivery_does_not_block_rounds_or_duplicate_messages():
    async def run():
        sent = []

        async def telegram(method, payload):
            assert method == 'sendMessage'
            sent.append(dict(payload))
            return {'result': {}}

        gate = TelegramPasswordGate(telegram)
        gate.redis = FakeRedis(decode_responses=True)
        gate.authorized.add(42)
        gate.chats[42] = 42
        gate.high_gap_notice_settings[42] = normalize({
            'sound': False, 'events': {'reference': False},
        })
        strategy = gate._demo_ledger(42).high_gap
        assert strategy.configure('100', 1, '100', 1000, min_stake=1000)
        strategy.enabled = True

        async def no_save(uid):
            pass
        gate._persist_demo = no_save

        async def feed(number, multiplier):
            await gate._handle_demo_round(42, multiplier, round_id=f'round-{number}',
                                          stream_id=f'{number}-0')
            await asyncio.gather(*list(gate._refresh_tasks))

        await feed(1, '130')
        assert sent == []
        await feed(2, '1.10')
        assert len(sent) == 1 and 'порог достигнут' in sent[-1]['text']
        await feed(3, '1.10')
        assert len(sent) == 2 and 'DEMO запущено' in sent[-1]['text']
        await feed(4, '120')
        assert len(sent) == 3 and 'цель достигнута' in sent[-1]['text']
        assert all(msg['disable_notification'] is True for msg in sent)

        await gate._handle_demo_round(42, '120', round_id='round-4', stream_id='4-0')
        await asyncio.gather(*list(gate._refresh_tasks))
        assert len(sent) == 3

        gate.high_gap_notice_settings[42]['enabled'] = False
        assert await gate._send_high_gap_notice(42, 'goal', 'hidden', round_id='round-5') is False
        assert len(sent) == 3
        await gate.close()

    asyncio.run(run())


def test_independent_settings_survive_persistence_and_reset():
    async def run():
        gate = TelegramPasswordGate(None)
        gate.redis = FakeRedis(decode_responses=True)
        gate.authorized.update({42, 43})
        gate.high_gap_notice_settings[42] = normalize({
            'enabled': False, 'sound': False, 'delete_delay': 60,
            'events': {'threshold': False, 'betting': False, 'goal': True},
        })
        await gate._persist_user(42)
        raw = await gate.redis.hget('luckyjet:high_gap_notices:v1', '42')
        assert json.loads(raw)['delete_delay'] == 60
        assert await gate.redis.hget('luckyjet:high_gap_notices:v1', '43') is None

        gate.high_gap_notice_settings.clear()
        await gate._load_persisted_state()
        assert gate.high_gap_notice_settings[42]['sound'] is False
        assert gate.high_gap_notice_settings[42]['events']['threshold'] is False
        assert gate.high_gap_notice_settings.get(43) is None

        gate.high_gap_notice_settings.pop(42)
        await gate._persist_user(42)
        assert await gate.redis.hget('luckyjet:high_gap_notices:v1', '42') is None
        await gate.close()

    asyncio.run(run())
