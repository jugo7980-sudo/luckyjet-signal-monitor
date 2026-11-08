"""Virtual DEMO Strategy 4: target-bound streak and loss-recovery progression.

Independently persisted from Strategy 3. Shares only the parent DEMO balance.
No live wagers. Cashout and streak threshold are the SAME chosen X (<1.50).
"""
from decimal import Decimal

from .target_streak_demo import TargetStreakStrategy
from .high_gap_demo import parse_multiplier


class RecoveryStreakStrategy(TargetStreakStrategy):
    def __init__(self, raw=None, *, currency='USD', default_stake=1000):
        raw = dict(raw) if isinstance(raw, dict) else {}
        raw.setdefault('target', '1.32')
        super().__init__(raw, currency=currency, default_stake=default_stake,
                         strategy_name='recovery_streak')
        if not Decimal('1.01') <= self.target < Decimal('1.50'):
            self.target = Decimal('1.32')

    def configure(self, *, target, trigger, stake_cents, currency):
        x = parse_multiplier(target)
        if x is None or not Decimal('1.01') <= x < Decimal('1.50'):
            return False
        return super().configure(target=target, trigger=trigger,
                                 stake_cents=stake_cents, currency=currency)
