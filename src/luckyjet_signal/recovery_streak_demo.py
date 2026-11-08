"""Independent virtual DEMO Strategy 4.

A stake is armed only by N *completed* consecutive rounds strictly below X.
After any lost stake, another fresh N-round signal is required. The configured
stake is the first wager, not an extra profit target. No real bets.
"""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING

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
        # A previously persisted v0.2.25 cycle could be in "betting" directly
        # after a loss. Do not place that old-style immediate next bet.
        self.recovery_signal_armed = raw.get('recovery_signal_armed') is True
        if self.phase == 'betting' and self.rounds and not self.recovery_signal_armed:
            self.phase = 'waiting'
            self.streak = 0

    def dump(self):
        state = super().dump()
        state['recovery_signal_armed'] = self.recovery_signal_armed
        return state

    def public(self, currency='USD'):
        state = super().public(currency)
        # A multi-bet cycle remains active even while waiting for a new signal;
        # reconfiguring it would otherwise erase the unpaid loss history.
        state['active'] = self.enabled and (self.phase == 'betting' or bool(self.rounds))
        return state

    def reset_progress(self):
        super().reset_progress()
        self.recovery_signal_armed = False

    def configure(self, *, target, trigger, stake_cents, currency):
        if self.rounds:
            return False
        return super().configure(target=target, trigger=trigger,
                                 stake_cents=stake_cents, currency=currency)

    def required_stake(self, currency):
        """Recover losses plus the net profit from a winning *first* wager.

        Example: 1,000 KRW at x1.38 targets 380 KRW net, not an extra
        1,000 KRW. Subsequent wagers recover the accumulated losses and the
        same 380 KRW, rounded to the currency's smallest permitted unit.
        """
        unit = 100 if currency == 'KRW' else 1
        initial_profit = self.payout(self.stake_cents, currency) - self.stake_cents
        needed = self.total_stake_cents + initial_profit
        amount = int((Decimal(needed) / (self.target - 1) / unit)
                     .to_integral_value(rounding=ROUND_CEILING)) * unit
        while amount <= MAX_AMOUNT and self.payout(amount, currency) - amount < needed:
            amount += unit
        return amount

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
                # Preserve the recovery stake calculated after a previous loss.
                if not self.rounds:
                    self.next_stake_cents = self.stake_cents
                self.recovery_signal_armed = bool(self.rounds)
                self.streak = 0
            return balance, None

        stake = self.next_stake_cents
        if (not allow_funding or not valid_amount(stake, currency)
                or stake > balance or self.payout(stake, currency) > MAX_AMOUNT):
            finished = self._complete('insufficient_balance', balance=balance,
                                      currency=currency, at=at)
            self.enabled = False
            self.phase = 'paused_funds'
            return balance, finished

        self.recovery_signal_armed = False
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

        self.next_stake_cents = self.required_stake(currency)
        # Do not place another bet until a NEW full streak has completed.
        # Balance adequacy is checked when that next bet is actually due.
        self.phase = 'waiting'
        self.streak = 0
        return balance, None
