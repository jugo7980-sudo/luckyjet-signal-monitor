"""Maximum-step history is tracked for every authorized user, DEMO on or off."""
import asyncio
import json
from datetime import datetime, timezone, timedelta

from fakeredis.aioredis import FakeRedis

import mini_app_history
from luckyjet_reliable_gate import ReliableTelegramPasswordGate
from luckyjet_signal.demo_ledger import DEFAULT_BALANCE_CENTS, DemoLedger

BANGKOK = timezone(timedelta(hours=7))


def test_offline_demo_tracks_steps_without_bets_and_crosses_midnight():
    ledger = DemoLedger(enabled=False, trigger_streak=3)
    late = datetime(2026, 11, 7, 16, 59, tzinfo=timezone.utc)
    next_day = late + timedelta(minutes=3)
    day1, day2 = "2026-11-07", "2026-11-08"

    for coefficient in ("1.25x", "1.12x", "1.34x"):
        assert ledger.track_max_step(coefficient, now=late)
    assert ledger.tracker_step == 1
    assert ledger.daily_max_steps[day1] == 1

    assert ledger.track_max_step("1.40x", now=next_day)
    assert ledger.tracker_step == 2
    assert ledger.daily_max_steps[day1] == 1
    assert ledger.daily_max_steps[day2] == 2

    ledger.track_max_step("2.40x", now=next_day)
    assert ledger.tracker_step == 0
    assert ledger.tracker_low_streak == 0
    assert ledger.daily_max_steps[day2] == 2

    assert not ledger.track_max_step("not-a-coefficient", now=next_day)
    assert ledger.balance_cents == DEFAULT_BALANCE_CENTS
    assert ledger.history == []
    assert ledger.active is False
    assert ledger.enabled is False
    assert ledger.cycle_rounds == []

    restored = DemoLedger(**ledger.dump())
    assert restored.daily_max_steps == ledger.daily_max_steps
    assert restored.tracker_step == 0
    assert restored.tracker_low_streak == 0


def test_low_balance_does_not_disable_maximum_step_tracking():
    ledger = DemoLedger(enabled=False, balance_cents=0, trigger_streak=2)
    for coefficient in ("1.18", "1.30", "1.10", "1.40", "2.5"):
        assert ledger.track_max_step(coefficient)
    assert max(ledger.daily_max_steps.values()) == 3
    assert ledger.balance_cents == 0
    assert not ledger.active


def test_always_on_tracking_across_rounds_reload_gap_and_history_api(monkeypatch):
    async def run():
        fake = FakeRedis(decode_responses=True)

        async def request(method, payload):
            return {"ok": True, "result": {"message_id": 123}}

        gate = ReliableTelegramPasswordGate(request, history_provider=lambda: [])
        gate.redis = fake
        gate.authorized.update({42, 43})

        class Notices:
            async def submit(self, *a, **kw):
                pass
            async def close(self):
                pass

        class Panels:
            def request(self):
                pass
            async def close(self):
                pass

        gate.notification_queue = Notices()
        gate.live_panels = Panels()

        demo_off = gate._demo_ledger(42)
        demo_off.enabled = False
        demo_off.trigger_streak = 2
        demo_on = gate._demo_ledger(43)
        demo_on.enabled = True
        demo_on.trigger_streak = 3

        for coefficient in ("1.14x", "1.25x", "1.34x", "2.11x"):
            await gate.on_round(coefficient)

        assert max(demo_off.daily_max_steps.values()) == 2
        assert max(demo_on.daily_max_steps.values()) == 1
        assert demo_off.balance_cents == DEFAULT_BALANCE_CENTS
        assert demo_off.history == []
        assert not demo_off.enabled and not demo_off.active
        assert demo_on.enabled and not demo_on.active

        raw = json.loads(await fake.hget("luckyjet:demo_sessions", "42"))
        assert raw["tracker_step"] == 0
        assert raw["tracker_low_streak"] == 0
        assert raw["daily_max_steps"] == demo_off.daily_max_steps
        assert "history" not in raw  # ReliableGate split storage

        day = next(iter(raw["daily_max_steps"]))
        monkeypatch.setattr(mini_app_history, "telegram_user", lambda *args: 42)
        status, _, body = await mini_app_history.history(
            "tma fixture", gate, "token", "view=max_steps",
        )
        response = json.loads(body)
        assert status == "200 OK"
        assert response["days"] == [{"day": day, "steps": 2}]

        status, _, body = await mini_app_history.history(
            "tma fixture", gate, "token", f"view=max_step_day&day={day}",
        )
        assert status == "200 OK"
        assert json.loads(body)["steps"] == 2

        # Source gaps invalidate only continuity, not previously saved maxima.
        for coefficient in ("1.15x", "1.16x"):
            await gate.on_round(coefficient)
        assert demo_off.tracker_step == 1
        await gate.on_history_gap()
        assert demo_off.tracker_step == 0
        assert demo_off.tracker_low_streak == 0
        assert max(demo_off.daily_max_steps.values()) == 2

        # Following an outage, a single below-2x round is not enough to
        # create a new simulated step with a trigger threshold of 2.
        await gate.on_round("1.05x")
        assert demo_off.tracker_step == 0
        assert demo_off.tracker_low_streak == 1
        assert demo_off.balance_cents == DEFAULT_BALANCE_CENTS

        state = json.loads(await fake.hget("luckyjet:demo_sessions", "42"))
        reloaded = DemoLedger(**state)
        assert reloaded.tracker_low_streak == 1
        assert reloaded.daily_max_steps == demo_off.daily_max_steps
        await gate.close()
    asyncio.run(run())
