"""User-scoped Mini App controls for virtual DEMO and Telegram notification settings."""
import asyncio
import json
import time
import secrets
from datetime import datetime
from zoneinfo import ZoneInfo
from redis.exceptions import RedisError
from luckyjet_signal.demo_money import parse_money
from round_archive import STREAM_KEY

import httpx

from mini_app_api import json_response, telegram_user
from luckyjet_signal.demo_ledger import DemoLedger, TRIGGER_OPTIONS, parse_dollars

ACTIONS = {
    'demo_toggle', 'demo_trigger', 'demo_stake', 'demo_reset',
    'notifications_toggle', 'sound_toggle', 'notification_threshold',
    'history_size', 'delete_delay', 'settings_reset', 'test_notification',
    'demo_probe_start', 'history_reset',
}


def snapshot(gate, uid):
    ledger = gate.demo_ledgers.get(uid) or DemoLedger()
    probe = getattr(gate, 'demo_probe', None)
    return {
        'admin': gate.is_admin(uid),
        'probe': {
            'state': probe.result,
            'requested': probe.requested,
            'running': bool(probe.task and not probe.task.done()),
            'checked_at': probe.checked_at,
            'source': probe.source,
        } if probe else None,
        'demo': {
            'currency': ledger.currency,
            'reset_revision': ledger.reset_revision,
            'initial_balance_cents': ledger.initial_balance_cents,
            'enabled': ledger.enabled,
            'active': ledger.active,
            'balance_cents': ledger.balance_cents, 'balance_verified': ledger.balance_verified,
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
        if action == 'history_reset':
            # Reset only the caller's visible history. Never delete source rounds,
            # global counters, or the DEMO ledger. All four scopes share one
            # append-only cutoff marker for a consistent all-tabs reset.
            if payload.get('confirm') is not True or value not in {'days', 'high_days', 'cycles', 'max_steps', 'all'}:
                return json_response('400 Bad Request', {'error': 'Подтверди вкладку для сброса.'})
            try:
                head = await gate.redis.xrevrange(STREAM_KEY, count=1)
                marker = json.dumps({
                    'stream_id': str(head[0][0]) if head else '0-0',
                    'at_ms': int(time.time() * 1000),
                    'day': datetime.now(ZoneInfo('Asia/Vladivostok')).date().isoformat(),
                })
                scopes = ('days', 'high_days', 'cycles', 'max_steps') if value == 'all' else (value,)
                await gate.redis.hset(f'luckyjet:history:user-reset:{uid}',
                                      mapping={scope: marker for scope in scopes})
            except (RedisError, OSError):
                return json_response('503 Service Unavailable', {'error': 'Не удалось сохранить сброс истории.'})
            message = 'История вкладок очищена. Исходный архив и DEMO сохранены.' if value == 'all' else 'Вкладка очищена. Исходный архив сохранён.'
        elif action == 'demo_probe_start':
            if not gate.is_admin(uid):
                return json_response('403 Forbidden', {'error': 'Проверка доступна администратору.'})
            probe = getattr(gate, 'demo_probe', None)
            if probe is None:
                return json_response('503 Service Unavailable', {'error': 'Диагностика не подключена.'})
            if probe.task is not None and not probe.task.done():
                return json_response('409 Conflict', {'error': 'Проверка уже выполняется.'})
            if time.monotonic() - probe.started_at < 60:
                return json_response('429 Too Many Requests', {'error': 'Повторная проверка через минуту.'})
            probe.start()
            message = 'Проверка запущена. Результат появится после чтения страницы мониторинга.'
        elif action == 'demo_toggle':
            if ledger.active and ledger.enabled:
                return json_response('409 Conflict', {'error': 'Активную DEMO-сессию нельзя выключить до завершения.'})
            was_enabled = ledger.enabled
            ledger.enabled = not ledger.enabled
            if ledger.enabled and not was_enabled:
                ledger.sync_history(gate.history_provider() if gate.history_provider else [])
            await gate._persist_demo(uid)
        elif action == 'demo_trigger':
            if type(value) is not int or value not in TRIGGER_OPTIONS:
                return json_response('400 Bad Request', {'error': 'Допустимое число подряд: 2–10.'})
            if not ledger.set_trigger(value):
                return json_response('409 Conflict', {'error': 'Условие нельзя изменить во время активного цикла.'})
            if ledger.enabled:
                ledger.sync_history(gate.history_provider() if gate.history_provider else [])
            await gate._persist_demo(uid)
        elif action == 'demo_stake':
            amount = parse_money(value, ledger.currency)
            if amount is None or not ledger.set_base_stake(amount):
                return json_response('409 Conflict', {'error': 'Ставка должна соответствовать валюте, быть не выше баланса и меняться только между циклами.'})
            await gate._persist_demo(uid)
        elif action == 'demo_reset':
            if payload.get('confirm') is not True:
                return json_response('400 Bad Request', {'error': 'Требуется подтверждение сброса.'})
            if not isinstance(value, dict) or set(value) != {'currency', 'balance', 'stake', 'revision'}:
                return json_response('400 Bad Request', {'error': 'Выбери валюту и введи баланс и ставку.'})
            currency = value['currency']
            if not isinstance(currency, str):
                return json_response('400 Bad Request', {'error': 'Недопустимая валюта.'})
            balance = parse_money(value['balance'], currency)
            stake = parse_money(value['stake'], currency)
            if balance is None or stake is None or stake > balance:
                return json_response('400 Bad Request', {'error': 'Проверь суммы: баланс и ставка должны быть положительными, ставка не выше баланса; воны — целые.'})
            if value['revision'] != ledger.reset_revision:
                return json_response('409 Conflict', {'error': 'Счёт уже сброшен. Обнови страницу перед новым сбросом.'})
            replacement = DemoLedger(**ledger.dump())
            replacement.reset(currency=currency, balance_cents=balance,
                              base_stake_cents=stake, reset_revision=secrets.token_hex(16))
            try:
                head = await gate.redis.xrevrange(STREAM_KEY, count=1)
                replacement.demo_after_stream_id = head[0][0] if head else ledger.last_archived_stream_id
                await gate._demo_storage.save(gate.redis, uid, replacement)
            except (RedisError, OSError):
                return json_response('503 Service Unavailable', {'error': 'Не удалось сохранить сброс. Обнови страницу и проверь состояние счёта.'})
            gate.demo_ledgers[uid] = replacement
            gate._demo_history_views.pop(uid, None)
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
        elif action == 'test_notification':
            # Never send a test to another chat or let a user spam Telegram.
            if gate.chats.get(uid) != uid or not callable(gate.request):
                return json_response('409 Conflict', {'error': 'Открой личный чат бота через /start.'})
            last = getattr(gate, '_mini_app_test_sent_at', None)
            if last is None:
                gate._mini_app_test_sent_at = last = {}
            now = time.monotonic()
            if now - last.get(uid, float('-inf')) < 30:
                return json_response('429 Too Many Requests', {'error': 'Тест доступен не чаще одного раза в 30 секунд.'})
            try:
                result = await gate.request('sendMessage', {
                    'chat_id': uid, 'text': '🧪 Тест уведомления Lucky Jet',
                    'disable_notification': uid in gate.silent_users,
                })
                message_id = result.get('result', {}).get('message_id')
            except (httpx.HTTPError, RuntimeError, KeyError, TypeError):
                return json_response('502 Bad Gateway', {'error': 'Telegram не принял тестовое сообщение.'})
            last[uid] = now
            if message_id is not None:
                task = asyncio.create_task(gate._schedule_delete(uid, int(message_id), 5))
                gate._deletion_tasks.add(task)
                task.add_done_callback(gate._deletion_tasks.discard)
            message = 'Тест отправлен в личный чат Telegram.'
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
