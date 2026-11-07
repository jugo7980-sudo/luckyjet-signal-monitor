"""Independent fixed-stake virtual DEMO strategy triggered by rounds after >X.

All decisions use only *previously completed* round results. The first stake
is committed on the round AFTER the chosen wait has elapsed, never on the
triggering round. No API here places real wagers.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def parse_multiplier(raw):
    """Game-style multipliers: finite, >= 1.01, at most two decimal places."""
    try:
        value = Decimal(str(raw).strip().lower().replace('x', '').replace(',', '.'))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if (not value.is_finite() or value < Decimal('1.01')
            or value.as_tuple().exponent < -2
            or value.adjusted() > 8):
        return None
    return value


def as_multiplier(raw, default='100'):
    value = parse_multiplier(raw)
    return value if value is not None else Decimal(default)


class HighGapStrategy:
    """One user's state; all money is the parent's shared integer-minor-units balance."""

    def __init__(self, raw=None, *, min_stake=1000, default_stake=1000):
        raw = raw if isinstance(raw, dict) else {}
        self.enabled = bool(raw.get('enabled', False))
        self.reference = as_multiplier(raw.get('reference', '100'))
        self.target = as_multiplier(raw.get('target', '100'))
        try:
            wait = int(raw.get('wait_rounds', 100))
        except (TypeError, ValueError, OverflowError):
            wait = 100
        self.wait_rounds = wait if 0 <= wait <= 100000 else 100
        try:
            stake = int(raw.get('stake_cents', default_stake))
        except (TypeError, ValueError, OverflowError):
            stake = default_stake
        self.stake_cents = stake if stake >= min_stake else max(min_stake, default_stake)

        allowed = {'waiting_reference', 'waiting', 'betting', 'paused_gap', 'paused_funds'}
        self.phase = raw.get('phase') if raw.get('phase') in allowed else 'waiting_reference'
        self.reference_id = str(raw.get('reference_id') or '')
        self.reference_value = str(raw.get('reference_value') or '')
        self.reference_at = raw.get('reference_at')
        self.elapsed = max(0, int(raw.get('elapsed', 0) or 0))
        self.started_at = raw.get('started_at')
        self.balance_before_cents = raw.get('balance_before_cents')
        self.rounds = list(raw.get('rounds') or [])
        self.total_stake_cents = int(raw.get('total_stake_cents', 0) or 0)
        self.last_outcome = raw.get('last_outcome')
        self.last_finished_at = raw.get('last_finished_at')

    def dump(self):
        return {
            'enabled': self.enabled, 'reference': str(self.reference),
            'target': str(self.target), 'wait_rounds': self.wait_rounds,
            'stake_cents': self.stake_cents, 'phase': self.phase,
            'reference_id': self.reference_id,
            'reference_value': self.reference_value,
            'reference_at': self.reference_at, 'elapsed': self.elapsed,
            'started_at': self.started_at,
            'balance_before_cents': self.balance_before_cents,
            'rounds': self.rounds, 'total_stake_cents': self.total_stake_cents,
            'last_outcome': self.last_outcome, 'last_finished_at': self.last_finished_at,
        }

    def public(self):
        return {
            'enabled': self.enabled, 'phase': self.phase,
            'reference': str(self.reference), 'target': str(self.target),
            'wait_rounds': self.wait_rounds,
            'stake_cents': self.stake_cents, 'elapsed': self.elapsed,
            'remaining': max(0, self.wait_rounds - self.elapsed)
            if self.phase == 'waiting' else None,
            'reference_value': self.reference_value,
            'reference_at': self.reference_at,
            'steps': len(self.rounds),
            'total_stake_cents': self.total_stake_cents,
            'active': self.phase == 'betting' and self.enabled,
            'last_outcome': self.last_outcome,
            'last_finished_at': self.last_finished_at,
        }

    def reset_progress(self):
        self.phase = 'waiting_reference'
        self.reference_id = ''
        self.reference_value = ''
        self.reference_at = None
        self.elapsed = 0
        self.started_at = None
        self.balance_before_cents = None
        self.rounds = []
        self.total_stake_cents = 0

    def configure(self, reference, wait_rounds, target, stake_cents, *, min_stake):
        if self.enabled and self.phase == 'betting':
            return False
        watch = parse_multiplier(reference)
        cashout = parse_multiplier(target)
        if (watch is None or cashout is None or type(wait_rounds) is not int
                or not 0 <= wait_rounds <= 100000
                or type(stake_cents) is not int or stake_cents < min_stake):
            return False
        self.reference = watch
        self.target = cashout
        self.wait_rounds = wait_rounds
        self.stake_cents = stake_cents
        self.reset_progress()  # A new trigger must never reuse old evidence.
        return True

    def arm_from_source(self, *, round_id, coefficient, elapsed, observed_at=None):
        """Read-only backfill. Past bets are NEVER simulated or charged."""
        if self.enabled:
            self.reference_id = str(round_id)
            self.reference_value = str(coefficient)
            self.reference_at = observed_at
            self.elapsed = max(0, int(elapsed))
            self.phase = 'betting' if self.elapsed >= self.wait_rounds else 'waiting'

    def _remember_reference(self, round_id, coefficient, at):
        self.reference_id = str(round_id)
        self.reference_value = str(coefficient)
        self.reference_at = at
        self.elapsed = 0
        self.phase = 'betting' if self.wait_rounds == 0 else 'waiting'

    def _complete(self, outcome, *, currency, balance, at, payout_cents=None,
                  final_coefficient=None):
        """Complete only an actually started cycle; keep every completed bet."""
        result = None
        if self.rounds:
            net = None if outcome == 'incomplete_data' else (
                int(payout_cents or 0) - self.total_stake_cents
            )
            result = {
                'strategy': 'high_gap', 'currency': currency,
                'reference_threshold': str(self.reference),
                'reference_round_id': self.reference_id,
                'reference_coefficient': self.reference_value,
                'wait_rounds': self.wait_rounds,
                'cashout_multiplier': str(self.target),
                'started_at': self.started_at, 'ended_at': at,
                'rounds': self.rounds[:],
                'base_stake_cents': self.stake_cents,
                'balance_before_cents': self.balance_before_cents,
                'balance_after_cents': balance,
                'total_stake_cents': self.total_stake_cents,
                'payout_cents': payout_cents,
                'net_cents': net, 'outcome': outcome,
                'coefficient': str(final_coefficient) if final_coefficient is not None else None,
            }
        self.rounds = []
        self.started_at = None
        self.balance_before_cents = None
        self.total_stake_cents = 0
        self.last_outcome = outcome
        self.last_finished_at = at
        return result

    def stop(self, *, currency, balance, now=None):
        if not self.enabled:
            return None
        at = (now or datetime.now(timezone.utc)).isoformat()
        record = self._complete('stopped_by_user', currency=currency, balance=balance, at=at)
        self.enabled = False
        self.phase = 'waiting_reference'
        return record

    def on_gap(self, *, currency, balance, now=None):
        at = (now or datetime.now(timezone.utc)).isoformat()
        had_open_bets = bool(self.rounds)
        record = self._complete('incomplete_data', currency=currency, balance=balance, at=at)
        self.enabled = False  # No betting with an unverified source sequence.
        self.phase = 'paused_gap'
        return record, had_open_bets

    def on_round(self, coefficient, *, round_id, currency, balance, now=None,
                 allow_funding=True):
        """Return (new shared balance, optional completed cycle)."""
        if not self.enabled or self.phase in {'paused_gap', 'paused_funds'}:
            return balance, None
        try:
            multiplier = Decimal(str(coefficient).strip().rstrip('xX').replace(',', '.'))
        except (ValueError, InvalidOperation, TypeError):
            return balance, None
        if not multiplier.is_finite() or multiplier < 1:
            return balance, None
        at = (now or datetime.now(timezone.utc)).isoformat()
        new_reference = multiplier > self.reference
        if self.phase == 'waiting_reference':
            if new_reference:
                self._remember_reference(round_id, multiplier, at)
            return balance, None
        if self.phase == 'waiting':
            if new_reference:
                self._remember_reference(round_id, multiplier, at)
            else:
                self.elapsed += 1
                if self.elapsed >= self.wait_rounds:
                    self.phase = 'betting'
            return balance, None

        # Only now do we have a stake ready BEFORE receiving this round result.
        if not allow_funding or balance < self.stake_cents:
            record = self._complete(
                'insufficient_balance', currency=currency, balance=balance, at=at,
            )
            self.enabled = False
            self.phase = 'paused_funds'
            return balance, record
        if self.started_at is None:
            self.started_at = at
            self.balance_before_cents = int(balance)
        balance -= self.stake_cents
        self.total_stake_cents += self.stake_cents
        record = {
            'round_id': str(round_id), 'coefficient': str(multiplier),
            'stake_cents': self.stake_cents, 'payout_cents': 0,
            'balance_after_cents': balance,
        }
        self.rounds.append(record)
        if multiplier >= self.target:
            payout = int((Decimal(self.stake_cents) * self.target).quantize(
                Decimal('1'), rounding=ROUND_HALF_UP))
            balance += payout
            record['payout_cents'] = payout
            record['balance_after_cents'] = balance
            done = self._complete('recovered', currency=currency, balance=balance,
                                  at=at, payout_cents=payout,
                                  final_coefficient=multiplier)
            self.phase = 'waiting_reference'
            if new_reference:
                self._remember_reference(round_id, multiplier, at)
            return balance, done
        if new_reference:
            # The reference threshold triggered, but the cashout target did not.
            done = self._complete('reference_changed', currency=currency,
                                  balance=balance, at=at,
                                  final_coefficient=multiplier)
            self._remember_reference(round_id, multiplier, at)
            return balance, done
        return balance, None
