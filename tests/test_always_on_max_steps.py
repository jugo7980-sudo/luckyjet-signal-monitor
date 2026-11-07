"""Continuous below-2x maxima are independent of DEMO and its trigger."""
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fakeredis.aioredis import FakeRedis

import mini_app_history
from luckyjet_reliable_gate import ReliableTelegramPasswordGate
from luckyjet_signal.demo_ledger import DEFAULT_BALANCE_CENTS, DemoLedger


def test_screenshot_six_rounds_are_maximum_six_not_demo_step():
    # Exactly the recent 20 coefficients seen on 07.11.2026, in source
    # chronological order (the UI presents them newest first).
    newest_first = [
        "196.94x", "1.10x", "1.17x", "1.90x", "1.18x", "1.44x",
        "2.39x", "1.39x", "2.90x", "2.22x", "1.09x", "1.12x",
        "1.16x", "1.01x", "1.45x", "1.06x", "2.30x", "6.73x",
        "2.11x", "3.76x",
    ]
    timestamp = datetime(2026, 11, 7, 6, 46, tzinfo=timezone.utc)
    for trigger in (2, 4, 6):
        ledger = DemoLedger(enabled=False, trigger_streak=trigger)
        for coefficient in reversed(newest_first):
            assert ledger.track_max_step(coefficient, now=timestamp)
        assert ledger.daily_max_low_streak == {"2026-11-07": 6}
        assert ledger.consecutive_below_two == 0
        assert ledger.balance_cents == DEFAULT_BALANCE_CENTS
        assert ledger.history == []
        assert ledger.daily_max_steps == {}  # DEMO steps are not invented.


def test_records_every_step_one_two_and_six_across_vladivostok_midnight():
    ledger = DemoLedger(enabled=False, trigger_streak=6)
    before = datetime(2026, 11, 7, 13, 59, tzinfo=timezone.utc)
    after = before + timedelta(minutes=3)
    for coefficient, expected in [("1.25", 1), ("1.12x", 2), ("1.34", 3)]:
        assert ledger.track_max_step(coefficient, now=before)
        assert ledger.daily_max_low_streak["2026-11-07"] == expected
    assert ledger.track_max_step("1.40x", now=after)
    assert ledger.daily_max_low_streak["2026-11-08"] == 4  # Series spans days.
    ledger.track_max_step("1.1x", now=after)
    ledger.track_max_step("1.2x", now=after)
    assert ledger.daily_max_low_streak["2026-11-08"] == 6
    assert ledger.track_max_step("2.00x", now=after)
    assert ledger.consecutive_below_two == 0
    assert ledger.track_max_step("1.05x", now=after)
    assert ledger.consecutive_below_two == 1
    assert ledger.daily_max_low_streak["2026-11-08"] == 6
    for bad in ("not-a-coefficient", "NaN", "Infinity", "0.75", None):
        assert not ledger.track_max_step(bad, now=after)
        assert ledger.consecutive_below_two == 1

    restored = DemoLedger(**ledger.dump())
    assert restored.daily_max_low_streak == ledger.daily_max_low_streak
    assert restored.consecutive_below_two == 1
    assert not restored.active
    assert restored.history == []


def test_demo_data_and_old_max_steps_cannot_contaminate_live_streak():
    ledger = DemoLedger(
        enabled=False,
        trigger_streak=4,
        daily_max_steps={"2026-11-07": 99},
        tracker_step=12,
        tracker_low_streak=3,
        history=[{"started_at": "2026-11-07T01:00:00+00:00",
                  "ended_at": "2026-11-07T01:05:00+00:00",
                  "rounds": [{}] * 7, "outcome": "recovered"}],
    )
    assert ledger.daily_max_low_streak == {}
    ledger.track_max_step("1.23x", now=datetime(2026, 11, 7, 3, tzinfo=timezone.utc))
    assert ledger.daily_max_low_streak == {"2026-11-07": 1}
    assert ledger.daily_max_steps["2026-11-07"] == 99
    assert ledger.consecutive_below_two == 1
    ledger.reset()
    assert ledger.daily_max_low_streak == {"2026-11-07": 1}
    assert ledger.consecutive_below_two == 1
    assert ledger.daily_max_steps == {}
    assert ledger.history == []
    assert ledger.balance_cents == DEFAULT_BALANCE_CENTS


def test_live_streak_without_demo_survives_reload_and_gap_and_api(monkeypatch):
    async def run():
        redis = FakeRedis(decode_responses=True)

        async def request(method, payload):
            return {"ok": True, "result": {"message_id": 123}}

        gate = ReliableTelegramPasswordGate(request, history_provider=lambda: [])
        gate.redis = redis
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

        day = next(iter(demo_off.daily_max_low_streak))
        assert demo_off.daily_max_low_streak[day] == 3
        assert demo_on.daily_max_low_streak[day] == 3
        assert demo_off.daily_max_steps == {}  # No DEMO started for user 42.
        assert demo_on.daily_max_steps[day] == 1
        assert demo_off.balance_cents == DEFAULT_BALANCE_CENTS
        assert demo_off.history == [] and not demo_off.enabled and not demo_off.active

        raw = json.loads(await redis.hget("luckyjet:demo_sessions", "42"))
        assert raw["consecutive_below_two"] == 0
        assert raw["daily_max_low_streak"] == demo_off.daily_max_low_streak
        assert "history" not in raw  # ReliableGate split storage.

        monkeypatch.setattr(mini_app_history, "telegram_user", lambda *args: 42)
        status, _, body = await mini_app_history.history(
            "tma fixture", gate, "token", "view=max_steps",
        )
        assert status == "200 OK"
        assert json.loads(body)["days"] == [{"day": day, "steps": 3}]

        status, _, body = await mini_app_history.history(
            "tma fixture", gate, "token", f"view=max_step_day&day={day}",
        )
        assert status == "200 OK"
        assert json.loads(body)["steps"] == 3

        # A gap ends the current run, not the historical daily maximum.
        await gate.on_round("1.15x")
        await gate.on_round("1.16x")
        assert demo_off.consecutive_below_two == 2
        await gate.on_history_gap()
        assert demo_off.consecutive_below_two == 0
        assert demo_off.daily_max_low_streak[day] == 3
        await gate.on_round("1.05x")
        assert demo_off.consecutive_below_two == 1

        saved = json.loads(await redis.hget("luckyjet:demo_sessions", "42"))
        restored = DemoLedger(**saved)
        assert restored.consecutive_below_two == 1
        assert restored.daily_max_low_streak == demo_off.daily_max_low_streak
        assert restored.balance_cents == DEFAULT_BALANCE_CENTS
        await gate.close()

    asyncio.run(run())
