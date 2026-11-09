"""Virtual-only strategy 3: consecutive low results, configurable cashout and recovery.

The streak arms a bet on the NEXT round; completed rounds are never wagered
retroactively. Shared money is stored as integer hundredths. No real bets.
"""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP

from .demo_money import MAX_AMOUNT, valid_amount
from .high_gap_demo import parse_multiplier


class TargetStreakStrategy:
    def __init__(self, raw=None, *, currency='USD', default_stake=1000,
                 strategy_name='target_streak'):
        raw = raw if isinstance(raw, dict) else {}
        self.strategy_name = strategy_name
        self.enabled = bool(raw.get('enabled', False))
        x = parse_multiplier(raw.get('target', '1.57'))
        self.target = x if x is not None and x > 1 else Decimal('1.57')
        try:
            trigger = int(raw.get('trigger', 3))
        except (TypeError, ValueError):
            trigger = 3
        min_trigger = 1 if self.strategy_name == 'recovery_streak' else 2
        self.trigger = trigger if min_trigger <= trigger <= 10 else 3
        try:
            stake = int(raw.get('stake_cents', default_stake))
        except (TypeError, ValueError):
            stake = default_stake
        self.stake_cents = stake if valid_amount(stake, currency) else default_stake
        allowed = {'waiting', 'betting', 'paused_gap', 'paused_funds'}
        self.phase = raw.get('phase') if raw.get('phase') in allowed else 'waiting'
        self.streak = max(0, min(self.trigger, int(raw.get('streak', 0) or 0)))
        self.rounds = list(raw.get('rounds') or [])
        self.total_stake_cents = max(0, int(raw.get('total_stake_cents', 0) or 0))
        self.next_stake_cents = max(0, int(raw.get('next_stake_cents', self.stake_cents) or 0))
        self.started_at = raw.get('started_at')
        self.balance_before_cents = raw.get('balance_before_cents')
        self.last_outcome = raw.get('last_outcome')
        self.last_finished_at = raw.get('last_finished_at')

    def dump(self):
        return {
            'enabled': self.enabled, 'target': str(self.target), 'trigger': self.trigger,
            'stake_cents': self.stake_cents, 'phase': self.phase,
            'streak': self.streak, 'rounds': self.rounds,
            'total_stake_cents': self.total_stake_cents,
            'next_stake_cents': self.next_stake_cents,
            'started_at': self.started_at,
            'balance_before_cents': self.balance_before_cents,
            'last_outcome': self.last_outcome, 'last_finished_at': self.last_finished_at,
        }

    def public(self, currency='USD'):
        return {
            'enabled': self.enabled, 'target': str(self.target), 'trigger': self.trigger,
            'stake_cents': self.stake_cents, 'phase': self.phase,
            'streak': self.streak, 'steps': len(self.rounds),
            'total_stake_cents': self.total_stake_cents,
            'next_stake_cents': self.next_stake_cents,
            'active': self.enabled and self.phase == 'betting',
            'last_outcome': self.last_outcome, 'last_finished_at': self.last_finished_at,
            'first_win_profit_cents': self.payout(self.stake_cents, currency) - self.stake_cents,
        }

    def payout(self, stake_cents, currency):
        unit = 100 if currency == 'KRW' else 1
        return int((Decimal(stake_cents) * self.target / unit).quantize(
            Decimal('1'), rounding=ROUND_HALF_UP)) * unit

    def required_stake(self, currency):
        """Smallest stake that recovers all losses + the original bet.

        After first loss: (losses + initial stake) / (target - 1).
        The configured first bet itself stays unchanged. At X<2 its first-win
        net profit is necessarily below the original bet.
        """
        unit = 100 if currency == 'KRW' else 1
        desired_profit = self.total_stake_cents + self.stake_cents
        amount = int((Decimal(desired_profit) / (self.target - 1) / unit)
                     .to_integral_value(rounding=ROUND_CEILING)) * unit
        # Rounding the payout down at a fractional X must not miss the target.
        while amount <= MAX_AMOUNT and self.payout(amount, currency) - amount < desired_profit:
            amount += unit
        return amount

    def configure(self, *, target, trigger, stake_cents, currency):
        if self.enabled and self.phase == 'betting':
            return False
        x = parse_multiplier(target)
        if (x is None or not Decimal('1.01') <= x <= Decimal('10000')
                or type(trigger) is not int
                or not (1 if self.strategy_name == 'recovery_streak' else 2) <= trigger <= 10
                or not valid_amount(stake_cents, currency)
                or Decimal(stake_cents) * x > MAX_AMOUNT):
            return False
        self.target, self.trigger, self.stake_cents = x, trigger, stake_cents
        self.reset_progress()
        return True

    def reset_progress(self):
        self.phase = 'waiting'
        self.streak = 0
        self.rounds = []
        self.total_stake_cents = 0
        self.next_stake_cents = self.stake_cents
        self.started_at = None
        self.balance_before_cents = None

    def seed_streak(self, coefficients):
        """Read-only seeding: never simulate wagers from historical rounds."""
        if not self.enabled or self.phase != 'waiting':
            return
        streak = 0
        for value in coefficients:
            x = parse_multiplier(value)
            if x is None or x >= self.target:
                break
            streak += 1
            if streak >= self.trigger:
                break
        self.streak = streak
        if streak >= self.trigger:
            self.phase = 'betting'
            self.streak = 0

    def _complete(self, outcome, *, balance, currency, at, payout_cents=0,
                  final_coefficient=None):
        record = None
        if self.rounds:
            record = {
                'strategy': self.strategy_name, 'currency': currency,
                'cashout_multiplier': str(self.target), 'trigger_streak': self.trigger,
                'base_stake_cents': self.stake_cents,
                'started_at': self.started_at, 'ended_at': at,
                'rounds': self.rounds[:], 'balance_before_cents': self.balance_before_cents,
                'balance_after_cents': int(balance),
                'total_stake_cents': self.total_stake_cents,
                'payout_cents': payout_cents,
                'net_cents': None if outcome == 'incomplete_data'
                else int(payout_cents or 0) - self.total_stake_cents,
                'outcome': outcome,
                'coefficient': str(final_coefficient) if final_coefficient is not None else None,
            }
        self.reset_progress()
        self.last_outcome = outcome
        self.last_finished_at = at
        return record

    def stop(self, *, balance, currency, now=None):
        if not self.enabled:
            return None
        completed = self._complete('stopped_by_user', balance=balance, currency=currency,
                                   at=(now or datetime.now(timezone.utc)).isoformat())
        self.enabled = False
        return completed

    def on_gap(self, *, balance, currency, now=None):
        had_bets = bool(self.rounds)
        completed = self._complete('incomplete_data', balance=balance, currency=currency,
                                   at=(now or datetime.now(timezone.utc)).isoformat(),
                                   payout_cents=None)
        self.enabled = False
        self.phase = 'paused_gap'
        return completed, had_bets

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
            return balance, None

        stake = self.next_stake_cents
        if (not allow_funding or not valid_amount(stake, currency)
                or stake > balance or self.payout(stake, currency) > MAX_AMOUNT):
            completed = self._complete('insufficient_balance', balance=balance,
                                       currency=currency, at=at)
            self.enabled = False
            self.phase = 'paused_funds'
            return balance, completed
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
        next_stake = self.required_stake(currency)
        if (not valid_amount(next_stake, currency)
                or next_stake > balance
                or self.payout(next_stake, currency) > MAX_AMOUNT):
            completed = self._complete('insufficient_balance', balance=balance,
                                       currency=currency, at=at)
            self.enabled = False
            self.phase = 'paused_funds'
            return balance, completed
        self.next_stake_cents = next_stake
        return balance, None
