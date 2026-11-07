"""Archive-backed DEMO catch-up never duplicates simulated stakes or hides gaps."""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from fakeredis.aioredis import FakeRedis

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from archive_consumer import process_archive_event, select_initial_cursor, UI_CURSOR_KEY
from luckyjet_signal.demo_ledger import DemoLedger, DEFAULT_BALANCE_CENTS
from luckyjet_signal.monitor import State
from round_archive import RoundArchive, STREAM_KEY, SEEN_KEY


def snapshot(*ids):
    return [{"id": str(i), "coefficient": 1.25} for i in ids]


class DemoGateFixture:
    def __init__(self, redis, **settings):
        self.redis = redis
        self.ledger = DemoLedger(**settings)
        self.history_gaps = 0
        self.coefficient_stats = None
        self.ready_event = asyncio.Event()
        self.ready_event.set()

    async def on_round(self, coefficient, *, round_id=None):
        assert round_id is not None
        _, changed = self.ledger.process_archived_round(round_id, coefficient)
        if changed:
            await self.redis.hset("fixture:demo", "42", json.dumps(self.ledger.dump()))

    async def on_history_gap(self):
        self.history_gaps += 1
        self.ledger.reset_max_step_progress()
        if self.ledger.enabled:
            self.ledger.balance_verified = False
            if self.ledger.active:
                self.ledger.finish_active("incomplete_data")


def test_catchup_after_update_and_restart_exactly_once():
    async def run():
        redis = FakeRedis(decode_responses=True)
        archive = RoundArchive(redis, "primary")
        gate = DemoGateFixture(redis, enabled=True, trigger_streak=2)
        state = State(last_round_id="1", previous=[1.25])
        panel = SimpleNamespace(history=["1.25x"], limit=20)

        await archive.append_snapshot(snapshot(3, 2, 1), observed_at_ms=1000)
        initial = await select_initial_cursor(redis, gate, "1")
        assert initial == await redis.hget(SEEN_KEY, "1")
        assert gate.history_gaps == 0

        entries = await redis.xrange(STREAM_KEY)
        events = [(eid, item) for eid, item in entries
                  if item.get("type") == "round" and item.get("round_id") in {"2", "3"}]
        for event_id, fields in events:
            await process_archive_event(redis, gate, state, panel, event_id, fields)

        # Second low round triggers the DEMO cycle; no bet placed until next.
        assert gate.ledger.active is True
        assert gate.ledger.balance_cents == DEFAULT_BALANCE_CENTS
        assert gate.ledger.last_archived_round_id == "3"
        assert state.last_round_id == "3"
        assert await redis.get(UI_CURSOR_KEY) == events[-1][0]

        # A new round after the interrupted update charges the first stake.
        await archive.append_snapshot(snapshot(4, 3, 2), observed_at_ms=2000)
        tail_id, item = (await redis.xrevrange(STREAM_KEY, count=1))[0]
        assert item["round_id"] == "4"
        await process_archive_event(redis, gate, state, panel, tail_id, item)
        before = gate.ledger.balance_cents
        assert before < DEFAULT_BALANCE_CENTS
        assert gate.ledger.last_archived_round_id == "4"

        # Simulated power loss after the ledger was saved but before a cursor
        # acknowledgement: reloading and replaying must not stake twice.
        loaded = json.loads(await redis.hget("fixture:demo", "42"))
        restarted_gate = DemoGateFixture(redis, **loaded)
        await process_archive_event(redis, restarted_gate, state, panel, tail_id, item)
        assert restarted_gate.ledger.balance_cents == before
        assert restarted_gate.ledger.cycle_rounds == gate.ledger.cycle_rounds
        assert restarted_gate.ledger.last_archived_round_id == "4"
        assert await redis.get(UI_CURSOR_KEY) == tail_id
        await redis.aclose()
    asyncio.run(run())


def test_gap_does_not_pretend_demo_balance_is_verified():
    async def run():
        redis = FakeRedis(decode_responses=True)
        archive = RoundArchive(redis, "primary")
        gate = DemoGateFixture(redis, enabled=True, trigger_streak=2)
        state = State(last_round_id="2", previous=[1.25])
        panel = SimpleNamespace(history=[], limit=20)
        await archive.append_snapshot(snapshot(2, 1), observed_at_ms=1000)
        await archive.append_snapshot(snapshot(9, 8), observed_at_ms=2000)
        events = await redis.xrange(STREAM_KEY)
        gap_id, gap = next((eid, fields) for eid, fields in events if fields.get("type") == "gap")
        await process_archive_event(redis, gate, state, panel, gap_id, gap)
        assert gate.history_gaps == 1
        assert gate.ledger.balance_verified is False
        assert gate.ledger.balance_cents == DEFAULT_BALANCE_CENTS
        assert await redis.get(UI_CURSOR_KEY) == gap_id
        await redis.aclose()
    asyncio.run(run())


def test_no_previous_archive_overlap_marks_gap_and_does_not_guess_profit():
    async def run():
        redis = FakeRedis(decode_responses=True)
        archive = RoundArchive(redis, "one")
        await archive.append_snapshot(snapshot(10, 9, 8), observed_at_ms=1000)
        gate = DemoGateFixture(redis, enabled=True)
        cursor = await select_initial_cursor(redis, gate, "legacy-never-in-archive")
        assert gate.history_gaps == 1
        assert gate.ledger.balance_verified is False
        assert cursor == (await redis.xrevrange(STREAM_KEY, count=1))[0][0]
        assert gate.ledger.balance_cents == DEFAULT_BALANCE_CENTS
        await redis.aclose()
    asyncio.run(run())


def test_demo_reset_preserves_archived_cursor_but_resets_unverified_flag():
    ledger = DemoLedger(enabled=True, balance_verified=False, last_archived_round_id="123")
    ledger.reset()
    assert ledger.balance_verified is True
    assert ledger.last_archived_round_id == "123"
