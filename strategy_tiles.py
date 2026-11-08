"""Read-only, per-strategy DEMO indicators; never modify the betting ledger.

Open-cycle net is derived from that strategy's own recorded bets. A closed
cycle keeps its recorded net until the next cycle starts. The common DEMO
balance must NEVER be used to infer a single strategy's profit.
"""


def _net_from_rounds(rounds):
    if not rounds:
        return None
    try:
        return sum(int(row['payout_cents']) - int(row['stake_cents'])
                   for row in rounds)
    except (KeyError, TypeError, ValueError):
        return None


def _last_cycle(history):
    if not history:
        return None
    entry = history[0]
    if not isinstance(entry, dict):
        return None
    net = entry.get('net_cents')
    return int(net) if isinstance(net, int) and not isinstance(net, bool) else None


def _tile(number, enabled, phase, progress, stake, target, rounds, history,
          last_action=None):
    current = bool(rounds)
    result = _net_from_rounds(rounds) if current else _last_cycle(history)
    return {
        'id': number,
        'enabled': bool(enabled),
        'phase': phase,
        'progress': progress,
        'stake_cents': int(stake) if stake is not None else None,
        'target': str(target),
        'net_cents': result,
        'result_scope': 'current' if current else ('last' if result is not None else 'none'),
        'last_action': last_action,
    }


def _action(rounds, history):
    rows = rounds if rounds else ((history[0].get('rounds') or [])
                                 if history and isinstance(history[0], dict) else [])
    if not rows:
        return 'Нет ставок'
    row = rows[-1]
    if not isinstance(row, dict):
        return 'Нет данных'
    try:
        return 'Выигрыш' if int(row['payout_cents']) > 0 else 'Проигрыш'
    except (KeyError, ValueError, TypeError):
        return 'Нет данных'


def strategy_tile_snapshot(ledger):
    """Produce four isolated progress/finance cards from committed server state."""
    classic_rounds = ledger.cycle_rounds if ledger.active else []
    classic_progress = (
        str(len(classic_rounds) + 1) if ledger.active
        else f'{ledger.low_streak}/{ledger.trigger_streak}'
    )
    high = ledger.high_gap
    high_progress = (
        f'{high.elapsed}/{high.wait_rounds}' if high.phase == 'waiting'
        else 'Ждёт X' if high.phase == 'waiting_reference'
        else 'Ставка' if high.phase == 'betting' else 'Пауза'
    )
    target = ledger.target_streak
    recovery = ledger.recovery_streak
    return [
        _tile(1, ledger.enabled, 'betting' if ledger.active else 'waiting',
              classic_progress,
              ledger.next_stake_cents if ledger.active else ledger.base_stake_cents,
              '2', classic_rounds, ledger.history,
              _action(classic_rounds, ledger.history)),
        _tile(2, high.enabled, high.phase, high_progress, high.stake_cents,
              high.target, high.rounds, ledger.high_history,
              _action(high.rounds, ledger.high_history)),
        _tile(3, target.enabled, target.phase,
              f'{target.streak}/{target.trigger}' if target.phase == 'waiting'
              else 'Ставка' if target.phase == 'betting' else 'Пауза',
              target.next_stake_cents, target.target, target.rounds,
              ledger.target_history, _action(target.rounds, ledger.target_history)),
        _tile(4, recovery.enabled, recovery.phase,
              f'{recovery.streak}/{recovery.trigger}' if recovery.phase == 'waiting'
              else 'Ставка' if recovery.phase == 'betting' else 'Пауза',
              recovery.next_stake_cents, recovery.target, recovery.rounds,
              ledger.recovery_history, _action(recovery.rounds, ledger.recovery_history)),
    ]
