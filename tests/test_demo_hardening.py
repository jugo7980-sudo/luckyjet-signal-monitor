from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from luckyjet_signal.demo_ledger import (
    DEFAULT_BALANCE_CENTS,
    DEFAULT_STAKE_CENTS,
    MINIMUM_STAKE_CENTS,
    DemoLedger,
)


def completed_entry(index):
    return {
        "started_at": f"2026-11-03T00:{index % 60:02d}:00+00:00",
        "ended_at": f"2026-11-03T00:{index % 60:02d}:10+00:00",
        "rounds": [],
        "base_stake_cents": MINIMUM_STAKE_CENTS,
        "balance_before_cents": DEFAULT_BALANCE_CENTS,
        "total_stake_cents": 0,
        "payout_cents": 0,
        "net_cents": 0,
        "balance_after_cents": DEFAULT_BALANCE_CENTS,
        "outcome": "recovered",
        "coefficient": "2.00",
    }


def start_cycle(ledger):
    ledger.set_trigger(2)
    assert ledger.process_round("1.10") is None
    assert ledger.process_round("1.20")["type"] == "trigger"
    assert ledger.active


def test_default_demo_stake_is_minimum_ten_dollars():
    assert MINIMUM_STAKE_CENTS == 1_000
    assert DEFAULT_STAKE_CENTS == MINIMUM_STAKE_CENTS
    ledger = DemoLedger()
    assert ledger.base_stake_cents == 1_000
    assert ledger.next_stake_cents == 1_000


def test_history_keeps_newest_one_hundred_sessions_on_load_and_finish():
    existing = [completed_entry(i) for i in range(105)]
    ledger = DemoLedger(enabled=True, history=existing)
    assert len(ledger.history) == 100
    assert ledger.history == existing[:100]

    start_cycle(ledger)
    ledger.process_round("2.00")
    assert len(ledger.history) == 100
    assert ledger.history[0]["outcome"] == "recovered"


def test_insufficient_next_stake_closes_session_before_unaffordable_step_and_resets_base_to_ten():
    ledger = DemoLedger(enabled=True, balance_cents=7_000, base_stake_cents=2_000)
    start_cycle(ledger)

    first = ledger.process_round("1.20")
    assert first["type"] == "loss"
    event = ledger.process_round("1.30")

    assert event["type"] == "insufficient_balance"
    assert event["needed_cents"] == 8_000
    assert ledger.active is False
    assert len(event["entry"]["rounds"]) == 2
    assert event["entry"]["outcome"] == "insufficient_balance"
    assert event["entry"]["base_stake_cents"] == 2_000
    assert ledger.balance_cents == 1_000
    assert ledger.base_stake_cents == MINIMUM_STAKE_CENTS
    assert ledger.next_stake_cents == MINIMUM_STAKE_CENTS


def test_active_session_rejects_trigger_and_base_stake_changes():
    ledger = DemoLedger(enabled=True)
    start_cycle(ledger)

    trigger = ledger.trigger_streak
    stake = ledger.base_stake_cents
    assert ledger.set_trigger(3) is False
    assert ledger.set_base_stake(5_000) is False
    assert ledger.trigger_streak == trigger
    assert ledger.base_stake_cents == stake


def test_reset_preserves_history_and_resets_balance_and_starting_stake_only():
    history = [completed_entry(1)]
    ledger = DemoLedger(
        enabled=True,
        trigger_streak=3,
        balance_cents=50_000,
        base_stake_cents=5_000,
        history=history,
    )

    ledger.reset()

    assert ledger.enabled is True
    assert ledger.trigger_streak == 3
    assert ledger.balance_cents == DEFAULT_BALANCE_CENTS
    assert ledger.base_stake_cents == MINIMUM_STAKE_CENTS
    assert ledger.history == history
    assert ledger.active is False


def test_incomplete_data_closes_active_session_without_fabricated_net_result():
    ledger = DemoLedger(enabled=True)
    start_cycle(ledger)
    ledger.process_round("1.20")
    confirmed_balance = ledger.balance_cents

    entry = ledger.finish_active("incomplete_data")

    assert entry["outcome"] == "incomplete_data"
    assert entry["net_cents"] is None
    assert entry["payout_cents"] is None
    assert entry["balance_after_cents"] == confirmed_balance
    assert ledger.balance_cents == confirmed_balance
    assert ledger.base_stake_cents == DEFAULT_STAKE_CENTS
    assert ledger.active is False


def test_user_stop_records_session_but_keeps_known_balance_and_is_distinct_from_loss():
    ledger = DemoLedger(enabled=True)
    start_cycle(ledger)
    ledger.process_round("1.20")
    confirmed_balance = ledger.balance_cents

    entry = ledger.finish_active("stopped_by_user")

    assert entry["outcome"] == "stopped_by_user"
    assert entry["net_cents"] == confirmed_balance - entry["balance_before_cents"]
    assert entry["balance_after_cents"] == confirmed_balance
    assert ledger.active is False


def test_demo_below_minimum_stake_stays_paused_and_does_not_build_trigger_streak():
    ledger = DemoLedger(enabled=True, trigger_streak=2, balance_cents=MINIMUM_STAKE_CENTS - 1)

    events = [ledger.process_round(value) for value in ("1.10", "1.20", "1.30", "1.40")]

    assert events == [None, None, None, None]
    assert ledger.active is False
    assert ledger.low_streak == 0
    assert ledger.history == []
