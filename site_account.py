"""Owner-only, short-lived Telegram dialogue for a browser login.

Only browser storage is persisted. Email/password/OTP never enter Redis or logs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import traceback
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlsplit


class LoginProblem(Exception):
    """A fixed, non-secret message suitable for displaying to the owner."""


class SessionStore:
    def __init__(self, path, login_url):
        self.path = Path(path)
        parsed = urlsplit(login_url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Site login URL must be HTTPS without credentials')
        self.origin = f'https://{parsed.netloc.lower()}'

    def load(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            state = data['state']
            if data['origin'] != self.origin or not isinstance(state, dict):
                return None
            if not isinstance(state.get('cookies'), list) or not isinstance(state.get('origins'), list):
                return None
            return state
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def save(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix='.session-')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump({'origin': self.origin, 'state': state}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(name, 0o600)
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def clear(self):
        self.path.unlink(missing_ok=True)


class SiteAccount:
    def __init__(self, owner_id, request, store, driver_factory, browser_lock,
                 *, input_timeout=180, attempt_timeout=600):
        self.owner_id = int(owner_id) if owner_id else None
        self.request = request
        self.store = store
        self.driver_factory = driver_factory
        self.browser_lock = browser_lock
        self.input_timeout = input_timeout
        self.attempt_timeout = attempt_timeout
        self.field = None
        self.email = None
        self.deadline = 0
        self.generation = 0
        self.status = 'saved' if store.load() else 'disconnected'
        self._task = None
        self._timer = None
        self._code = None
        self._control = asyncio.Lock()

    @property
    def busy(self):
        return self._task is not None and not self._task.done()

    def storage_state(self):
        return self.store.load()

    def _owner(self, sender, chat):
        return (self.owner_id is not None and sender.get('id') == self.owner_id
                and not sender.get('is_bot') and chat.get('type') == 'private'
                and chat.get('id') == self.owner_id)

    def menu(self):
        return {'inline_keyboard': [
            [{'text': '🔐 Войти / войти заново', 'callback_data': 'site_login'}],
            [{'text': '🔄 Статус', 'callback_data': 'site_status'}],
            [{'text': '✖ Отменить ввод', 'callback_data': 'site_cancel'}],
            [{'text': '🚪 Забыть вход на сервере', 'callback_data': 'site_forget'}],
            [{'text': '🏠 Главное меню', 'callback_data': 'menu_main'}],
        ]}

    async def _say(self, text):
        if self.owner_id is not None:
            await self.request('sendMessage', {
                'chat_id': self.owner_id, 'text': text, 'protect_content': True,
                'reply_markup': self.menu(),
            })

    async def show_status(self):
        labels = {
            'saved': 'Сессия сохранена. Будет использована браузером; срок действия проверяется сайтом.',
            'connected': '✅ Вход на сайте подтверждён. Сессия сохранена.',
            'disconnected': 'Аккаунт не подключён.',
            'connecting': '⏳ Выполняется вход на сайт.',
            'code': '📩 Ожидаю код из почты.',
            'error': '❌ Последний вход не выполнен. Можно повторить попытку.',
        }
        pending = {'email': '\nОжидаю почту.', 'password': '\nОжидаю пароль.'}.get(self.field, '')
        await self._say('👤 Аккаунт сайта\n' + labels[self.status] + pending)

    async def _delete(self, msg):
        try:
            await self.request('deleteMessage', {
                'chat_id': self.owner_id, 'message_id': msg['message_id'],
            })
        except Exception:
            # Never include a Telegram update or exception payload in diagnostics.
            pass

    def _clear_input(self):
        self.field = None
        self.email = None
        self.deadline = 0
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    async def _prompt(self, field, text):
        if self._timer is not None:
            self._timer.cancel()
        self.field = field
        self.deadline = time.monotonic() + self.input_timeout
        if field != 'code':
            self._timer = asyncio.create_task(self._expire_input(self.deadline))
        await self._say(text)

    async def _expire_input(self, deadline):
        try:
            await asyncio.sleep(self.input_timeout)
            if self.deadline == deadline and self.field in {'email', 'password'}:
                self.field = None
                self.email = None
                self.deadline = 0
                await self._say('Время ввода истекло. Нажми «Войти» для новой попытки.')
        except asyncio.CancelledError:
            pass

    async def start_login(self):
        await self.cancel(notify=False)
        await self._prompt('email', '🔐 Вход на сайт\nПришли почту аккаунта.\n'
                           'Почта, пароль и код будут удаляться из чата по возможности. '
                           'Telegram-боты не используют сквозное шифрование. '
                           'Пароль и код не сохраняются на сервере.')

    async def cancel(self, *, notify=True):
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._task = None
        if self._code is not None and not self._code.done():
            self._code.cancel()
        self._code = None
        self._clear_input()
        self.status = 'saved' if self.store.load() else 'disconnected'
        if notify:
            await self._say('Ввод отменён. Пароль и код не сохранены.')

    async def forget(self):
        await self.cancel(notify=False)
        self.store.clear()
        self.generation += 1
        self.status = 'disconnected'
        await self._say('Сохранённый вход удалён с сервера. Другие сеансы на сайте не отозваны.')

    async def handle_callback(self, callback):
        action = callback.get('data', '')
        if action not in {'site_login', 'site_status', 'site_cancel', 'site_forget', 'site_forget_confirm'}:
            return False
        allowed = self._owner(callback.get('from') or {},
                              (callback.get('message') or {}).get('chat') or {})
        try:
            await self.request('answerCallbackQuery', {
                'callback_query_id': callback['id'],
                'text': '' if allowed else 'Доступно только владельцу в личном чате.',
                'show_alert': not allowed,
            })
        except Exception:
            pass
        if not allowed:
            return True
        async with self._control:
            if action == 'site_login':
                await self.start_login()
            elif action == 'site_cancel':
                await self.cancel()
            elif action == 'site_forget':
                await self.request('sendMessage', {'chat_id': self.owner_id,
                    'text': 'Удалить сохранённую сессию с сервера?',
                    'reply_markup': {'inline_keyboard': [[
                        {'text': 'Да, удалить', 'callback_data': 'site_forget_confirm'},
                        {'text': 'Отмена', 'callback_data': 'site_status'},
                    ]]}})
            elif action == 'site_forget_confirm':
                await self.forget()
            else:
                await self.show_status()
        return True

    async def handle_message(self, msg):
        if not self._owner(msg.get('from') or {}, msg.get('chat') or {}):
            return False
        value = msg.get('text')
        if not isinstance(value, str):
            return False
        command = value.strip().split(' ', 1)[0].split('@', 1)[0].lower()
        async with self._control:
            if command in {'/site', '/account'}:
                await self._delete(msg)
                await self.show_status()
                return True
            if command == '/cancel' and (self.field or self.busy):
                await self._delete(msg)
                await self.cancel()
                return True
            if self.field is None:
                return False
            if command in {'/start', '/menu', '/settings', '/status', '/help'}:
                await self.cancel(notify=False)
                return False
            await self._delete(msg)
            if time.monotonic() >= self.deadline:
                await self.cancel(notify=False)
                await self._say('Время ввода истекло. Нажми «Войти» снова.')
                return True
            if self.field == 'email':
                email = value.strip()
                if len(email) > 254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
                    await self._say('Почта не распознана. Пришли адрес вида name@example.com.')
                    return True
                self.email = email
                await self._prompt('password', 'Пришли пароль сайта одним сообщением. /cancel — отмена.')
            elif self.field == 'password':
                if not value or len(value) > 512:
                    await self._say('Пароль должен содержать от 1 до 512 символов.')
                    return True
                email = self.email
                self._clear_input()
                self.status = 'connecting'
                self._task = asyncio.create_task(self._login(email, value))
            elif self.field == 'code':
                code = value.strip().replace(' ', '').replace('-', '')
                if not re.fullmatch(r'[0-9]{4,10}', code):
                    await self._say('Пришли только код из письма: от 4 до 10 цифр.')
                    return True
                if self._code is not None and not self._code.done():
                    self.field = None
                    self._code.set_result(code)
            return True

    async def _request_code(self, retry=False):
        self._code = asyncio.get_running_loop().create_future()
        self.status = 'code'
        await self._prompt('code', ('Код не принят. ' if retry else '') +
                           '📩 Сайт запросил код из почты. Пришли его сюда в течение 3 минут.')
        try:
            code = await asyncio.wait_for(self._code, self.input_timeout)
            self.status = 'connecting'
            return code
        finally:
            self._code = None
            self.field = None

    async def _login(self, email, password):
        stage = 'notify'
        try:
            await self._say('⏳ Открываю сайт и выполняю вход…')
            async with asyncio.timeout(self.attempt_timeout):
                stage = 'browser_lock'
                async with self.browser_lock:
                    stage = 'browser_start'
                    async with self.driver_factory() as driver:
                        stage = 'login'
                        state = await driver.login(email, password, self._request_code)
                        password = None
                        stage = 'session_save'
                        self.store.save(state)
                        self.generation += 1
                        self.status = 'connected'
            await self._say('✅ Вход на сайте подтверждён. Сессия сохранена для бота.')
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self.status = 'error'
            await self._say('Время входа или ожидания кода истекло. Нажми «Войти» снова.')
        except LoginProblem as exc:
            self.status = 'error'
            await self._say(str(exc))
        except Exception as exc:
            self.status = 'error'
            kind = type(exc).__name__
            frames = traceback.extract_tb(exc.__traceback__)
            location = ' > '.join(f'{frame.name}:{frame.lineno}' for frame in frames[-5:])
            logging.getLogger('luckyjet.site_account').error(
                'Site login failed: type=%s stage=%s location=%s', kind, stage, location,
            )
            await self._say(f'Вход не выполнен. Ошибка {kind}, этап {stage}. '
                            'Передай это сообщение для проверки. Пароль повторно пока не отправляй.')
        finally:
            password = None
            email = None
            self._clear_input()

    async def close(self):
        await self.cancel(notify=False)
