"""User-scoped Mini App controls for virtual DEMO and Telegram notification settings."""
import asyncio
import json
import time
import secrets
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo
from redis.exceptions import RedisError
from luckyjet_signal.demo_money import parse_money
from round_archive import STREAM_KEY

import httpx

from mini_app_api import json_response, telegram_user
from strategy_tiles import strategy_tile_snapshot
from luckyjet_signal.demo_ledger import DemoLedger, TRIGGER_OPTIONS, parse_dollars
from luckyjet_signal.demo_money import MAX_AMOUNT
from high_gap_notifications import EVENTS as HIGH_GAP_NOTICE_EVENTS, DELETE_DELAYS as HIGH_GAP_NOTICE_DELAYS, normalize as normalize_high_gap_notices
from target_streak_notifications import EVENTS as TARGET_NOTICE_EVENTS, DELETE_DELAYS as TARGET_NOTICE_DELAYS, normalize as normalize_target_notices
from recovery_streak_notifications import EVENTS as RECOVERY_NOTICE_EVENTS, DELETE_DELAYS as RECOVERY_NOTICE_DELAYS, normalize as normalize_recovery_notices


ACTIONS = {
    'demo_toggle', 'demo_trigger', 'demo_stake', 'demo_reset',
    'high_gap_toggle', 'high_gap_config',
    'target_streak_toggle', 'target_streak_config',
    'recovery_streak_toggle', 'recovery_streak_config',
    'recovery_notice_toggle', 'recovery_notice_sound',
    'recovery_notice_delete_delay', 'recovery_notice_event', 'recovery_notice_test',
    'target_notice_toggle', 'target_notice_sound', 'target_notice_delete_delay',
    'target_notice_event', 'target_notice_test',
    'high_gap_notice_toggle', 'high_gap_notice_sound',
    'high_gap_notice_delete_delay', 'high_gap_notice_event', 'high_gap_notice_test',
    'notifications_toggle', 'sound_toggle', 'notification_threshold',
    'history_size', 'delete_delay', 'settings_reset', 'test_notification',
    'demo_probe_start', 'history_reset',
}


def snapshot(gate, uid):
    ledger = gate.demo_ledgers.get(uid) or DemoLedger()
    probe = getattr(gate, 'demo_probe', None)
    return {
        'admin': gate.is_admin(uid),
        'strategy_tiles': strategy_tile_snapshot(ledger),
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
        'high_gap': ledger.high_gap.public(),
        'target_streak': ledger.target_streak.public(ledger.currency),
        'recovery_streak': ledger.recovery_streak.public(ledger.currency),
        'recovery_streak_notifications': normalize_recovery_notices(gate.recovery_notice_settings.get(uid)),
        'target_streak_notifications': normalize_target_notices(gate.target_notice_settings.get(uid)),
        'high_gap_notifications': normalize_high_gap_notices(gate.high_gap_notice_settings.get(uid)),
        'settings': {
            'notifications': uid not in gate.notifications_disabled,
            'sound': uid not in gate.silent_users,
            'threshold': gate.user_threshold(uid),
            'history_size': gate.user_history_size(uid),
            'delete_delay': gate.user_delete_seconds(uid),
        },
    }



async def _seed_high_gap(redis, ledger):
    """Anchor to last validated round already processed by this user.

    A current source snapshot is never replayed as if it were a paid bet.
    Stop on a missing-history marker; gaps cannot be silently bridged.
    """
    cursor = ledger.last_archived_stream_id
    if not cursor:
        return False
    events = await redis.xrevrange('luckyjet:archive:v1:events', max=cursor, count=20000)
    elapsed = 0
    after_anchor = []  # newest first; only the first waiting window is filtered
    for stream_id, fields in events:
        kind = str(fields.get('type', ''))
        if kind in {'gap', 'baseline'}:
            break
        if kind != 'round':
            continue
        try:
            coefficient = Decimal(str(fields.get('coefficient', '')))
        except (InvalidOperation, ValueError, TypeError):
            break
        if not coefficient.is_finite() or coefficient < 1:
            break
        if coefficient > ledger.high_gap.reference:
            strategy = ledger.high_gap
            window = after_anchor[-strategy.wait_rounds:] if strategy.wait_rounds else []
            matching = sum(c > strategy.filter_threshold for c in window)
            if strategy.filter_enabled and matching >= strategy.filter_limit:
                # The latest anchor was invalidated. Never resurrect an older one.
                strategy.filter_rejected = True
                return False
            observed = fields.get('observed_at', '')
            try:
                at = datetime.fromtimestamp(int(observed) / 1000, timezone.utc).isoformat()
            except (TypeError, ValueError, OverflowError, OSError):
                at = None
            ledger.high_gap.arm_from_source(
                round_id=fields.get('round_id', ''),
                coefficient=coefficient, elapsed=elapsed,
                observed_at=at, filter_count=matching if strategy.filter_enabled else 0,
            )
            return True
        after_anchor.append(coefficient)
        elapsed += 1
    return False

