"""Independent Telegram notices for the virtual Strategy 4.

Messages are derived from the persisted state transition, never speculative.
"""
from luckyjet_signal.demo_money import format_money

EVENTS = ('armed', 'betting', 'loss', 'goal', 'safe', 'stopped')
DELETE_DELAYS = (0, 10, 30, 60)
DEFAULT_EVENTS = {
    'armed': True, 'betting': True, 'loss': False,
    'goal': True, 'safe': True, 'stopped': True,
}


def normalize(raw=None):
    raw = raw if isinstance(raw, dict) else {}
    configured = raw.get('events') if isinstance(raw.get('events'), dict) else {}
    delay = raw.get('delete_delay')
    return {
        'enabled': raw['enabled'] if type(raw.get('enabled')) is bool else True,
        'sound': raw['sound'] if type(raw.get('sound')) is bool else True,
        'delete_delay': delay if type(delay) is int and delay in DELETE_DELAYS else 10,
        'events': {
            event: configured[event] if type(configured.get(event)) is bool else default
            for event, default in DEFAULT_EVENTS.items()
        },
    }


def changed_events(before, after, completed=None):
    """Derive alerts strictly from confirmed ledger transitions and settled bets."""
    if not before.get('enabled'):
        return []
    results = []
    previous_steps = int(before.get('steps', 0) or 0)
    current_steps = int(after.get('steps', 0) or 0)
    completed_rounds = (completed or {}).get('rounds') or []
    new_wager = (current_steps > previous_steps or len(completed_rounds) > previous_steps)
    if before.get('phase') == 'waiting' and after.get('phase') == 'betting':
        results.append('armed')
    if before.get('phase') == 'betting' and previous_steps == 0 and new_wager:
        results.append('betting')
    if before.get('phase') == 'betting' and new_wager and (
            not completed or
            (completed_rounds and not completed_rounds[-1].get('payout_cents'))):
        results.append('loss')
    if completed and completed.get('outcome') == 'recovered':
        results.append('goal')
    elif completed and completed.get('outcome') == 'safe_limit':
        results.append('safe')
    elif completed and completed.get('outcome') in (
            'insufficient_balance', 'incomplete_data', 'stopped_by_user'):
        results.append('stopped')
    return results


def format_event(event, state, currency, completed=None):
    x = state.get('target', '1.57')
    trigger = state.get('trigger', 3)
    stake = state.get('stake_cents', 0)
    if event == 'armed':
        return (f'Стратегия №4 · серия достигнута\n'
                f'{trigger} подряд ниже x{x}. Ставка — со следующего раунда.')
    if event == 'betting':
        return (f'Стратегия №4 · DEMO запущено\n'
                f'Фиксированная ставка: {format_money(stake, currency)}\n'
                f'Вывод: x{x}')
    if event == 'loss':
        steps = (len(completed.get('rounds') or []) if completed else state.get('steps', 0))
        return (f'Стратегия №4 · ставка проиграна\n'
                f'Ставок в текущей сессии: {steps}\n'
                f'Сейф: {state.get("safe_losses", 2) or "Выкл"}\n'
                f'Следующая автоставка: {format_money(stake, currency)}')
    if event == 'safe' and completed:
        return (f'Стратегия №4 · сейф сработал, сессия закрыта\n'
                f'Проигрышей: {completed.get("losses", len(completed.get("rounds") or []))}\n'
                f'Чистый итог: {format_money(completed.get("net_cents"), currency)}\n'
                f'Новый вход: после {trigger} подряд ниже x{x}')
    if event == 'goal' and completed:
        result = completed.get('net_cents')
        return (f'Стратегия №4 · целевой X достигнут, цикл завершён\n'
                f'Коэффициент: x{completed.get("coefficient")}\n'
                f'Шагов: {len(completed.get("rounds") or [])}\n'
                f'Чистый итог: {format_money(result, currency)}')
    if event == 'stopped' and completed:
        return (f'Стратегия №4 · DEMO остановлено\n'
                f'Причина: {completed.get("outcome")}\n'
                f'Шагов: {len(completed.get("rounds") or [])}')
    raise ValueError('Unknown Strategy 4 event')
