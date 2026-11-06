"""Admin-only Mini App user access management, reusing the bot's revoke logic.

Only verified, authorized bot administrators may read or mutate access.
Blocking/revoking uses the existing bot method, including its DEMO cleanup.
"""
import asyncio
import json
from urllib.parse import parse_qs

from mini_app_api import json_response, telegram_user

PAGE_SIZE = 20
ACTIONS = frozenset({'block', 'revoke', 'unblock'})


def user_state(gate, uid):
    return {
        'id': uid,
        'status': 'blocked' if uid in gate.blocked else
                  'authorized' if uid in gate.authorized else 'not_authorized',
    }


def list_users(gate, page):
    users = sorted((gate.authorized | gate.blocked) - {gate.admin_chat_id})
    total = len(users)
    return {
        'admin': True,
        'authorized_count': len(gate.authorized),
        'blocked_count': len(gate.blocked),
        'page': page,
        'page_size': PAGE_SIZE,
        'total': total,
        'users': [user_state(gate, uid) for uid in users[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]],
    }


async def admin(authorization, gate, token, method='GET', body=None, query=''):
    if not authorization.startswith('tma '):
        return json_response('401 Unauthorized', {'error': 'Открой приложение через Telegram.'})
    try:
        uid = telegram_user(authorization[4:], token)
    except (ValueError, KeyError, TypeError, AttributeError):
        return json_response('401 Unauthorized', {'error': 'Сеанс истёк. Переоткрой приложение.'})
    if gate is None:
        return json_response('503 Service Unavailable', {'error': 'Бот запускается.'})
    if not await gate.is_authorized(uid) or not gate.is_admin(uid):
        return json_response('403 Forbidden', {'error': 'Только администратор.'})

    if method == 'GET':
        try:
            args = parse_qs(query, keep_blank_values=True, max_num_fields=2)
            if set(args) - {'page'} or any(len(x) != 1 for x in args.values()):
                raise ValueError('Invalid page')
            raw = args.get('page', ['0'])[0]
            if not raw.isascii() or not raw.isdecimal() or len(raw) > 4:
                raise ValueError('Invalid page')
            page = int(raw)
        except ValueError:
            return json_response('400 Bad Request', {'error': 'Некорректная страница.'})
        return json_response('200 OK', list_users(gate, page))

    if method != 'POST':
        return json_response('405 Method Not Allowed', {'error': 'Метод не поддерживается.'})
    if gate.redis is None:
        return json_response('503 Service Unavailable', {'error': 'Хранилище недоступно.'})

    try:
        data = json.loads(body or b'')
        if not isinstance(data, dict) or set(data) != {'action', 'target', 'confirm', 'confirm_id'}:
            raise ValueError('Unexpected fields')
        action, target = data['action'], data['target']
        if action not in ACTIONS or type(target) is not int or not 0 < target <= 9223372036854775807:
            raise ValueError('Invalid action/target')
        if data['confirm'] is not True or data['confirm_id'] != str(target):
            raise ValueError('Confirmation mismatch')
    except (ValueError, TypeError, UnicodeDecodeError):
        return json_response('400 Bad Request', {'error': 'Нужно подтвердить действие для выбранного ID.'})

    # Always reject self-mutation, including unblock, even when already blocked.
    if gate.is_admin(target) or target == uid:
        return json_response('403 Forbidden', {'error': 'Нельзя изменить доступ администратора.'})

    locks = getattr(gate, '_mini_app_admin_locks', None)
    if locks is None:
        gate._mini_app_admin_locks = locks = {}
    lock = locks.setdefault(target, asyncio.Lock())
    async with lock:
        if action == 'unblock':
            if target not in gate.blocked:
                return json_response('409 Conflict', {'error': 'Пользователь не заблокирован.'})
            gate.blocked.discard(target)
            await gate._persist_user(target)
            msg = 'Пользователь разблокирован. Для входа нужен пароль.'
        else:
            if target not in gate.authorized or target in gate.blocked:
                return json_response('409 Conflict', {'error': 'У пользователя нет действующего доступа.'})
            await gate._revoke_user(target, block=action == 'block')
            msg = 'Доступ заблокирован.' if action == 'block' else 'Доступ отозван.'
        return json_response('200 OK', {'message': msg, 'user': user_state(gate, target)})
