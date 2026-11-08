"""User-specific, virtual-only Strategy 2 Telegram notification presentation.

The strategy ledger remains the sole source of truth for bets and outcomes.
This module only classifies persisted state transitions and formats notices.
"""
from luckyjet_signal.demo_money import format_money

EVENTS = ('reference', 'threshold', 'betting', 'goal', 'safe')
DEFAULT_EVENTS = {
    'reference': False,  # Optional: avoid messages for every new >X anchor.
    'threshold': True,
    'betting': True,
    'goal': True,
    'safe': True,
}
DELETE_DELAYS = (0, 10, 30, 60)


def normalize(raw=None):
    raw = raw if isinstance(raw, dict) else {}
    choices = raw.get('events') if isinstance(raw.get('events'), dict) else {}
    delay = raw.get('delete_delay')
    return {
        'enabled': raw['enabled'] if type(raw.get('enabled')) is bool else True,
        'sound': raw['sound'] if type(raw.get('sound')) is bool else True,
        'delete_delay': delay if type(delay) is int and delay in DELETE_DELAYS else 10,
        'events': {
            event: choices[event] if type(choices.get(event)) is bool else default
            for event, default in DEFAULT_EVENTS.items()
        },
    }


def changed_events(before, after, completed=None):
    """Return event names in chronological order; never infer a wager from arming."""
    if not before.get('enabled') or not after.get('enabled'):
        return []
    notices = []
    if (before.get('phase') in {'waiting_reference', 'waiting'}
            and after.get('reference_id')
            and after.get('reference_id') != before.get('reference_id')):
        notices.append('reference')
    if (before.get('phase') != 'betting' and after.get('phase') == 'betting'
            and not before.get('rounds') and not after.get('rounds')):
        notices.append('threshold')
    if (before.get('phase') == 'betting'
            and before.get('started_at') is None
            and (after.get('started_at') is not None
                 or (completed and completed.get('rounds')))):
        # started_at is set only by the first actual wager. Unlike the
        # mutable rounds list, this scalar snapshot remains stable.
        # An immediate win clears started_at but supplies the final cycle.
        notices.append('betting')
    if completed and completed.get('outcome') == 'recovered':
        notices.append('goal')
    if completed and completed.get('outcome') in ('bet_limit', 'active_filter_limit'):
        notices.append('safe')
    return notices


def format_event(event, high, currency, completed=None):
    reference = str(high.get('reference', '100'))
    target = str(high.get('target', '100'))
    wait = int(high.get('wait_rounds', 100))
    if event == 'reference':
        return (
            'Стратегия №2 · отслеживание\n'
            f'Опорный коэффициент: x{high.get("reference_value")}\n'
            f'Ожидание: {wait} раундов'
        )
    if event == 'threshold':
        return (
            'Стратегия №2 · порог достигнут\n'
            f'После коэффициента >x{reference} прошло {wait} раундов.\n'
            'DEMO готово к ставкам со следующего раунда.'
        )
    if event == 'betting':
        balance = (
            completed.get('balance_before_cents') if completed
            else high.get('balance_before_cents')
        )
        if balance is None:
            return (
                'Стратегия №2 · DEMO запущено\n'
                f'Ставка: {format_money(high["stake_cents"], currency)}\n'
                f'Цель: ≥x{target}'
            )
        return (
            'Стратегия №2 · DEMO запущено\n'
            f'Ставка: {format_money(high["stake_cents"], currency)}\n'
            f'Цель: ≥x{target}\n'
            f'Баланс перед первой ставкой: {format_money(balance, currency)}'
        )
    if event == 'goal' and completed:
        net = completed.get('net_cents')
        net_text = ('+' if net is not None and net > 0 else '') + (
            format_money(net, currency) if net is not None else 'неизвестно'
        )
        return (
            'Стратегия №2 · цель достигнута\n'
            f'Выпал коэффициент: x{completed.get("coefficient")}\n'
            f'Сделано ставок: {len(completed.get("rounds") or [])}\n'
            f'Результат цикла: {net_text}\n'
            'Цикл завершён. Ожидание нового опорного X.'
        )
    if event == 'safe' and completed:
        net = completed.get('net_cents')
        net_text = ('+' if net is not None and net > 0 else '') + (
            format_money(net, currency) if net is not None else 'неизвестно'
        )
        reason = ('лимит ставок' if completed.get('outcome') == 'bet_limit'
                  else 'фильтр X во время ставок')
        return (
            'Стратегия №2 · сейв сработал\n'
            f'Причина: {reason}\n'
            f'Ставок: {len(completed.get("rounds") or [])}\n'
            f'Итог: {net_text}\n'
            'Стратегия включена. Ожидание нового опорного X.'
        )
    raise ValueError('Unknown Strategy 2 notification event')
