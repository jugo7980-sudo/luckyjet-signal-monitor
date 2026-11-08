"""Independent virtual DEMO Strategy 4: fixed autobet and session loss cap.

The existing single-target behaviour is retained for saved legacy configurations.
New range configurations use a continuous session: wins credit the DEMO balance
without ending the session, and the chosen safe policy ends the session on losses.
No real bets are placed and no past rounds are recalculated.
"""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

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
        upper = parse_multiplier(raw.get('target_max', str(self.target)))
        self.target_max = (upper if upper is not None
                           and self.target <= upper <= Decimal('10000')
                           else self.target)
        # Old persisted sessions retain their historic win-closes-cycle behaviour.
        self.continuous_session = raw.get('continuous_session') is True
        self.safe_mode = (raw.get('safe_mode') if raw.get('safe_mode') in
                          ('total', 'consecutive') else 'total')
        cap = raw.get('safe_losses', 2)
        self.safe_losses = cap if type(cap) is int and 0 <= cap <= 10 else 2
        consecutive = raw.get('consecutive_losses', 0)
        self.consecutive_losses = (consecutive if type(consecutive) is int
                                   and consecutive >= 0 else 0)
        # An old active journal is preserved byte-for-byte. Only FUTURE
        # wagers use the auto-bet state machine. Never recompute past payouts.
        self.session_autobet = (raw.get('session_autobet') is True
                                if 'session_autobet' in raw else not bool(self.rounds))
        if self.rounds and self.phase not in ('paused_gap', 'paused_funds'):
            self.phase = 'betting'
            self.streak = 0
        self.next_stake_cents = self.stake_cents

    @property
    def losses(self):
        return sum(int(row.get('payout_cents') or 0) == 0 for row in self.rounds)

    @property
    def counted_losses(self):
        return self.consecutive_losses if self.safe_mode == 'consecutive' else self.losses

    def next_target(self):
        """Fixed, disclosed 3-point cycle; it does NOT predict the next round."""
        if not self.continuous_session or self.target_max == self.target:
            return self.target
        index = len(self.rounds) % 3
        if index == 0:
            return self.target
        if index == 1:
            return ((self.target + self.target_max) / 2).quantize(
                Decimal('0.01'), rounding=ROUND_HALF_UP)
        return self.target_max

    @staticmethod
    def payout_at(stake_cents, currency, target):
        unit = 100 if currency == 'KRW' else 1
        return int((Decimal(stake_cents) * target / unit).quantize(
            Decimal('1'), rounding=ROUND_HALF_UP)) * unit

    def dump(self):
        state = super().dump()
        state.update(safe_losses=self.safe_losses, session_autobet=self.session_autobet,
                     target_max=str(self.target_max),
                     continuous_session=self.continuous_session,
                     safe_mode=self.safe_mode,
                     consecutive_losses=self.consecutive_losses)
        return state

    def public(self, currency='USD'):
        state = super().public(currency)
        state.update(
            active=self.enabled and (self.phase == 'betting' or bool(self.rounds)),
            safe_losses=self.safe_losses, losses=self.losses,
            counted_losses=self.counted_losses,
            consecutive_losses=self.consecutive_losses,
            safe_mode=self.safe_mode, continuous_session=self.continuous_session,
            target_max=str(self.target_max), next_target=str(self.next_target()),
            last_bet_win=(bool(self.rounds) and
                          int(self.rounds[-1].get('payout_cents') or 0) > 0),
            session_net_cents=(sum(int(r.get('payout_cents') or 0)
                                   for r in self.rounds) - self.total_stake_cents),
            mode='autobet')
        return state

    def reset_progress(self):
        super().reset_progress()
        self.consecutive_losses = 0
        self.session_autobet = True

    def configure(self, *, target, trigger, stake_cents, currency, safe_losses=None,
                  target_max=None, safe_mode=None, continuous_session=None):
        if self.rounds:
            return False
        cap = self.safe_losses if safe_losses is None else safe_losses
        mode = self.safe_mode if safe_mode is None else safe_mode
        continuous = (self.continuous_session if continuous_session is None
                      else continuous_session)
        lower = parse_multiplier(target)
        upper = parse_multiplier(target_max if target_max is not None else target)
        if (type(cap) is not int or not 0 <= cap <= 10
                or mode not in ('total', 'consecutive')
                or type(continuous) is not bool
                or lower is None or upper is None
                or not Decimal('1.01') <= lower <= upper <= Decimal('10000')
                or Decimal(stake_cents) * upper > MAX_AMOUNT):
            return False
        if not super().configure(target=target, trigger=trigger,
                                 stake_cents=stake_cents, currency=currency):
            return False
        self.safe_losses = cap
        self.safe_mode = mode
        self.target_max = upper
        self.continuous_session = continuous
        return True

    def required_stake(self, currency):
        """Every wager in an auto session is precisely the configured size."""
        return self.stake_cents

    def _complete(self, outcome, *, balance, currency, at, payout_cents=0,
                  final_coefficient=None):
        fixed = all(r.get('stake_cents') == self.stake_cents for r in self.rounds)
        session_autobet = self.session_autobet
        losses = self.losses
        counted = self.counted_losses
        wins = sum(int(r.get('payout_cents') or 0) > 0 for r in self.rounds)
        safe_mode = self.safe_mode
        target_max = str(self.target_max)
        continuous = self.continuous_session
        if continuous and payout_cents is not None:
            # Settled wins during a continuous session must be included in the
            # final net result, including when the session closes on a loss.
            payout_cents = sum(int(r.get('payout_cents') or 0) for r in self.rounds)
        entry = super()._complete(outcome, balance=balance, currency=currency,
                                  at=at, payout_cents=payout_cents,
                                  final_coefficient=final_coefficient)
        if entry is not None:
            entry['stake_mode'] = 'fixed' if fixed else 'legacy_variable'
            entry['session_mode'] = 'autobet' if session_autobet else 'legacy_migrated'
            entry['safe_losses'] = self.safe_losses
            entry['losses'] = losses
            if continuous:
                entry['cashout_multiplier_max'] = target_max
                entry['safe_mode'] = safe_mode
                entry['counted_losses'] = counted
                entry['wins'] = wins
                entry['continuous_session'] = True
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
            # The original low-X entry rule is unchanged: N consecutive
            # results strictly below the configured lower target.
            self.streak = self.streak + 1 if multiplier < self.target else 0
            if self.streak >= self.trigger:
                self.phase = 'betting'
                self.next_stake_cents = self.stake_cents
                self.streak = 0
            # The signal round is never wagered retroactively.
            return balance, None

        if self.safe_losses and self.counted_losses >= self.safe_losses:
            return balance, self._complete('safe_limit', balance=balance,
                                            currency=currency, at=at)

        stake = self.stake_cents
        chosen_target = self.next_target()
        if (not allow_funding or not valid_amount(stake, currency)
                or stake > balance
                or self.payout_at(stake, currency, chosen_target) > MAX_AMOUNT):
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
        if self.continuous_session:
            record['cashout_target'] = str(chosen_target)
        self.rounds.append(record)
        if multiplier >= chosen_target:
            payout = self.payout_at(stake, currency, chosen_target)
            balance += payout
            record['payout_cents'] = payout
            record['balance_after_cents'] = balance
            if self.continuous_session:
                # Win does NOT close the new session or change its stake.
                self.consecutive_losses = 0
                self.next_stake_cents = self.stake_cents
                return balance, None
            return balance, self._complete(
                'recovered', balance=balance, currency=currency, at=at,
                payout_cents=payout, final_coefficient=multiplier,
            )

        self.consecutive_losses += 1
        self.next_stake_cents = self.stake_cents
        if self.safe_losses and self.counted_losses >= self.safe_losses:
            # Reset SESSION only; next entry again requires a low-X streak.
            return balance, self._complete('safe_limit', balance=balance,
                                            currency=currency, at=at)
        self.phase = 'betting'
        return balance, None
