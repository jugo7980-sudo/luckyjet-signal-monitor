"""User-scoped Mini App controls for virtual DEMO and Telegram notification settings."""
import asyncio
import json

from mini_app_api import json_response, telegram_user
from luckyjet_signal.demo_ledger import DemoLedger, TRIGGER_OPTIONS, parse_dollars

ACTIONS = {
    'demo_toggle', 'demo_trigger', 'demo_stake', 'demo_reset',
    'notifications_toggle', 'sound_toggle', 'notification_threshold',
    'history_size', 'delete_delay', 'settings_reset',
}


def snapshot(gate, uid):
    ledger = gate.demo_ledgers.get(uid) or DemoLedger()
    return {
        'demo': {
            'enabled': ledger.enabled,
            'active': ledger.active,
            'balance_cents': ledger.balance_cents,
            'base_stake_cents': ledger.base_stake_cents,
            'trigger': ledger.trigger_streak,
            'low_streak': ledger.low_streak,
            'step': len(ledger.cycle_rounds) + 1 if ledger.active else None,
            'next_stake_cents': ledger.next_stake_cents if ledger.active else None,
        },
        'settings': {
            'notifications': uid not in gate.notifications_disabled,
            'sound': uid not in gate.silent_users,
            'threshold': gate.user_threshold(uid),
            'history_size': gate.user_history_size(uid),
            'delete_delay': gate.user_delete_seconds(uid),
        },
    }


async def controls(authorization, gate, token, method='GET', body=None):
    if not authorization.startswith('tma '):
        return json_response('401 Unauthorized', {'error': 'Открой приложение через Telegram.'})
    try:
        uid = telegram_user(authorization[4:], token)
    except (ValueError, KeyError, TypeError, AttributeError):
        return json_response('401 Unauthorized', {'error': 'Сеанс истёк. Переоткрой приложение.'})
    if gate is None:
        return json_response('503 Service Unavailable', {'error': 'Бот запускается.'})
    if not await gate.is_authorized(uid) or (uid in gate.blocked and not gate.is_admin(uid)):
        return json_response('403 Forbidden', {'error': 'Нет доступа.'})
    if method == 'GET':
        return json_response('200 OK', snapshot(gate, uid))
    if method != 'POST':
        return json_response('405 Method Not Allowed', {'error': 'Метод не поддерживается.'})
    if gate.redis is None:
        return json_response('503 Service Unavailable', {'error': 'Хранилище недоступно, изменение не сохранено.'})
    try:
        payload = json.loads(body or b'')
        if not isinstance(payload, dict) or set(payload) - {'action', 'value', 'confirm'}:
            raise ValueError('Invalid payload')
        action = payload.get('action')
        if action not in ACTIONS:
            raise ValueError('Unknown action')
    except (ValueError, TypeError, UnicodeDecodeError):
        return json_response('400 Bad Request', {'error': 'Недопустимая команда.'})

    locks = getattr(gate, '_mini_app_control_locks', None)
    if locks is None:
        gate._mini_app_control_locks = locks = {}
    lock = locks.setdefault(uid, asyncio.Lock())
    async with lock:
        ledger = gate._demo_ledger(uid)
        value = payload.get('value')
        message = 'Сохранено.'
        if action == 'demo_toggle':
            if ledger.active and ledger.enabled:
                return json_response('409 Conflict', {'error': 'Активную DEMO-сессию нельзя выключить до завершения.'})
            was_enabled = ledger.enabled
            ledger.enabled = not ledger.enabled
            if ledger.enabled and not was_enabled:
                ledger.sync_history(gate.history_provider() if gate.history_provider else [])
            await gate._persist_demo(uid)
        elif action == 'demo_trigger':
            if type(value) is not int or value not in TRIGGER_OPTIONS:
                return json_response('400 Bad Request', {'error': 'Допустимое число подряд: 2–6.'})
            if not ledger.set_trigger(value):
                return json_response('409 Conflict', {'error': 'Условие нельзя изменить во время активного цикла.'})
            if ledger.enabled:
                ledger.sync_history(gate.history_provider() if gate.history_provider else [])
            await gate._persist_demo(uid)
        elif action == 'demo_stake':
            amount = parse_dollars(value)
            if amount is None or not ledger.set_base_stake(amount):
                return json_response('409 Conflict', {'error': 'Ставка от $10, не выше баланса и только между циклами.'})
            await gate._persist_demo(uid)
        elif action == 'demo_reset':
            if payload.get('confirm') is not True:
                return json_response('400 Bad Request', {'error': 'Требуется подтверждение сброса.'})
            ledger.reset()
            gate._demo_history_views.pop(uid, None)
            await gate._persist_demo(uid)
            message = 'DEMO сброшено вместе с историей и статистикой.'
        elif action == 'notifications_toggle':
            if uid in gate.notifications_disabled:
                gate.notifications_disabled.discard(uid)
            else:
                gate.notifications_disabled.add(uid)
                await gate._clear_streak_notice(uid)
            await gate._persist_user(uid)
        elif action == 'sound_toggle':
            if uid in gate.silent_users:
                gate.silent_users.remove(uid)
            else:
                gate.silent_users.add(uid)
            await gate._persist_user(uid)
        elif action == 'notification_threshold':
            if type(value) is not int or value not in range(2, 11):
                return json_response('400 Bad Request', {'error': 'Допустимая серия: 2–10.'})
            gate.thresholds[uid] = value
            await gate._persist_user(uid)
        elif action == 'history_size':
            if type(value) is not int or value not in {20, 30, 50, 100}:
                return json_response('400 Bad Request', {'error': 'История: 20, 30, 50 или 100.'})
            gate.history_sizes[uid] = value
            await gate._persist_user(uid)
            await gate.ensure_private_history(uid)
        elif action == 'delete_delay':
            if type(value) is not int or value not in {0, 10, 30, 60}:
                return json_response('400 Bad Request', {'error': 'Допустимые значения: 0, 10, 30, 60 секунд.'})
            gate.delete_seconds[uid] = value
            await gate._persist_user(uid)
        elif action == 'settings_reset':
            if payload.get('confirm') is not True:
                return json_response('400 Bad Request', {'error': 'Требуется подтверждение сброса.'})
            gate.thresholds.pop(uid, None)
            gate.delete_seconds.pop(uid, None)
            gate.history_sizes.pop(uid, None)
            gate.notifications_disabled.discard(uid)
            gate.silent_users.discard(uid)
            await gate._persist_user(uid)
            message = 'Настройки восстановлены; DEMO сохранено.'
        return json_response('200 OK', {'message': message, **snapshot(gate, uid)})
