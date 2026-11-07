"""Regression tests: UI and daily statistics use Vladivostok UTC+10."""
from datetime import datetime, timezone
from pathlib import Path

from coefficient_stats import CoefficientStats, TZ as COEF_TZ
from luckyjet_password_gate import TelegramPasswordGate
from luckyjet_signal.demo_ledger import DemoLedger, VLADIVOSTOK_TZ
from mini_app_history import TZ as HISTORY_TZ

ROOT = Path(__file__).resolve().parents[1]


def test_vladivostok_boundary_is_1400_utc():
    before = datetime(2026, 11, 7, 13, 59, tzinfo=timezone.utc)
    after = datetime(2026, 11, 7, 14, 1, tzinfo=timezone.utc)
    for tz in (COEF_TZ, HISTORY_TZ, VLADIVOSTOK_TZ):
        assert tz.key == "Asia/Vladivostok"
        assert before.astimezone(tz).strftime("%Y-%m-%d %H:%M") == "2026-11-07 23:59"
        assert after.astimezone(tz).strftime("%Y-%m-%d %H:%M") == "2026-11-08 00:01"
    assert CoefficientStats.day(before.timestamp()) == "2026-11-07"
    assert CoefficientStats.day(after.timestamp()) == "2026-11-08"
    gate = TelegramPasswordGate(None)
    assert gate._cycle_time(before.isoformat()) == "07.11.2026 23:59:00"
    assert gate._cycle_time(after.isoformat()) == "08.11.2026 00:01:00"
    series = DemoLedger(enabled=False)
    assert series.track_max_step("1.01x", now=before)
    assert series.track_max_step("1.12x", now=after)
    assert series.daily_max_low_streak == {"2026-11-07": 1, "2026-11-08": 2}


def test_app_pages_only_advertise_vladivostok_time():
    for path in ("web/mini-app.html", "web/history.js"):
        contents = (ROOT / path).read_text(encoding="utf-8")
        assert "Asia/Vladivostok" in contents
        assert "Владивосток" in contents
        assert "Asia/Bangkok" not in contents
        assert "Таиланд" not in contents
        assert "UTC+7" not in contents
