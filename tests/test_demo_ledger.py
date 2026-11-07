from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from luckyjet_signal.demo_ledger import (
    DEFAULT_BALANCE_CENTS,
    DEFAULT_STAKE_CENTS,
    DemoLedger,
    format_dollars,
    parse_dollars,
)


def test_four_low_rounds_start_virtual_cycle_and_2x_recovers_initial_stake():
    ledger = DemoLedger(enabled=True)

    for value in ("1.10x", "1.40x", "1.80x"):
        assert ledger.process_round(value) is None
    trigger = ledger.process_round("1.99x")

    assert trigger == {"type": "trigger", "next_stake_cents": DEFAULT_STAKE_CENTS}
    assert ledger.balance_cents == DEFAULT_BALANCE_CENTS
    assert ledger.active is True

    assert ledger.process_round("1.50x")["next_stake_cents"] == DEFAULT_STAKE_CENTS * 2
    assert ledger.process_round("1.20x")["next_stake_cents"] == DEFAULT_STAKE_CENTS * 4
    result = ledger.process_round("2.00x")

    assert result["type"] == "recovered"
    assert result["total_stake_cents"] == DEFAULT_STAKE_CENTS * 7
    assert result["payout_cents"] == DEFAULT_STAKE_CENTS * 8
    assert result["net_cents"] == DEFAULT_STAKE_CENTS
    assert result["balance_after_cents"] == DEFAULT_BALANCE_CENTS + DEFAULT_STAKE_CENTS
    assert ledger.history[0]["rounds"] == [
        {
            "coefficient": "1.50",
            "stake_cents": DEFAULT_STAKE_CENTS,
            "payout_cents": 0,
            "balance_after_cents": DEFAULT_BALANCE_CENTS - DEFAULT_STAKE_CENTS,
        },
        {
            "coefficient": "1.20",
            "stake_cents": DEFAULT_STAKE_CENTS * 2,
            "payout_cents": 0,
            "balance_after_cents": DEFAULT_BALANCE_CENTS - DEFAULT_STAKE_CENTS * 3,
        },
        {
            "coefficient": "2.00",
            "stake_cents": DEFAULT_STAKE_CENTS * 4,
            "payout_cents": DEFAULT_STAKE_CENTS * 8,
            "balance_after_cents": DEFAULT_BALANCE_CENTS + DEFAULT_STAKE_CENTS,
        },
    ]


def test_three_round_trigger_can_be_selected_and_cycle_waits_for_next_round():
    ledger = DemoLedger(enabled=True)
    assert ledger.set_trigger(3)
    assert ledger.process_round("1.10") is None
    assert ledger.process_round("1.20") is None
    assert ledger.process_round("1.30")["type"] == "trigger"
    assert ledger.active
    assert ledger.process_round("3.00")["type"] == "recovered"
    assert ledger.history[0]["payout_cents"] == DEFAULT_STAKE_CENTS * 2
    assert ledger.history[0]["net_cents"] == DEFAULT_STAKE_CENTS


def test_enabling_demo_uses_the_current_visible_low_round_streak():
    ledger = DemoLedger(enabled=True, trigger_streak=3)
    event = ledger.sync_history(["1.30x", "1.20x", "1.10x", "4.00x"])

    assert event == {"type": "trigger", "next_stake_cents": DEFAULT_STAKE_CENTS}
    assert ledger.active is True
    assert ledger.low_streak == 0


def test_balance_limit_stops_progression_without_negative_balance():
    ledger = DemoLedger(enabled=True)
    for _ in range(4):
        ledger.process_round("1.20")
    event = None
    for _ in range(20):
        event = ledger.process_round("1.20")
        if event and event["type"] == "insufficient_balance":
            break

    assert event["type"] == "insufficient_balance"
    assert event["needed_cents"] > ledger.balance_cents
    assert ledger.balance_cents >= 0
    assert ledger.active is False
    assert ledger.history[0]["outcome"] == "insufficient_balance"


def test_demo_off_does_not_advance_and_round_at_two_resets_waiting_streak():
    ledger = DemoLedger(enabled=False)
    assert ledger.process_round("1.10") is None
    assert ledger.low_streak == 0
    ledger.enabled = True
    ledger.process_round("1.10")
    assert ledger.low_streak == 1
    ledger.process_round("2.00")
    assert ledger.low_streak == 0


