"""Independent virtual DEMO Strategy 4: armed fixed autobet with a loss cap.

Wait for N consecutive *completed* rounds below the selected X, then wager
the same configured virtual amount on EVERY subsequent round until the selected
cashout X is reached or the session's loss cap ("safe") is hit. A safe closure
resets the entry series without turning the strategy off. No real bets.
"""
from datetime import datetime, timezone
from decimal import Decimal

from .demo_money import MAX_AMOUNT, valid_amount
from .target_streak_demo import TargetStreakStrategy
from .high_gap_demo import parse_multiplier


class RecoveryStreakStrategy(TargetStreakStrategy):
    def __init__(self, raw=None, *, currency='USD', default_stake=1000):
        raw = dict(raw) if isinstance(raw, dict) else {}
        raw.setdefault('target', '1.32')
        super().__init__(raw, currency=currency, default_stake=default_stake,
                         strategy_name='recovery_streak')
        if not Decimal('1.01') <= self.target <= Decimal('10000'):
            self.target = Decimal('1.32')
        cap = raw.get('safe_losses', 2)
        self.safe_losses = cap if type(cap) is int and 0 <= cap <= 10 else 2
        # An old active journal is preserved byte-for-byte. Only FUTURE
        # wagers use the new auto-bet state machine. Never recompute payouts.
        self.session_autobet = (raw.get('session_autobet') is True
                                if 'session_autobet' in raw else not bool(self.rounds))
        if self.rounds and self.phase not in ('paused_gap', 'paused_funds'):
            self.phase = 'betting'
            self.streak = 0
        self.next_stake_cents = self.stake_cents

    @property
    def losses(self):
        return sum(int(row.get('payout_cents') or 0) == 0 for row in self.rounds)

    def dump(self):
        state = super().dump()
        state['safe_losses'] = self.safe_losses
        state['session_autobet'] = self.session_autobet
        return state

    def public(self, currency='USD'):
        state = super().public(currency)
        state['active'] = self.enabled and (self.phase == 'betting' or bool(self.rounds))
        state['safe_losses'] = self.safe_losses
        state['losses'] = self.losses
        state['mode'] = 'autobet'
        return state

    def reset_progress(self):
        super().reset_progress()
        self.session_autobet = True

    def configure(self, *, target, trigger, stake_cents, currency, safe_losses=None):
        if self.rounds:
            return False
        cap = self.safe_losses if safe_losses is None else safe_losses
        if type(cap) is not int or not 0 <= cap <= 10:
            return False
        if not super().configure(target=target, trigger=trigger,
                                 stake_cents=stake_cents, currency=currency):
            return False
        self.safe_losses = cap
        return True

    def required_stake(self, currency):
        """Every wager in the auto session has precisely the configured size."""
        return self.stake_cents

    def _complete(self, outcome, *, balance, currency, at, payout_cents=0,
                  final_coefficient=None):
        fixed = all(r.get('stake_cents') == self.stake_cents for r in self.rounds)
        session_autobet = self.session_autobet
        losses = self.losses
        entry = super()._complete(outcome, balance=balance, currency=currency,
                                  at=at, payout_cents=payout_cents,
                                  final_coefficient=final_coefficient)
        if entry is not None:
            entry['stake_mode'] = 'fixed' if fixed else 'legacy_variable'
            entry['session_mode'] = 'autobet' if session_autobet else 'legacy_migrated'
            entry['safe_losses'] = self.safe_losses
            entry['losses'] = losses
        return entry

    def on_round(self, coefficient, *, round_id, balance, currency, now=None,
                 allow_funding=True):
        if not self.enabled or self.phase in ('paused_gap', 'paused_funds'):
            return balance, None
        multiplier = parse_multiplier(coefficient)
        if multiplier is None:
            return balance, None
        at = (now or datetime.now(timezone.utc)).isoformat()

        if self.phase == 'waiting':
            self.streak = self.streak + 1 if multiplier < self.target else 0
            if self.streak >= self.trigger:
                self.phase = 'betting'
                self.next_stake_cents = self.stake_cents
                self.streak = 0
            # The signal round is never wagered retroactively.
            return balance, None

        # A pre-upgrade active session may already exceed its new safe cap.
        # Close the existing journal before placing ANY post-upgrade wager.
        if self.safe_losses and self.losses >= self.safe_losses:
            return balance, self._complete('safe_limit', balance=balance,
                                            currency=currency, at=at)

        stake = self.stake_cents
        if (not allow_funding or not valid_amount(stake, currency)
                or stake > balance or self.payout(stake, currency) > MAX_AMOUNT):
            finished = self._complete('insufficient_balance', balance=balance,
                                      currency=currency, at=at)
            self.enabled = False
            self.phase = 'paused_funds'
            return balance, finished

        if self.started_at is None:
            self.started_at = at
            self.balance_before_cents = int(balance)
        balance -= stake
        self.total_stake_cents += stake
        record = {
            'round_id': str(round_id), 'coefficient': str(multiplier),
            'stake_cents': stake, 'payout_cents': 0,
            'balance_after_cents': balance, 'observed_at': at,
        }
        self.rounds.append(record)
        if multiplier >= self.target:
            payout = self.payout(stake, currency)
            balance += payout
            record['payout_cents'] = payout
            record['balance_after_cents'] = balance
            return balance, self._complete(
                'recovered', balance=balance, currency=currency, at=at,
                payout_cents=payout, final_coefficient=multiplier,
            )

        self.next_stake_cents = self.stake_cents
        if self.safe_losses and self.losses >= self.safe_losses:
            # Reset this SESSION only. Stay enabled and wait for a new
            # initial entry signal on subsequent completed rounds.
            return balance, self._complete('safe_limit', balance=balance,
                                            currency=currency, at=at)
        # A single initial signal opens an uninterrupted auto-bet session.
        self.phase = 'betting'
        return balance, None
