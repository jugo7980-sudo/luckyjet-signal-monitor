"""Virtual-only demo progression and balance ledger for Lucky Jet rounds."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


DEFAULT_BALANCE_CENTS = 1_000_000  # $10,000.00
DEFAULT_STAKE_CENTS = 15_000  # $150.00
MINIMUM_STAKE_CENTS = 1_000  # $10.00
TRIGGER_OPTIONS = (3, 4, 5, 6)


def parse_dollars(value) -> int | None:
    try:
        amount = Decimal(str(value).strip().replace(",", ".").replace("$", ""))
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -2:
        return None
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_dollars(cents: int) -> str:
    amount = Decimal(int(cents)) / 100
    if amount < 0:
        return f"-${abs(amount):,.2f}"
    return f"${amount:,.2f}"


class DemoLedger:
    """Tracks simulated bets only; it never sends a wager to the game."""

    def __init__(self, **state):
        self.enabled = bool(state.get("enabled", False))
        trigger = self._int(state.get("trigger_streak", 4), 4)
        self.trigger_streak = trigger if trigger in TRIGGER_OPTIONS else 4
        self.balance_cents = max(0, self._int(state.get("balance_cents", DEFAULT_BALANCE_CENTS), DEFAULT_BALANCE_CENTS))
        stake = self._int(state.get("base_stake_cents", DEFAULT_STAKE_CENTS), DEFAULT_STAKE_CENTS)
        self.base_stake_cents = max(MINIMUM_STAKE_CENTS, stake)
        self.low_streak = max(0, self._int(state.get("low_streak", 0), 0))
        self.active = bool(state.get("active", False))
        self.next_stake_cents = max(MINIMUM_STAKE_CENTS, self._int(state.get("next_stake_cents", self.base_stake_cents), self.base_stake_cents))
        self.cycle_stake_cents = max(0, self._int(state.get("cycle_stake_cents", 0), 0))
        self.cycle_start_balance_cents = max(0, self._int(state.get("cycle_start_balance_cents", self.balance_cents), self.balance_cents))
        self.cycle_started_at = state.get("cycle_started_at")
        self.cycle_rounds = list(state.get("cycle_rounds", []))[-100:]
        self.history = list(state.get("history", []))[-50:]

    @staticmethod
    def _int(value, default):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def dump(self):
        return {
            "enabled": self.enabled,
            "trigger_streak": self.trigger_streak,
            "balance_cents": self.balance_cents,
            "base_stake_cents": self.base_stake_cents,
            "low_streak": self.low_streak,
            "active": self.active,
            "next_stake_cents": self.next_stake_cents,
            "cycle_stake_cents": self.cycle_stake_cents,
            "cycle_start_balance_cents": self.cycle_start_balance_cents,
            "cycle_started_at": self.cycle_started_at,
            "cycle_rounds": self.cycle_rounds,
            "history": self.history,
        }

    def set_trigger(self, count: int) -> bool:
        if count not in TRIGGER_OPTIONS:
            return False
        self.trigger_streak = count
        if not self.active:
            self.low_streak = min(self.low_streak, count - 1)
        return True

    def set_balance(self, cents: int) -> bool:
        if cents < MINIMUM_STAKE_CENTS or self.active:
            return False
        self.balance_cents = cents
        if not self.active:
            self.cycle_start_balance_cents = cents
        return True

    def set_base_stake(self, cents: int) -> bool:
        if cents < MINIMUM_STAKE_CENTS or cents > self.balance_cents:
            return False
        self.base_stake_cents = cents
        if not self.active:
            self.next_stake_cents = cents
        return True

    def sync_history(self, coefficients):
        """Arm on an already visible consecutive run when demo is enabled."""
        if self.active:
            return None
        count = 0
        for value in list(coefficients or []):
            try:
                multiplier = Decimal(str(value).strip().rstrip("xX").replace(",", "."))
            except (InvalidOperation, ValueError):
                break
            if not multiplier.is_finite() or multiplier < 1 or multiplier >= 2:
                break
            count += 1
        self.low_streak = min(count, self.trigger_streak)
        if count < self.trigger_streak:
            return None
        self.low_streak = 0
        if self.balance_cents < self.base_stake_cents:
            return {"type": "insufficient_balance", "needed_cents": self.base_stake_cents}
        self.active = True
        self.cycle_started_at = datetime.now(timezone.utc).isoformat()
        self.next_stake_cents = self.base_stake_cents
        self.cycle_stake_cents = 0
        self.cycle_rounds = []
        self.cycle_start_balance_cents = self.balance_cents
        return {"type": "trigger", "next_stake_cents": self.next_stake_cents}

    def reset(self):
        enabled = self.enabled
        trigger = self.trigger_streak
        stake = self.base_stake_cents
        self.__init__(
            enabled=enabled,
            trigger_streak=trigger,
            base_stake_cents=stake,
        )

    def _finish_cycle(self, *, outcome: str, payout_cents: int = 0, coefficient=None):
        net_cents = payout_cents - self.cycle_stake_cents
        entry = {
            "started_at": self.cycle_started_at,
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "rounds": self.cycle_rounds[:],
            "base_stake_cents": self.base_stake_cents,
            "balance_before_cents": self.cycle_start_balance_cents,
            "total_stake_cents": self.cycle_stake_cents,
            "payout_cents": payout_cents,
            "net_cents": net_cents,
            "balance_after_cents": self.balance_cents,
            "outcome": outcome,
            "coefficient": coefficient,
        }
        self.history.insert(0, entry)
        self.history = self.history[:50]
        self.active = False
        self.cycle_started_at = None
        self.low_streak = 0
        self.next_stake_cents = self.base_stake_cents
        self.cycle_stake_cents = 0
        self.cycle_rounds = []
        self.cycle_start_balance_cents = self.balance_cents
        return entry

    def process_round(self, coefficient):
        if not self.enabled:
            return None
        try:
            multiplier = Decimal(str(coefficient).strip().rstrip("xX").replace(",", "."))
        except (InvalidOperation, ValueError):
            return None
        if not multiplier.is_finite() or multiplier < 1:
            return None

        if not self.active:
            if multiplier >= 2:
                self.low_streak = 0
                return None
            self.low_streak += 1
            if self.low_streak < self.trigger_streak:
                return None
            self.low_streak = 0
            if self.balance_cents < self.base_stake_cents:
                return {"type": "insufficient_balance", "needed_cents": self.base_stake_cents}
            self.active = True
            self.cycle_started_at = datetime.now(timezone.utc).isoformat()
            self.next_stake_cents = self.base_stake_cents
            self.cycle_stake_cents = 0
            self.cycle_rounds = []
            self.cycle_start_balance_cents = self.balance_cents
            return {"type": "trigger", "next_stake_cents": self.next_stake_cents}

        stake = self.next_stake_cents
        if self.balance_cents < stake:
            entry = self._finish_cycle(outcome="insufficient_balance")
            return {"type": "insufficient_balance", "needed_cents": stake, "entry": entry}

        self.balance_cents -= stake
        self.cycle_stake_cents += stake
        round_entry = {
            "coefficient": str(multiplier),
            "stake_cents": stake,
            "payout_cents": 0,
            "balance_after_cents": self.balance_cents,
        }
        self.cycle_rounds.append(round_entry)

        if multiplier < 2:
            next_stake = stake * 2
            if self.balance_cents < next_stake:
                entry = self._finish_cycle(outcome="insufficient_balance")
                return {
                    "type": "insufficient_balance",
                    "lost_stake_cents": stake,
                    "needed_cents": next_stake,
                    "entry": entry,
                }
            self.next_stake_cents = next_stake
            return {
                "type": "loss",
                "coefficient": float(multiplier),
                "stake_cents": stake,
                "next_stake_cents": next_stake,
                "balance_cents": self.balance_cents,
            }

        # The strategy cashes out at 2x; the final crash multiplier only
        # determines whether that target was reached.
        payout = stake * 2
        self.balance_cents += payout
        round_entry["payout_cents"] = payout
        round_entry["balance_after_cents"] = self.balance_cents
        entry = self._finish_cycle(outcome="recovered", payout_cents=payout, coefficient=str(multiplier))
        return {"type": "recovered", **entry}