def test_usd_validation_and_formatting():
    assert parse_dollars("10") == 1_000
    assert parse_dollars("$150.25") == 15_025
    assert parse_dollars("9.99") == 999
    assert parse_dollars("1.001") is None
    assert parse_dollars("NaN") is None
    assert format_dollars(1_000_000) == "$10,000.00"


def test_virtual_balance_cannot_be_changed_mid_cycle():
    ledger = DemoLedger(enabled=True)
    for _ in range(4):
        ledger.process_round("1.20")
    ledger.process_round("1.30")

    assert ledger.set_balance(2_000_000) is False


def test_demo_state_round_trips_without_losing_open_cycle_or_history():
    ledger = DemoLedger(enabled=True)
    for _ in range(4):
        ledger.process_round("1.50")
    ledger.process_round("1.20")
    restored = DemoLedger(**ledger.dump())

    assert restored.active is True
    assert restored.next_stake_cents == DEFAULT_STAKE_CENTS * 2
    assert restored.balance_cents == DEFAULT_BALANCE_CENTS - DEFAULT_STAKE_CENTS
    assert restored.cycle_rounds == ledger.cycle_rounds


def test_two_round_trigger_bets_only_on_following_round_and_survives_reload():
    ledger = DemoLedger(enabled=True)
    assert ledger.set_trigger(2)
    assert ledger.process_round('1.10') is None
    assert ledger.process_round('1.90')['type'] == 'trigger'
    assert ledger.balance_cents == DEFAULT_BALANCE_CENTS
    restored = DemoLedger(**ledger.dump())
    assert restored.trigger_streak == 2
    assert restored.process_round('2.00')['type'] == 'recovered'
    assert restored.balance_cents == DEFAULT_BALANCE_CENTS + DEFAULT_STAKE_CENTS


def test_trigger_options_extend_to_ten_and_survive_persistence():
    from luckyjet_signal.demo_ledger import TRIGGER_OPTIONS

    assert TRIGGER_OPTIONS == tuple(range(2, 11))
    for trigger in range(7, 11):
        ledger = DemoLedger(enabled=True)
        assert ledger.set_trigger(trigger)
        for i in range(1, trigger):
            assert ledger.process_round("1.20x") is None
            assert ledger.low_streak == i
            assert ledger.active is False

        restored = DemoLedger(**ledger.dump())
        assert restored.trigger_streak == trigger
        assert restored.low_streak == trigger - 1
        event = restored.process_round("1.20x")
        assert event == {"type": "trigger", "next_stake_cents": DEFAULT_STAKE_CENTS}
        assert restored.active is True
        # Triggering arms the following simulated bet, without changing balance.
        assert restored.balance_cents == DEFAULT_BALANCE_CENTS
        assert restored.process_round("2.00x")["type"] == "recovered"
        assert restored.balance_cents == DEFAULT_BALANCE_CENTS + DEFAULT_STAKE_CENTS

    ledger = DemoLedger()
    for invalid in (1, 11, 99):
        assert not ledger.set_trigger(invalid)
    assert DemoLedger(trigger_streak=10).trigger_streak == 10
    assert DemoLedger(trigger_streak=11).trigger_streak == 4


def test_ten_round_trigger_syncs_existing_history_only_after_tenth_round():
    ledger = DemoLedger(enabled=True, trigger_streak=10)
    assert ledger.sync_history(["1.20x"] * 9 + ["2.00x"]) is None
    assert ledger.low_streak == 9
    assert ledger.sync_history(["1.20x"] * 10 + ["2.00x"]) == {
        "type": "trigger", "next_stake_cents": DEFAULT_STAKE_CENTS,
    }
    assert ledger.active
    assert ledger.balance_cents == DEFAULT_BALANCE_CENTS


def test_telegram_trigger_menu_offers_all_options_up_to_ten():
    from luckyjet_password_gate import TelegramPasswordGate

    gate = TelegramPasswordGate(None)
    ledger = gate._demo_ledger(42)
    assert ledger.set_trigger(10)
    title, markup = gate._choice_screen(42, "demo_trigger_menu")
    assert "Старт демо" in title
    choices = [
        button for row in markup["inline_keyboard"] for button in row
        if button["callback_data"].startswith("demo_trigger:")
    ]
    assert [item["callback_data"] for item in choices] == [
        f"demo_trigger:{n}" for n in range(2, 11)
    ]
    assert any("10 подряд" in item["text"] for item in choices)
    assert gate._supported_callback("demo_trigger:10", 42)
    assert not gate._supported_callback("demo_trigger:11", 42)
