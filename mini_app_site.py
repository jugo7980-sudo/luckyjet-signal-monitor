"""Owner-only, transient web login for the existing site browser integration.

Secrets are accepted in small authenticated HTTPS POST bodies and are never persisted
to Redis, logs, responses, localStorage or sessionStorage. Only the validated browser
session returned by the existing driver is saved using SiteAccount.store.
"""
import asyncio
import json
import logging
import re
import time
from urllib.parse import parse_qs

from mini_app_api import json_response, telegram_user
from site_account import LoginProblem

log = logging.getLogger('luckyjet.mini_app_site')


class WebLoginFlow:
    def __init__(self, account):
        self.account = account
        self.task = None
        self.code = None
        self.phase = 'idle'
        self.error = None

    @property
    def running(self):
        return self.task is not None and not self.task.done()

    async def cancel(self):
        code = self.code
        self.code = None
        if code is not None and not code.done():
            code.cancel()
        task = self.task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.task = None
        self.phase = 'idle'
        self.error = None

    async def request_code(self, retry=False):
        self.code = asyncio.get_running_loop().create_future()
        self.phase = 'retry_code' if retry else 'code'
        try:
            code = await asyncio.wait_for(self.code, timeout=self.account.input_timeout)
            self.phase = 'connecting'
            return code
        finally:
            self.code = None

    async def login(self, email, password):
        self.phase = 'connecting'
        self.error = None
        try:
            async with asyncio.timeout(self.account.attempt_timeout):
                async with self.account.browser_lock:
                    async with self.account.driver_factory() as driver:
                        session_state = await driver.login(email, password, self.request_code)
                        password = None
                    self.account.store.save(session_state)
                    self.account.generation += 1
                    self.account.status = 'connected'
                    self.phase = 'connected'
        except asyncio.CancelledError:
            self.phase = 'idle'
            raise
        except TimeoutError:
            self.phase = 'error'
            self.error = 'Время входа истекло. Повтори попытку.'
        except LoginProblem as exc:
            # Only fixed, internally authored diagnostic messages may reach the UI.
            # Never include raw site text, URLs, page HTML or credentials.
            log.warning('Mini App site sign-in failed: LoginProblem')
            self.phase = 'error'
            self.error = str(exc)
            screenshot = getattr(exc, 'page_screenshot', None)
            if screenshot:
                try:
                    await self.account.request('sendPhoto', {
                        'chat_id': self.account.owner_id,
                        'photo_file': ('login-page.png', screenshot, 'image/png'),
                        'caption': 'Состояние страницы до ввода логина и пароля',
                    })
                except Exception:
                    log.warning('Could not deliver login screenshot to owner')
        except Exception as exc:
            # Do not format exception messages: they can include site/credential data.
            log.warning('Mini App site sign-in failed: %s', type(exc).__name__)
            self.phase = 'error'
            self.error = 'Вход не выполнен. Проверь данные или доступность сайта.'
        finally:
            email = password = None


def view(account, flow):
    saved = account.store.load() is not None
    if flow.running or flow.phase in {'connected', 'error'}:
        phase = flow.phase
    else:
        phase = 'saved' if saved else 'disconnected'
    return {
        'phase': phase,
        'saved_session': saved,
        'error': flow.error if phase == 'error' else None,
    }


async def site(authorization, gate, token, method='GET', body=None):
    if not authorization.startswith('tma '):
        return json_response('401 Unauthorized', {'error': 'Открой приложение через Telegram.'})
    try:
        uid = telegram_user(authorization[4:], token)
    except (ValueError, KeyError, TypeError, AttributeError):
        return json_response('401 Unauthorized', {'error': 'Сеанс истёк. Переоткрой приложение.'})
    account = getattr(gate, 'site_account', None) if gate is not None else None
    if gate is None or account is None:
        return json_response('503 Service Unavailable', {'error': 'Авторизация сайта не подключена.'})
    if (not await gate.is_authorized(uid) or not gate.is_admin(uid)
            or account.owner_id != uid):
        return json_response('403 Forbidden', {'error': 'Доступно только владельцу бота.'})
    flow = getattr(account, '_mini_app_web_login', None)
    if flow is None:
        account._mini_app_web_login = flow = WebLoginFlow(account)

    if method == 'GET':
        return json_response('200 OK', view(account, flow))
    if method != 'POST':
        return json_response('405 Method Not Allowed', {'error': 'Метод не поддерживается.'})

    # Credentials and OTP are accepted only from recently opened Telegram WebApps.
    try:
        query = parse_qs(authorization[4:], keep_blank_values=True, max_num_fields=24)
        auth_times = query.get('auth_date', [])
        if len(auth_times) != 1 or time.time() - int(auth_times[0]) > 600:
            raise ValueError('Old session')
        data = json.loads(body or b'')
        if not isinstance(data, dict):
            raise ValueError('Invalid body')
        action = data.get('action')
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        return json_response('400 Bad Request', {'error': 'Недопустимый запрос. Переоткрой приложение.'})

    async with account._control:
        if action == 'login':
            if set(data) != {'action', 'email', 'password'}:
                return json_response('400 Bad Request', {'error': 'Неполные данные входа.'})
            email, password = data.get('email'), data.get('password')
            if (not isinstance(email, str) or len(email) > 254
                    or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email)
                    or not isinstance(password, str) or not 1 <= len(password) <= 512):
                return json_response('400 Bad Request', {'error': 'Проверь адрес почты и пароль.'})
            if account.busy or account.field:
                return json_response('409 Conflict', {'error': 'Заверши ввод через Telegram-бота.'})
            if flow.running:
                return json_response('409 Conflict', {'error': 'Вход уже выполняется.'})
            await flow.cancel()
            flow.phase = 'connecting'
            flow.task = asyncio.create_task(flow.login(email, password))
            email = password = None
            message = 'Вход запущен.'
        elif action == 'code':
            if set(data) != {'action', 'code'}:
                return json_response('400 Bad Request', {'error': 'Укажи только код.'})
            code = data.get('code')
            if not isinstance(code, str) or not re.fullmatch(r'[0-9]{4,10}', code):
                return json_response('400 Bad Request', {'error': 'Код: от 4 до 10 цифр.'})
            if flow.phase not in {'code', 'retry_code'} or flow.code is None or flow.code.done():
                return json_response('409 Conflict', {'error': 'Сайт ещё не запросил код.'})
            flow.phase = 'connecting'
            flow.code.set_result(code)
            code = None
            message = 'Код передан браузеру.'
        elif action == 'cancel':
            if set(data) != {'action'}:
                return json_response('400 Bad Request', {'error': 'Некорректная команда.'})
            await flow.cancel()
            message = 'Вход отменён.'
        elif action == 'forget':
            if set(data) != {'action', 'confirm'} or data.get('confirm') is not True:
                return json_response('400 Bad Request', {'error': 'Подтверди удаление сессии.'})
            await flow.cancel()
            await account.cancel(notify=False)
            account.store.clear()
            account.generation += 1
            account.status = 'disconnected'
            message = 'Сохранённый вход удалён с сервера. Другие сеансы сайта не отозваны.'
        else:
            return json_response('400 Bad Request', {'error': 'Неизвестная команда.'})
    return json_response('200 OK', {'message': message, **view(account, flow)})
