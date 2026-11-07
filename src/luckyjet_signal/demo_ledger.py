"""Virtual-only demo progression and balance ledger for Lucky Jet rounds."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal, DecimalException, InvalidOperation, ROUND_HALF_UP


DEFAULT_BALANCE_CENTS = 1_000_000  # $10,000.00
MINIMUM_STAKE_CENTS = 1_000  # $10.00
DEFAULT_STAKE_CENTS = MINIMUM_STAKE_CENTS
TRIGGER_OPTIONS = (2, 3, 4, 5, 6)
MAX_HISTORY = 100


def parse_dollars(value) -> int | None:
    try:
        amount = Decimal(str(value).strip().replace(",", ".").replace("$", ""))
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -2:
        return None
    try:
        return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (DecimalException, ValueError, OverflowError):
        return None


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
        self.history = list(state.get("history", []))
        self.daily_max_steps = dict(state.get("daily_max_steps", {}))
        # Read-only virtual progression used by Maximum Step, independent of DEMO.
        self.tracker_low_streak = max(0, self._int(state.get("tracker_low_streak", 0), 0))
        self.tracker_step = max(0, self._int(state.get("tracker_step", 0), 0))
        self.backfill_daily_steps()
        self.history = self.history[:MAX_HISTORY]

    def backfill_daily_steps(self):
        # Only same-day sessions have an unambiguous daily attribution.
        for entry in self.history:
            try:
                start = datetime.fromisoformat(entry['started_at'])
                end = datetime.fromisoformat(entry['ended_at'])
                if start.tzinfo is None or end.tzinfo is None:
                    continue
                day = start.astimezone(timezone(timedelta(hours=7))).date().isoformat()
                if day == end.astimezone(timezone(timedelta(hours=7))).date().isoformat():
                    steps = len(entry.get('rounds', []))
                    if entry.get('outcome') in {'stopped_by_user', 'incomplete_data'}:
                        steps += 1
                    if steps:
                        self.daily_max_steps[day] = max(self.daily_max_steps.get(day, 0), steps)
            except (KeyError, TypeError, ValueError):
                continue

        if self.active and self.cycle_started_at:
            try:
                started = datetime.fromisoformat(self.cycle_started_at)
                today = datetime.now(timezone(timedelta(hours=7))).date()
                if started.tzinfo is not None and started.astimezone(timezone(timedelta(hours=7))).date() == today:
                    self._record_step(len(self.cycle_rounds) + 1)
            except (TypeError, ValueError):
                pass

    def _record_step(self, step, now=None):
        day = (now or datetime.now(timezone.utc)).astimezone(timezone(timedelta(hours=7))).date().isoformat()
        self.daily_max_steps[day] = max(self.daily_max_steps.get(day, 0), step)

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
            "daily_max_steps": self.daily_max_steps,
            "tracker_low_streak": self.tracker_low_streak,
            "tracker_step": self.tracker_step,
        }

    def track_max_step(self, coefficient, *, now=None):
        """Track the next hypothetical step even when DEMO is disabled.

        Uses the same consecutive-below-2x trigger as DEMO, but never makes
        virtual bets, changes the balance, or creates DEMO cycle history.
        The current step is recorded immediately when it becomes reachable.
        """
        try:
            multiplier = Decimal(str(coefficient).strip().rstrip("xX").replace(",", "."))
        except (InvalidOperation, ValueError):
            return False
        if not multiplier.is_finite() or multiplier < 1:
            return False

        if self.tracker_step:
            if multiplier >= 2:
                self.tracker_step = 0
                self.tracker_low_streak = 0
                return True
            self.tracker_step += 1
            self._record_step(self.tracker_step, now)
            return True

        if multiplier >= 2:
            self.tracker_low_streak = 0
            return True
        self.tracker_low_streak += 1
        if self.tracker_low_streak >= self.trigger_streak:
            self.tracker_step = 1
            self.tracker_low_streak = 0
            self._record_step(1, now)
        return True

    def reset_max_step_progress(self):
        """Discard continuity when the source stream has a gap."""
        self.tracker_low_streak = 0
        self.tracker_step = 0

    def set_trigger(self, count: int) -> bool:
        if self.active or count not in TRIGGER_OPTIONS:
            return False
        self.trigger_streak = count
        self.low_streak = min(self.low_streak, count - 1)
        return True

    def set_balance(self, cents: int) -> bool:
        if cents < MINIMUM_STAKE_CENTS or self.active:
            return False
        self.balance_cents = cents
        self.cycle_start_balance_cents = cents
        return True

    def set_base_stake(self, cents: int) -> bool:
        if self.active or cents < MINIMUM_STAKE_CENTS or cents > self.balance_cents:
            return False
        self.base_stake_cents = cents
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
        self._record_step(1)
        return {"type": "trigger", "next_stake_cents": self.next_stake_cents}

    def reset(self):
        enabled = self.enabled
        trigger = self.trigger_streak
        self.__init__(
            enabled=enabled,
            trigger_streak=trigger,
            base_stake_cents=MINIMUM_STAKE_CENTS,
        )

    def _finish_cycle(
        self,
        *,
        outcome: str,
        payout_cents: int | None = 0,
        coefficient=None,
        financial_complete: bool = True,
    ):
        net_cents = (
            self.balance_cents - self.cycle_start_balance_cents
            if financial_complete
            else None
        )
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
        self.history = self.history[:MAX_HISTORY]
        self.active = False
        self.cycle_started_at = None
        self.low_streak = 0
        self.next_stake_cents = self.base_stake_cents
        self.cycle_stake_cents = 0
        self.cycle_rounds = []
        self.cycle_start_balance_cents = self.balance_cents
        return entry

    def finish_active(self, outcome: str):
        if outcome not in {"incomplete_data", "stopped_by_user"}:
            raise ValueError("Unsupported demo interruption outcome")
        if not self.active:
            return None
        if outcome == "incomplete_data":
            return self._finish_cycle(
                outcome=outcome,
                payout_cents=None,
                financial_complete=False,
            )
        return self._finish_cycle(outcome=outcome)

    def _finish_insufficient(self, needed_cents: int, *, lost_stake_cents=None):
        entry = self._finish_cycle(outcome="insufficient_balance")
        self.base_stake_cents = MINIMUM_STAKE_CENTS
        self.next_stake_cents = MINIMUM_STAKE_CENTS
        event = {
            "type": "insufficient_balance",
            "needed_cents": needed_cents,
            "entry": entry,
        }
        if lost_stake_cents is not None:
            event["lost_stake_cents"] = lost_stake_cents
        return event

    def process_round(self, coefficient, *, now=None):
        if not self.enabled:
            return None
        try:
            multiplier = Decimal(str(coefficient).strip().rstrip("xX").replace(",", "."))
        except (InvalidOperation, ValueError):
            return None
        if not multiplier.is_finite() or multiplier < 1:
            return None

        if not self.active and self.balance_cents < MINIMUM_STAKE_CENTS:
            self.low_streak = 0
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
            self._record_step(1, now)
            return {"type": "trigger", "next_stake_cents": self.next_stake_cents}

        stake = self.next_stake_cents
        if self.balance_cents < stake:
            return self._finish_insufficient(stake)

        self._record_step(len(self.cycle_rounds) + 1, now)
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
                return self._finish_insufficient(
                    next_stake,
                    lost_stake_cents=stake,
                )
            self.next_stake_cents = next_stake
            self._record_step(len(self.cycle_rounds) + 1, now)
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