async def _seed_target_streak(redis, ledger):
    """Seed the current streak from confirmed history, never charge old bets."""
    if redis is None or not ledger.last_archived_stream_id:
        return False
    events = await redis.xrevrange(STREAM_KEY, max=ledger.last_archived_stream_id,
                                   count=ledger.target_streak.trigger + 2)
    prior = []
    for _id, fields in events:
        if fields.get('type') in ('gap', 'baseline'):
            break
        if fields.get('type') == 'round':
            prior.append(fields.get('coefficient'))
        if len(prior) >= ledger.target_streak.trigger:
            break
    ledger.target_streak.seed_streak(prior)
    return True


async def _seed_recovery_streak(redis, ledger):
    """Seed the current streak from confirmed history, never charge old bets."""
    if redis is None or not ledger.last_archived_stream_id:
        return False
    events = await redis.xrevrange(STREAM_KEY, max=ledger.last_archived_stream_id,
                                   count=ledger.recovery_streak.trigger + 2)
    prior = []
    for _id, fields in events:
        if fields.get('type') in ('gap', 'baseline'):
            break
        if fields.get('type') == 'round':
            prior.append(fields.get('coefficient'))
        if len(prior) >= ledger.recovery_streak.trigger:
            break
    ledger.recovery_streak.seed_streak(prior)
    return True


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
        elif action == 'target_streak_toggle':
            strategy = ledger.target_streak
            if strategy.enabled:
                finished = strategy.stop(balance=ledger.balance_cents,
                                         currency=ledger.currency)
                if finished:
                    ledger.target_history.insert(0, finished)
                    ledger.target_history = ledger.target_history[:100]
            else:
                if ledger.balance_verified is False:
                    return json_response('409 Conflict', {
                        'error': 'Баланс не подтверждён. Сначала сбрось DEMO.',
                    })
                if not ledger.last_archived_stream_id:
                    return json_response('409 Conflict', {
                        'error': 'Ожидание первого подтверждённого раунда.',
                    })
                strategy.reset_progress()
                strategy.enabled = True
                await _seed_target_streak(gate.redis, ledger)
            await gate._persist_demo(uid)
        elif action == 'target_streak_config':
            if not isinstance(value, dict) or set(value) != {'target', 'trigger', 'stake'}:
                return json_response('400 Bad Request', {
                    'error': 'Укажи целевой X, серию подряд и первую ставку.',
                })
            amount = parse_money(value['stake'], ledger.currency)
            if (amount is None or not isinstance(value['target'], str)
                    or type(value['trigger']) is not int
                    or amount > ledger.balance_cents):
                return json_response('400 Bad Request', {'error': 'Проверь ставку и параметры.'})
            if not ledger.target_streak.configure(
                target=value['target'], trigger=value['trigger'], stake_cents=amount,
                currency=ledger.currency,
            ):
                return json_response('409 Conflict', {
                    'error': 'Недопустимый X (1.01–10000) или активный цикл.',
                })
            if ledger.target_streak.enabled:
                await _seed_target_streak(gate.redis, ledger)
            await gate._persist_demo(uid)
        elif action == 'recovery_streak_toggle':
            strategy = ledger.recovery_streak
            if strategy.enabled:
                finished = strategy.stop(balance=ledger.balance_cents,
                                         currency=ledger.currency)
                if finished:
                    ledger.recovery_history.insert(0, finished)
                    ledger.recovery_history = ledger.recovery_history[:100]
            else:
                if ledger.balance_verified is False:
                    return json_response('409 Conflict', {
                        'error': 'Баланс не подтверждён. Сначала сбрось DEMO.',
                    })
                if not ledger.last_archived_stream_id:
                    return json_response('409 Conflict', {
                        'error': 'Ожидание первого подтверждённого раунда.',
                    })
                strategy.reset_progress()
                strategy.enabled = True
                await _seed_recovery_streak(gate.redis, ledger)
            await gate._persist_demo(uid)
        elif action == 'recovery_streak_config':
            allowed = {'target', 'trigger', 'stake', 'safe_losses',
                       'target_max', 'safe_mode', 'continuous_session',
                       'entry_range_enabled', 'cashout_target'}
            if (not isinstance(value, dict) or
                    not {'target', 'trigger', 'stake'} <= set(value) or
                    not set(value) <= allowed):
                return json_response('400 Bad Request', {
                    'error': 'Укажи целевой X, серию подряд, ставку и сейф.',
                })
            amount = parse_money(value['stake'], ledger.currency)
            safe_losses = value.get('safe_losses', ledger.recovery_streak.safe_losses)
            target_max = value.get('target_max')
            safe_mode = value.get('safe_mode')
            continuous = value.get('continuous_session')
            range_mode = value.get('entry_range_enabled')
            cashout_target = value.get('cashout_target')
            if (range_mode is True and cashout_target is None):
                return json_response('400 Bad Request', {
                    'error': 'Укажи отдельный коэффициент вывода.',
                })
            if (amount is None or not isinstance(value['target'], str)
                    or (target_max is not None and not isinstance(target_max, str))
                    or type(value['trigger']) is not int
                    or type(safe_losses) is not int or not 0 <= safe_losses <= 10
                    or (safe_mode is not None and safe_mode not in ('total', 'consecutive'))
                    or (continuous is not None and type(continuous) is not bool)
                    or (range_mode is not None and type(range_mode) is not bool)
                    or (cashout_target is not None and not isinstance(cashout_target, str))
                    or amount > ledger.balance_cents):
                return json_response('400 Bad Request', {
                    'error': 'Проверь ставку, серию, диапазон X и сейв.',
                })
            if not ledger.recovery_streak.configure(
                target=value['target'], target_max=target_max,
                trigger=value['trigger'], stake_cents=amount,
                currency=ledger.currency, safe_losses=safe_losses,
                safe_mode=safe_mode, continuous_session=continuous,
                entry_range_enabled=range_mode, cashout_target=cashout_target,
            ):
                return json_response('409 Conflict', {
                    'error': 'Проверь диапазон входа, X вывода или активный цикл.',
                })
            if ledger.recovery_streak.enabled:
                await _seed_recovery_streak(gate.redis, ledger)
            await gate._persist_demo(uid)
        elif action == 'high_gap_toggle':
            if ledger.high_gap.enabled:
                completed = ledger.high_gap.stop(
                    currency=ledger.currency, balance=ledger.balance_cents,
                )
                if completed:
                    ledger.high_history.insert(0, completed)
                    ledger.high_history = ledger.high_history[:100]
            else:
                if ledger.balance_verified is False:
                    return json_response('409 Conflict', {
                        'error': 'Баланс непроверен после пропуска раундов. Сначала сбрось DEMO.',
                    })
                if not ledger.last_archived_stream_id:
                    return json_response('409 Conflict', {
                        'error': 'Ожидание первого подтверждённого раунда. Повтори включение.',
                    })
                ledger.high_gap.reset_progress()
                ledger.high_gap.enabled = True
                await _seed_high_gap(gate.redis, ledger)
            await gate._persist_demo(uid)
        elif action == 'high_gap_config':
            base_fields = {'reference', 'wait_rounds', 'target', 'stake'}
            filter_fields = {'filter_enabled', 'filter_threshold', 'filter_limit'}
            safe_fields = {'bets_limit_enabled', 'bets_limit', 'active_filter_enabled',
                           'active_filter_threshold', 'active_filter_window',
                           'active_filter_limit'}
            if (not isinstance(value, dict) or not base_fields <= set(value)
                    or not set(value) <= (base_fields | filter_fields | safe_fields)):
                return json_response('400 Bad Request', {
                    'error': 'Укажи порог, число раундов, автовывод и сумму.',
                })
            stake = parse_money(value['stake'], ledger.currency)
            reference = value['reference']
            target = value['target']
            wait = value['wait_rounds']
            if stake is None or not isinstance(reference, str) or not isinstance(target, str) or type(wait) is not int:
                return json_response('400 Bad Request', {'error': 'Проверь настройки стратегии.'})
            from luckyjet_signal.high_gap_demo import parse_multiplier
            target_multiplier = parse_multiplier(target)
            if target_multiplier is None or Decimal(stake) * target_multiplier > MAX_AMOUNT:
                return json_response('400 Bad Request', {
                    'error': 'Автовывод превышает допустимую точность виртуального баланса.',
                })
            if not ledger.high_gap.configure(
                reference, wait, target, stake,
                min_stake=ledger.minimum_stake_cents,
                filter_enabled=value.get('filter_enabled'),
                filter_threshold=value.get('filter_threshold'),
                filter_limit=value.get('filter_limit'),
                bets_limit_enabled=value.get('bets_limit_enabled'),
                bets_limit=value.get('bets_limit'),
                active_filter_enabled=value.get('active_filter_enabled'),
                active_filter_threshold=value.get('active_filter_threshold'),
                active_filter_window=value.get('active_filter_window'),
                active_filter_limit=value.get('active_filter_limit'),
            ):
                return json_response('409 Conflict', {
                    'error': 'Некорректные параметры или активный цикл. Дождись завершения.',
                })
            if ledger.high_gap.enabled:
                await _seed_high_gap(gate.redis, ledger)
            await gate._persist_demo(uid)
        elif action == 'demo_toggle':
            if ledger.active and ledger.enabled:
                # Explicit OFF must stop Strategy 1 immediately and archive its
                # real incurred net, without touching the other strategies.
                ledger.finish_active('stopped_by_user')
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
        elif action in {'target_notice_toggle', 'target_notice_sound',
                         'target_notice_delete_delay', 'target_notice_event'}:
            prefs = normalize_target_notices(gate.target_notice_settings.get(uid))
            if action == 'target_notice_toggle':
                prefs['enabled'] = not prefs['enabled']
            elif action == 'target_notice_sound':
                prefs['sound'] = not prefs['sound']
            elif action == 'target_notice_delete_delay':
                if type(value) is not int or value not in TARGET_NOTICE_DELAYS:
                    return json_response('400 Bad Request', {'error': 'Удаление: 0, 10, 30 или 60 секунд.'})
                prefs['delete_delay'] = value
            else:
                if not isinstance(value, str) or value not in TARGET_NOTICE_EVENTS:
                    return json_response('400 Bad Request', {'error': 'Недопустимое событие №3.'})
                prefs['events'][value] = not prefs['events'][value]
            gate.target_notice_settings[uid] = prefs
            await gate._persist_user(uid)
        elif action == 'target_notice_test':
            if gate.chats.get(uid) != uid or not callable(gate.request):
                return json_response('409 Conflict', {'error': 'Открой личный чат бота через /start.'})
            sent = getattr(gate, '_mini_app_target_test_sent_at', None)
            if sent is None:
                gate._mini_app_target_test_sent_at = sent = {}
            now = time.monotonic()
            if now - sent.get(uid, float('-inf')) < 30:
                return json_response('429 Too Many Requests', {'error': 'Тест доступен раз в 30 секунд.'})
            if not await gate._send_target_notice(uid, 'test',
                    'Тест уведомлений стратегии №3', test=True):
                return json_response('502 Bad Gateway', {'error': 'Telegram не принял сообщение.'})
            sent[uid] = now
            message = 'Тест уведомлений стратегии №3 отправлен.'
        elif action in {'recovery_notice_toggle', 'recovery_notice_sound',
                         'recovery_notice_delete_delay', 'recovery_notice_event'}:
            prefs = normalize_recovery_notices(gate.recovery_notice_settings.get(uid))
            if action == 'recovery_notice_toggle':
                prefs['enabled'] = not prefs['enabled']
            elif action == 'recovery_notice_sound':
                prefs['sound'] = not prefs['sound']
            elif action == 'recovery_notice_delete_delay':
                if type(value) is not int or value not in RECOVERY_NOTICE_DELAYS:
                    return json_response('400 Bad Request', {'error': 'Удаление: 0, 10, 30 или 60 секунд.'})
                prefs['delete_delay'] = value
            else:
                if not isinstance(value, str) or value not in RECOVERY_NOTICE_EVENTS:
                    return json_response('400 Bad Request', {'error': 'Недопустимое событие №4.'})
                prefs['events'][value] = not prefs['events'][value]
            gate.recovery_notice_settings[uid] = prefs
            await gate._persist_user(uid)
        elif action == 'recovery_notice_test':
            if gate.chats.get(uid) != uid or not callable(gate.request):
                return json_response('409 Conflict', {'error': 'Открой личный чат бота через /start.'})
            sent = getattr(gate, '_mini_app_target_test_sent_at', None)
            if sent is None:
                gate._mini_app_target_test_sent_at = sent = {}
            now = time.monotonic()
            if now - sent.get(uid, float('-inf')) < 30:
                return json_response('429 Too Many Requests', {'error': 'Тест доступен раз в 30 секунд.'})
            if not await gate._send_recovery_notice(uid, 'test',
                    'Тест уведомлений стратегии №4', test=True):
                return json_response('502 Bad Gateway', {'error': 'Telegram не принял сообщение.'})
            sent[uid] = now
            message = 'Тест уведомлений стратегии №4 отправлен.'
        elif action in {'high_gap_notice_toggle', 'high_gap_notice_sound',
                        'high_gap_notice_delete_delay', 'high_gap_notice_event'}:
            prefs = normalize_high_gap_notices(gate.high_gap_notice_settings.get(uid))
            if action == 'high_gap_notice_toggle':
                prefs['enabled'] = not prefs['enabled']
            elif action == 'high_gap_notice_sound':
                prefs['sound'] = not prefs['sound']
            elif action == 'high_gap_notice_delete_delay':
                if type(value) is not int or value not in HIGH_GAP_NOTICE_DELAYS:
                    return json_response('400 Bad Request', {'error': 'Удаление: 0, 10, 30 или 60 секунд.'})
                prefs['delete_delay'] = value
            else:
                if not isinstance(value, str) or value not in HIGH_GAP_NOTICE_EVENTS:
                    return json_response('400 Bad Request', {'error': 'Неизвестное событие стратегии №2.'})
                prefs['events'][value] = not prefs['events'][value]
            gate.high_gap_notice_settings[uid] = prefs
            await gate._persist_user(uid)
        elif action == 'high_gap_notice_test':
            if gate.chats.get(uid) != uid or not callable(gate.request):
                return json_response('409 Conflict', {'error': 'Открой личный чат бота через /start.'})
            last = getattr(gate, '_mini_app_test_sent_at', None)
            if last is None:
                gate._mini_app_test_sent_at = last = {}
            now = time.monotonic()
            if now - last.get(uid, float('-inf')) < 30:
                return json_response('429 Too Many Requests', {'error': 'Тест доступен раз в 30 секунд.'})
            if not await gate._send_high_gap_notice(
                uid, 'test', 'Тест уведомлений стратегии №2', test=True,
            ):
                return json_response('502 Bad Gateway', {'error': 'Telegram не принял тестовое сообщение.'})
            last[uid] = now
            message = 'Тест стратегии №2 отправлен в Telegram.'
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
            gate.high_gap_notice_settings.pop(uid, None)
            gate.target_notice_settings.pop(uid, None)
            gate.recovery_notice_settings.pop(uid, None)
            await gate._persist_user(uid)
            message = 'Настройки восстановлены; DEMO сохранено.'
        return json_response('200 OK', {'message': message, **snapshot(gate, uid)})
