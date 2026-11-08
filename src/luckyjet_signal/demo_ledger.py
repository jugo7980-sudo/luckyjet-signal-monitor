"""Virtual-only demo progression and balance ledger for Lucky Jet rounds."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from decimal import Decimal, DecimalException, InvalidOperation, ROUND_HALF_UP
from .demo_money import CURRENCIES, valid_amount, format_money
from .high_gap_demo import HighGapStrategy
from .target_streak_demo import TargetStreakStrategy


DEFAULT_BALANCE_CENTS = 1_000_000  # $10,000.00
MINIMUM_STAKE_CENTS = 1_000  # $10.00
DEFAULT_STAKE_CENTS = MINIMUM_STAKE_CENTS
TRIGGER_OPTIONS = tuple(range(2, 11))
MAX_HISTORY = 100
VLADIVOSTOK_TZ = ZoneInfo('Asia/Vladivostok')


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
        self.currency = state.get("currency", "USD")
        if self.currency not in CURRENCIES:
            self.currency = "USD"
        self.minimum_stake_cents = CURRENCIES[self.currency][2]
        self.initial_balance_cents = max(0, self._int(state.get("initial_balance_cents", DEFAULT_BALANCE_CENTS), DEFAULT_BALANCE_CENTS))
        self.reset_revision = str(state.get("reset_revision") or "legacy")
        self.demo_after_stream_id = str(state.get("demo_after_stream_id") or "")
        self.enabled = bool(state.get("enabled", False))
        trigger = self._int(state.get("trigger_streak", 4), 4)
        self.trigger_streak = trigger if trigger in TRIGGER_OPTIONS else 4
        self.balance_cents = max(0, self._int(state.get("balance_cents", DEFAULT_BALANCE_CENTS), DEFAULT_BALANCE_CENTS))
        stake = self._int(state.get("base_stake_cents", DEFAULT_STAKE_CENTS), DEFAULT_STAKE_CENTS)
        self.base_stake_cents = max(self.minimum_stake_cents, stake)
        self.low_streak = max(0, self._int(state.get("low_streak", 0), 0))
        self.active = bool(state.get("active", False))
        self.next_stake_cents = max(self.minimum_stake_cents, self._int(state.get("next_stake_cents", self.base_stake_cents), self.base_stake_cents))
        self.cycle_stake_cents = max(0, self._int(state.get("cycle_stake_cents", 0), 0))
        self.cycle_start_balance_cents = max(0, self._int(state.get("cycle_start_balance_cents", self.balance_cents), self.balance_cents))
        self.cycle_started_at = state.get("cycle_started_at")
        self.cycle_rounds = list(state.get("cycle_rounds", []))[-100:]
        self.history = list(state.get("history", []))
        self.high_history = list(state.get("high_history") or [])[:MAX_HISTORY]
        self.target_history = list(state.get("target_history") or [])[:MAX_HISTORY]
        self.pending_archive_cycles = list(state.get("pending_archive_cycles") or [])
        self.high_gap = HighGapStrategy(
            state.get("high_gap"), min_stake=self.minimum_stake_cents,
            default_stake=self.base_stake_cents,
        )
        self.target_streak = TargetStreakStrategy(
            state.get("target_streak"), currency=self.currency,
            default_stake=self.base_stake_cents,
        )
        self.daily_max_steps = dict(state.get("daily_max_steps", {}))
        # Source-based streak statistics are independent of DEMO trigger,
        # balances and completed cycles. Do not migrate the legacy
        # daily_max_steps: it contains DEMO-step values and is not comparable.
        raw_maxima = state.get("daily_max_low_streak") or {}
        self.daily_max_low_streak = {
            day: max(0, self._int(value, 0))
            for day, value in raw_maxima.items()
            if isinstance(day, str) and len(day) == 10 and self._int(value, 0) > 0
        }
        self.consecutive_below_two = max(0, self._int(state.get("consecutive_below_two", 0), 0))
        self.last_archived_round_id = str(state.get("last_archived_round_id") or "")
        self.last_archived_stream_id = str(state.get("last_archived_stream_id") or "")
        self.balance_verified = bool(state.get("balance_verified", True))
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
                day = start.astimezone(VLADIVOSTOK_TZ).date().isoformat()
                if day == end.astimezone(VLADIVOSTOK_TZ).date().isoformat():
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
                today = datetime.now(VLADIVOSTOK_TZ).date()
                if started.tzinfo is not None and started.astimezone(VLADIVOSTOK_TZ).date() == today:
                    self._record_step(len(self.cycle_rounds) + 1)
            except (TypeError, ValueError):
                pass

    def _record_step(self, step, now=None):
        day = (now or datetime.now(timezone.utc)).astimezone(VLADIVOSTOK_TZ).date().isoformat()
        self.daily_max_steps[day] = max(self.daily_max_steps.get(day, 0), step)

    @staticmethod
    def _int(value, default):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def dump(self):
        return {
            "currency": self.currency,
            "initial_balance_cents": self.initial_balance_cents,
            "reset_revision": self.reset_revision,
            "demo_after_stream_id": self.demo_after_stream_id,
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
            "high_history": self.high_history,
            "target_history": self.target_history,
            "target_streak": self.target_streak.dump(),
            "pending_archive_cycles": self.pending_archive_cycles,
            "high_gap": self.high_gap.dump(),
            "daily_max_steps": self.daily_max_steps,
            "daily_max_low_streak": self.daily_max_low_streak,
            "consecutive_below_two": self.consecutive_below_two,
            "last_archived_round_id": self.last_archived_round_id,
            "last_archived_stream_id": self.last_archived_stream_id,
            "balance_verified": self.balance_verified,
        }

    def track_max_step(self, coefficient, *, now=None):
        """Count every consecutive completed round below 2x, independent of DEMO.

        A 2x-or-higher result ends the run. Daily maxima record the observed
        run length in Asia/Vladivostok, with the run continuing across midnight.
        No bets, DEMO counters, balances or cycle histories are touched.
        """
        try:
            multiplier = Decimal(str(coefficient).strip().rstrip("xX").replace(",", "."))
        except (InvalidOperation, ValueError, TypeError):
            return False
        if not multiplier.is_finite() or multiplier < 1:
            return False

        if multiplier >= 2:
            self.consecutive_below_two = 0
            return True
        self.consecutive_below_two += 1
        day = (now or datetime.now(timezone.utc)).astimezone(
            VLADIVOSTOK_TZ
        ).date().isoformat()
        self.daily_max_low_streak[day] = max(
            self.daily_max_low_streak.get(day, 0), self.consecutive_below_two
        )
        return True

    @staticmethod
    def _stream_position(stream_id):
        parts = str(stream_id or "").split("-")
        if len(parts) != 2 or not all(part.isdecimal() for part in parts):
            raise ValueError("Invalid archive stream sequence number")
        return tuple(int(part) for part in parts)

    def process_archived_round(self, round_id, coefficient, *, now=None, stream_id=None):
        """Idempotent DEMO processing across crashes and replay cursor rewinds.

        The stream position is monotonic. Reject *any* earlier event, not just
        the same last source round, before calculating another simulated bet.
        """
        identifier = str(round_id or "").strip()
        if not identifier:
            raise ValueError("Archived DEMO processing requires a source round ID")
        position = None
        if stream_id is not None:
            position = self._stream_position(stream_id)
            if self.last_archived_stream_id:
                previous = self._stream_position(self.last_archived_stream_id)
                if position <= previous:
                    return None, False
        if identifier == self.last_archived_round_id:
            return None, False
        if not self.track_max_step(coefficient, now=now):
            return None, False
        before_reset = (position is not None and self.demo_after_stream_id
                        and position <= self._stream_position(self.demo_after_stream_id))
        high_committed = self.high_gap.enabled and self.high_gap.phase == 'betting'
        # Both virtual stakes are resolved from one pre-result balance. The
        # established <2x strategy has priority if funds cannot cover both.
        legacy_commit = (
            self.next_stake_cents if self.enabled and self.active
            and self.balance_cents >= self.next_stake_cents else 0
        )
        high_funded = (
            not high_committed or
            self.balance_cents >= legacy_commit + self.high_gap.stake_cents
        )
        # Funding precedence: existing Strategy 1, then Strategy 2, then #3.
        # Reserve simultaneous pre-result stakes before any of them settles.
        high_commit = self.high_gap.stake_cents if high_committed and high_funded else 0
        target_funded = (self.balance_cents >= legacy_commit + high_commit +
                         self.target_streak.next_stake_cents)
        event = self.process_round(coefficient, now=now, round_id=identifier) if self.enabled and not before_reset else None
        if not before_reset:
            self.balance_cents, high_cycle = self.high_gap.on_round(
                coefficient, round_id=identifier, currency=self.currency,
                balance=self.balance_cents, now=now, allow_funding=high_funded,
            )
            if high_cycle is not None:
                self.high_history.insert(0, high_cycle)
                self.high_history = self.high_history[:MAX_HISTORY]
            self.balance_cents, target_cycle = self.target_streak.on_round(
                coefficient, round_id=identifier, balance=self.balance_cents,
                currency=self.currency, now=now, allow_funding=target_funded,
            )
            if target_cycle is not None:
                self.target_history.insert(0, target_cycle)
                self.target_history = self.target_history[:MAX_HISTORY]
        self.last_archived_round_id = identifier
        if position is not None:
            self.last_archived_stream_id = str(stream_id)
        return event, True

    def reset_max_step_progress(self):
        """Source discontinuity ends only the current series, not its maxima."""
        self.consecutive_below_two = 0

    def set_trigger(self, count: int) -> bool:
        if self.active or count not in TRIGGER_OPTIONS:
            return False
        self.trigger_streak = count
        self.low_streak = min(self.low_streak, count - 1)
        return True

    def set_balance(self, cents: int) -> bool:
        if not valid_amount(cents, self.currency) or self.active:
            return False
        self.balance_cents = cents
        self.cycle_start_balance_cents = cents
        return True

    def set_base_stake(self, cents: int) -> bool:
        if self.active or not valid_amount(cents, self.currency) or cents > self.balance_cents:
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
            self.enabled = False
            return {"type": "insufficient_balance", "needed_cents": self.base_stake_cents}
        self.active = True
        self.cycle_started_at = datetime.now(timezone.utc).isoformat()
        self.next_stake_cents = self.base_stake_cents
        self.cycle_stake_cents = 0
        self.cycle_rounds = []
        self.cycle_start_balance_cents = self.balance_cents
        self._record_step(1)
        return {"type": "trigger", "next_stake_cents": self.next_stake_cents}

    def money(self, cents):
        return format_money(cents, self.currency)

    def reset(self, *, currency="USD", balance_cents=DEFAULT_BALANCE_CENTS,
              base_stake_cents=DEFAULT_STAKE_CENTS, reset_revision=None):
        if (not valid_amount(balance_cents, currency)
                or not valid_amount(base_stake_cents, currency)
                or base_stake_cents > balance_cents):
            raise ValueError("Invalid DEMO reset amounts")
        # A DEMO reset must not erase independent source statistics.
        streak_maxima = self.daily_max_low_streak.copy()
        ongoing_streak = self.consecutive_below_two
        last_round_id = self.last_archived_round_id
        last_stream_id = self.last_archived_stream_id
        enabled = self.enabled
        trigger = self.trigger_streak
        old_currency = self.currency
        high_was_enabled = self.high_gap.enabled
        high_settings = self.high_gap.dump()
        target_was_enabled = self.target_streak.enabled
        target_settings = self.target_streak.dump()
        # Preserve every available pre-reset cycle, including any that was
        # open at the moment of reset. The saved current journal still clears.
        if self.active:
            self._finish_cycle(outcome="stopped_by_user")
        if self.high_gap.rounds:
            closed = self.high_gap.stop(
                currency=self.currency, balance=self.balance_cents,
            )
            if closed:
                self.high_history.insert(0, closed)
                self.high_history = self.high_history[:MAX_HISTORY]
        if self.target_streak.rounds:
            closed = self.target_streak.stop(balance=self.balance_cents, currency=self.currency)
            if closed:
                self.target_history.insert(0, closed)
                self.target_history = self.target_history[:MAX_HISTORY]
        pending = self.pending_archive_cycles + self.history + self.high_history + self.target_history
        target_settings['enabled'] = target_was_enabled
        target_settings['phase'] = 'waiting'
        target_settings['streak'] = 0
        target_settings['rounds'] = []
        target_settings['started_at'] = None
        target_settings['balance_before_cents'] = None
        target_settings['total_stake_cents'] = 0
        target_settings['next_stake_cents'] = target_settings['stake_cents']
        if currency != old_currency:
            target_settings['stake_cents'] = base_stake_cents
            target_settings['next_stake_cents'] = base_stake_cents
        # Reset never silently changes the user's on/off preference.
        high_settings["enabled"] = high_was_enabled
        high_settings["phase"] = "waiting_reference"
        high_settings["reference_id"] = ""
        high_settings["reference_value"] = ""
        high_settings["reference_at"] = None
        high_settings["elapsed"] = 0
        high_settings["rounds"] = []
        high_settings["started_at"] = None
        high_settings["balance_before_cents"] = None
        high_settings["total_stake_cents"] = 0
        if currency != old_currency:
            high_settings["stake_cents"] = base_stake_cents
        self.__init__(
            enabled=enabled,
            trigger_streak=trigger,
            currency=currency,
            balance_cents=balance_cents,
            initial_balance_cents=balance_cents,
            base_stake_cents=base_stake_cents,
            reset_revision=reset_revision or self.reset_revision,
            daily_max_low_streak=streak_maxima,
            consecutive_below_two=ongoing_streak,
            high_gap=high_settings,
            high_history=[],
            target_streak=target_settings,
            target_history=[],
            pending_archive_cycles=pending,
            last_archived_round_id=last_round_id,
            last_archived_stream_id=last_stream_id,
            balance_verified=True,
        )

    def _finish_cycle(
        self,
        *,
        outcome: str,
        payout_cents: int | None = 0,
        coefficient=None,
        financial_complete: bool = True,
        now=None,
    ):
        # The other strategy may change the shared balance in this cycle.
        # A global balance delta would falsify this strategy's net result.
        net_cents = (
            int(payout_cents or 0) - self.cycle_stake_cents
            if financial_complete
            else None
        )
        entry = {
            "currency": self.currency,
            "started_at": self.cycle_started_at,
            "ended_at": (now or datetime.now(timezone.utc)).isoformat(),
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

    def _finish_insufficient(self, needed_cents: int, *, lost_stake_cents=None, now=None):
        entry = self._finish_cycle(outcome="insufficient_balance", now=now)
        self.enabled = False
        self.next_stake_cents = self.base_stake_cents
        event = {
            "type": "insufficient_balance",
            "needed_cents": needed_cents,
            "entry": entry,
        }
        if lost_stake_cents is not None:
            event["lost_stake_cents"] = lost_stake_cents
        return event

    def process_round(self, coefficient, *, now=None, round_id=None):
        if not self.enabled:
            return None
        try:
            multiplier = Decimal(str(coefficient).strip().rstrip("xX").replace(",", "."))
        except (InvalidOperation, ValueError):
            return None
        if not multiplier.is_finite() or multiplier < 1:
            return None

        if not self.active and self.balance_cents < self.base_stake_cents:
            self.low_streak = 0
            self.enabled = False
            return {"type": "insufficient_balance", "needed_cents": self.base_stake_cents}

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
            self.cycle_started_at = (now or datetime.now(timezone.utc)).isoformat()
            self.next_stake_cents = self.base_stake_cents
            self.cycle_stake_cents = 0
            self.cycle_rounds = []
            self.cycle_start_balance_cents = self.balance_cents
            self._record_step(1, now)
            return {"type": "trigger", "next_stake_cents": self.next_stake_cents}

        stake = self.next_stake_cents
        if self.balance_cents < stake:
            return self._finish_insufficient(stake, now=now)

        self._record_step(len(self.cycle_rounds) + 1, now)
        self.balance_cents -= stake
        self.cycle_stake_cents += stake
        round_entry = {
            "coefficient": str(multiplier),
            "stake_cents": stake,
            "payout_cents": 0,
            "balance_after_cents": self.balance_cents,
        }
        # Accounting-only metadata, no effect on bet selection or payout.
        if round_id is not None:
            round_entry["round_id"] = str(round_id)
            round_entry["observed_at"] = (now or datetime.now(timezone.utc)).isoformat()
        self.cycle_rounds.append(round_entry)

        if multiplier < 2:
            next_stake = stake * 2
            if self.balance_cents < next_stake:
                return self._finish_insufficient(
                    next_stake,
                    lost_stake_cents=stake,
                    now=now,
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
        entry = self._finish_cycle(outcome="recovered", payout_cents=payout, coefficient=str(multiplier), now=now)
        return {"type": "recovered", **entry}
