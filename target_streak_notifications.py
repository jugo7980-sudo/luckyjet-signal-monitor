"""Independent Telegram notices for the virtual Strategy 3.

Messages are derived from the persisted state transition, never speculative.
"""
from luckyjet_signal.demo_money import format_money

EVENTS = ('armed', 'betting', 'loss', 'goal', 'stopped')
DELETE_DELAYS = (0, 10, 30, 60)
DEFAULT_EVENTS = {
    'armed': True, 'betting': True, 'loss': False,
    'goal': True, 'stopped': True,
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
    """Use only state confirmed by an actual archived round."""
    if not before.get('enabled'):
        return []
    results = []
    if before.get('phase') == 'waiting' and after.get('phase') == 'betting':
        results.append('armed')
    if (before.get('phase') == 'betting' and before.get('steps', 0) == 0 and
            (after.get('steps', 0) > 0 or (completed and completed.get('rounds')))):
        results.append('betting')
    if (before.get('phase') == 'betting' and after.get('steps', 0) > before.get('steps', 0)
            and not completed):
        results.append('loss')
    if completed and completed.get('outcome') == 'recovered':
        results.append('goal')
    elif completed and completed.get('outcome') in (
            'insufficient_balance', 'incomplete_data', 'stopped_by_user'):
        results.append('stopped')
    return results


def format_event(event, state, currency, completed=None):
    x = state.get('target', '1.57')
    trigger = state.get('trigger', 3)
    stake = state.get('stake_cents', 0)
    if event == 'armed':
        return (f'Стратегия №3 · серия достигнута\n'
                f'{trigger} подряд ниже x{x}. Ставка — со следующего раунда.')
    if event == 'betting':
        return (f'Стратегия №3 · DEMO запущено\n'
                f'Первая ставка: {format_money(stake, currency)}\n'
                f'Вывод: x{x}')
    if event == 'loss':
        return (f'Стратегия №3 · ставка проиграна\n'
                f'Ставок: {state.get("steps", 0)}\n'
                f'Следующая ставка: {format_money(state.get("next_stake_cents", 0), currency)}')
    if event == 'goal' and completed:
        result = completed.get('net_cents')
        return (f'Стратегия №3 · цель достигнута\n'
                f'Коэффициент: x{completed.get("coefficient")}\n'
                f'Шагов: {len(completed.get("rounds") or [])}\n'
                f'Чистый итог: {format_money(result, currency)}')
    if event == 'stopped' and completed:
        return (f'Стратегия №3 · DEMO остановлено\n'
                f'Причина: {completed.get("outcome")}\n'
                f'Шагов: {len(completed.get("rounds") or [])}')
    raise ValueError('Unknown Strategy 3 event')
