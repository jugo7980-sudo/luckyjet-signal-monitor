"""Bounded login driver. Never performs betting or bypasses site challenges."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
import os
import re
import time

import httpx
import logging
from urllib.parse import urlsplit
from playwright.async_api import Error as BrowserError, TimeoutError as BrowserTimeout

from luckyjet_signal.browser_runtime import low_memory_async_playwright
from site_account import LoginProblem


DEFAULT_SELECTORS = {
    'email': 'input[type="email"], input[name="email"], input[autocomplete="email"], input[placeholder*="email" i]',
    'password': 'input[type="password"]',
    'code': 'input[autocomplete="one-time-code"], input[name="code"], input[name="otp"], input[placeholder*="код" i], input[placeholder*="code" i]',
    'error': '[role="alert"], [data-testid="login-error"]',
    'success': '[data-testid="logout"]',
}


def trusted_origin(current_url, login_url):
    current, expected = urlsplit(current_url), urlsplit(login_url)
    return (current.scheme == expected.scheme == 'https'
            and current.hostname == expected.hostname and current.port == expected.port
            and not current.username and not current.password)


def classify_login(*, success, code, error, challenge):
    if challenge:
        return 'challenge'
    if code:
        return 'code'
    if error:
        return 'error'
    return 'success' if success else 'waiting'



async def verify_proxy_country(proxy_url: str | None, expected_country: str) -> None:
    """Confirm the SOCKS exit country before any site credentials are transmitted."""
    if not proxy_url:
        raise LoginProblem('Японский прокси не подключён. Вход остановлен, прямое подключение запрещено.')

    # Both checks are made through the configured SOCKS tunnel, never directly.
    sources = (
        ('https://api.country.is/', 'country'),
        ('https://ipwho.is/', 'country_code'),
    )
    try:
        async with httpx.AsyncClient(proxy=proxy_url, timeout=10, trust_env=False) as client:
            for url, field in sources:
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                    payload = response.json()
                    country = str(payload.get(field, '')).upper() if isinstance(payload, dict) else ''
                    if len(country) != 2 or not country.isalpha():
                        continue
                except (httpx.HTTPError, ValueError, TypeError):
                    continue
                if country != expected_country.upper():
                    raise LoginProblem(
                        f'Выходной IP прокси не в Японии (страна: {country}). Вход остановлен.'
                    )
                return
    except httpx.HTTPError:
        pass
    raise LoginProblem('Не удалось подтвердить японский IP через прокси. Вход остановлен.')


class SiteLoginBrowser:
    def __init__(self, login_url, *, headless=True, selectors=None, result_timeout=30,
                 playwright_factory=low_memory_async_playwright, proxy_url=None,
                 expected_country=None):
        self.login_url = login_url
        self.headless = headless
        self.selectors = {key: os.getenv(f'SITE_AUTH_{key.upper()}_SELECTOR', value)
                          for key, value in DEFAULT_SELECTORS.items()}
        self.selectors.update(selectors or {})
        self.submit_selector = os.getenv('SITE_AUTH_SUBMIT_SELECTOR', '')
        self.code_submit_selector = os.getenv('SITE_AUTH_CODE_SUBMIT_SELECTOR', '')
        self.result_timeout = result_timeout
        self.playwright_factory = playwright_factory
        self.proxy_url = proxy_url
        self.expected_country = expected_country
        self.context = None
        self.page = None
        self._stack = None

    async def __aenter__(self):
        self._stack = AsyncExitStack()
        try:
            if self.expected_country:
                await verify_proxy_country(self.proxy_url, self.expected_country)
            p = await self._stack.enter_async_context(self.playwright_factory())
            options = {'headless': self.headless}
            if self.proxy_url:
                options['proxy'] = {'server': self.proxy_url}
            browser = await p.chromium.launch(**options)
            self._stack.push_async_callback(browser.close)
            self.context = await browser.new_context(viewport={'width': 430, 'height': 932}, locale='ru-RU')
            self.page = await self.context.new_page()
            self.page.set_default_timeout(5000)
            return self
        except BaseException:
            await self._stack.aclose()
            raise

    async def __aexit__(self, *args):
        await self._stack.aclose()

    def _check_origin(self):
        if not trusted_origin(self.page.url, self.login_url):
            raise LoginProblem('Сайт перенаправил на другой адрес. Вход остановлен; нужно проверить ссылку.')

    async def _visible(self, locator):
        for i in range(min(await locator.count(), 20)):
            item = locator.nth(i)
            if await item.is_visible():
                return item
        return None

    async def _field(self, name):
        return await self._visible(self.page.locator(self.selectors[name]))

    async def _text(self, pattern):
        return await self._visible(self.page.get_by_text(re.compile(pattern, re.I)))

    async def _success(self):
        return (await self._field('success') is not None or
                await self._text(r'^\s*(Выйти|Выход|Log\s*out|Sign\s*out)\s*$') is not None)

    async def _challenge(self):
        return await self._text(r'^(Verify you are human|Checking your browser|Проверка безопасности|Подтвердите, что вы человек)') is not None

    async def _submit(self, field, *, code=False):
        self._check_origin()
        selector = self.code_submit_selector if code else self.submit_selector
        button = await self._visible(self.page.locator(selector)) if selector else None
        if button is None:
            pattern = r'^\s*(Подтвердить|Продолжить|Confirm|Verify|Continue)\s*$' if code else r'^\s*(Войти|Вход|Log in|Login|Sign in)\s*$'
            scope = field
            # Search from the input outwards. Custom dialogs often use divs
            # instead of form; a page-wide search picks the covered header button.
            for _ in range(15):
                scope = scope.locator('xpath=..')
                if not await scope.count():
                    break
                button = await self._visible(scope.get_by_role('button', name=re.compile(pattern, re.I)))
                if button is not None:
                    break
        if button is not None:
            await self._click(button, 'подтверждения кода' if code else 'отправки формы входа')
        else:
            await field.press('Enter')

    async def _click(self, button, action):
        try:
            await button.click()
        except BrowserTimeout as exc:
            detail = str(exc).lower()
            opening = action == 'открытия входа'
            # A SPA can open the dialog and still leave Playwright waiting for a
            # navigation/actionability check. Do not mistake that for a failed click.
            if opening:
                try:
                    if await self._field('email') is not None or await self._field('password') is not None:
                        return
                except BrowserError:
                    pass
            reason = 'причина пока не определена'
            if 'intercepts pointer events' in detail:
                reason = 'кнопку перекрывает другой элемент'
            elif 'not enabled' in detail or 'element is disabled' in detail:
                reason = 'кнопка отключена'
            elif 'not stable' in detail:
                reason = 'кнопка движется или страница перерисовывается'
            elif 'not visible' in detail:
                reason = 'кнопка скрыта'
            elif 'detached' in detail or 'not attached' in detail:
                reason = 'элемент удалён при обновлении страницы'

            # An ordinary pointer click can time out on animated SPA headers.
            # Only for OPENING the login form (never submitting secrets), try a
            # DOM click if hit testing proves the button is genuinely on top.
            # Do not click through overlays, challenges or disabled controls.
            if opening and reason == 'причина пока не определена':
                try:
                    hit = await button.evaluate("""el => {
                        const rect = el.getBoundingClientRect();
                        const x = rect.x + rect.width / 2;
                        const y = rect.y + rect.height / 2;
                        const top = document.elementFromPoint(x, y);
                        return {
                            visible: rect.width > 0 && rect.height > 0 &&
                                     x >= 0 && y >= 0 &&
                                     x < innerWidth && y < innerHeight,
                            uncovered: !!top && (top === el || el.contains(top)),
                            enabled: !el.matches(':disabled, [aria-disabled="true"]') &&
                                     getComputedStyle(el).pointerEvents !== 'none'
                        };
                    }""", timeout=1500)
                    if hit and hit.get('visible') and hit.get('uncovered') and hit.get('enabled'):
                        self._check_origin()
                        if not await self._challenge():
                            await button.dispatch_event('click', timeout=1500)
                            logging.getLogger('luckyjet.site_login_browser').info(
                                'Login opener recovered with verified DOM click'
                            )
                            return
                    elif hit and hit.get('visible') and not hit.get('uncovered'):
                        reason = 'кнопку перекрывает другой элемент'
                    elif hit and not hit.get('enabled'):
                        reason = 'кнопка отключена'
                    else:
                        reason = 'кнопка вне доступной области'
                except BrowserError:
                    reason = 'элемент изменился во время нажатия'

            problem = LoginProblem(
                f'Сайт не дал нажать кнопку {action}. Диагностика: {reason}. '
                'Снимок страницы будет отправлен в Telegram, если доступен.'
            )
            if opening:
                try:
                    problem.page_screenshot = await self.page.screenshot(type='png')
                except BrowserError:
                    problem.page_screenshot = None
            raise problem from None

    async def _login_opener(self):
        """Choose an on-screen login control over off-screen duplicates."""
        viewport = self.page.viewport_size or {'width': 430, 'height': 932}
        fallback = None
        choices = (
            r'^\s*Вход\s*$',
            r'^\s*(?:Log in|Login|Sign in)\s*$',
            r'^\s*Войти\s*$',
        )
        for role in ('button', 'link'):
            for name in choices:
                candidates = self.page.get_by_role(role, name=re.compile(name, re.I))
                for i in range(min(await candidates.count(), 20)):
                    item = candidates.nth(i)
                    if not await item.is_visible():
                        continue
                    bounds = await item.bounding_box()
                    if bounds is None or bounds['width'] <= 0 or bounds['height'] <= 0:
                        continue
                    cx = bounds['x'] + bounds['width'] / 2
                    cy = bounds['y'] + bounds['height'] / 2
                    if 0 <= cx < viewport['width'] and 0 <= cy < viewport['height']:
                        return item
                    if fallback is None:
                        fallback = item

        # Scroll normally for a below-fold opener, never click through overlays.
        if fallback is not None:
            try:
                await fallback.scroll_into_view_if_needed(timeout=1500)
                bounds = await fallback.bounding_box()
                if bounds and bounds['width'] > 0 and bounds['height'] > 0:
                    cx = bounds['x'] + bounds['width'] / 2
                    cy = bounds['y'] + bounds['height'] / 2
                    if 0 <= cx < viewport['width'] and 0 <= cy < viewport['height']:
                        return fallback
            except BrowserError:
                pass
        return None

    async def _open_form(self):
        end = time.monotonic() + min(15, self.result_timeout)
        opened = False
        email_tab = False
        while time.monotonic() < end:
            self._check_origin()
            if await self._challenge():
                raise LoginProblem('Сайт требует проверку браузера/CAPTCHA. Автоматический вход остановлен.')
            email = await self._field('email')
            password = await self._field('password')
            if email is not None and password is not None:
                return email, password
            if not email_tab and (password is not None or opened):
                email_choice = re.compile(
                    r'^\s*(?:(?:Вход|Login)\s*[-—:]\s*)?'
                    r'(?:Авторизация\s+по\s+почте|Вход\s+по\s+почте|'
                    r'Войти\s+по\s+почте|Почта|E-?mail)\s*
                    await self._click(tab, 'выбора почты')
                    opened = email_tab = True
                    continue
            if not opened:
                button = await self._login_opener()
                if button is not None:
                    await self._click(button, 'открытия входа')
                    opened = True
            await asyncio.sleep(.2)
        raise LoginProblem('Не найдена форма входа по почте. Нужно проверить ссылку и поля сайта.')

    async def _wait_result(self, *, after_code=False):
        end = time.monotonic() + self.result_timeout
        while time.monotonic() < end:
            self._check_origin()
            code = await self._field('code') is not None
            error = await self._field('error') is not None
            result = classify_login(success=await self._success(), code=code,
                                    error=error, challenge=await self._challenge())
            if result == 'challenge':
                raise LoginProblem('Сайт требует проверку браузера/CAPTCHA. Автоматический вход остановлен.')
            if result == 'success':
                return result
            if result == 'error' and not code:
                raise LoginProblem('Сайт отклонил вход. Проверь почту и пароль и начни заново.')
            if code and not after_code:
                return 'code'
            await asyncio.sleep(.2)
        # A prior OTP error can remain visible during a valid retry's network
        # request. Give that submission the whole result window before retrying.
        if after_code and await self._field('code') is not None and await self._field('error') is not None:
            return 'code'
        email_visible = await self._field('email') is not None
        password_visible = await self._field('password') is not None
        code_visible = await self._field('code') is not None
        error_visible = await self._field('error') is not None
        logging.getLogger('luckyjet.site_login_browser').warning(
            'Site login result timeout: login_form=%s code=%s error=%s after_code=%s',
            email_visible and password_visible, code_visible, error_visible, after_code,
        )
        if email_visible and password_visible:
            raise LoginProblem(
                'Форма входа осталась открыта. Сайт не подтвердил авторизацию '
                'после отправки данных. Проверь сообщение под формой.'
            )
        raise LoginProblem(
            'Форма входа изменилась, но вход не подтверждён сайтом. '
            'Нужна проверка интерфейса сайта.'
        )

    async def _fill_code(self, code):
        self._check_origin()
        fields = self.page.locator(self.selectors['code'])
        visible = [fields.nth(i) for i in range(min(await fields.count(), 10))
                   if await fields.nth(i).is_visible()]
        if not visible:
            raise LoginProblem('Поле кода больше не доступно. Начни вход заново.')
        if len(visible) == len(code):
            for field, digit in zip(visible, code):
                await field.fill(digit)
        else:
            await visible[0].fill(code)
        await self._submit(visible[-1], code=True)

    async def login(self, email, password, request_code):
        await self.page.goto(self.login_url, wait_until='domcontentloaded', timeout=60000)
        self._check_origin()
        email_field, password_field = await self._open_form()
        self._check_origin()
        await email_field.fill(email)
        self._check_origin()
        await password_field.fill(password)
        password = None
        await self._submit(password_field)
        result = await self._wait_result()
        for attempt in range(3):
            if result == 'success':
                return await self.context.storage_state(indexed_db=True)
            code = await request_code(attempt > 0)
            await self._fill_code(code)
            code = None
            result = await self._wait_result(after_code=True)
        if result == 'success':
            return await self.context.storage_state(indexed_db=True)
        raise LoginProblem('Код не принят после трёх попыток. Начни вход заново.')

, re.I
                )
                tab = await self._visible(self.page.get_by_role('button', name=email_choice))
                if tab is None:
                    tab = await self._visible(self.page.get_by_role('link', name=email_choice))
                if tab is not None:
                    await self._click(tab, 'выбора почты')
                    opened = email_tab = True
                    continue
            if not opened:
                button = await self._login_opener()
                if button is not None:
                    await self._click(button, 'открытия входа')
                    opened = True
            await asyncio.sleep(.2)
        raise LoginProblem('Не найдена форма входа по почте. Нужно проверить ссылку и поля сайта.')

    async def _wait_result(self, *, after_code=False):
        end = time.monotonic() + self.result_timeout
        while time.monotonic() < end:
            self._check_origin()
            code = await self._field('code') is not None
            error = await self._field('error') is not None
            result = classify_login(success=await self._success(), code=code,
                                    error=error, challenge=await self._challenge())
            if result == 'challenge':
                raise LoginProblem('Сайт требует проверку браузера/CAPTCHA. Автоматический вход остановлен.')
            if result == 'success':
                return result
            if result == 'error' and not code:
                raise LoginProblem('Сайт отклонил вход. Проверь почту и пароль и начни заново.')
            if code and not after_code:
                return 'code'
            await asyncio.sleep(.2)
        # A prior OTP error can remain visible during a valid retry's network
        # request. Give that submission the whole result window before retrying.
        if after_code and await self._field('code') is not None and await self._field('error') is not None:
            return 'code'
        email_visible = await self._field('email') is not None
        password_visible = await self._field('password') is not None
        code_visible = await self._field('code') is not None
        error_visible = await self._field('error') is not None
        logging.getLogger('luckyjet.site_login_browser').warning(
            'Site login result timeout: login_form=%s code=%s error=%s after_code=%s',
            email_visible and password_visible, code_visible, error_visible, after_code,
        )
        if email_visible and password_visible:
            raise LoginProblem(
                'Форма входа осталась открыта. Сайт не подтвердил авторизацию '
                'после отправки данных. Проверь сообщение под формой.'
            )
        raise LoginProblem(
            'Форма входа изменилась, но вход не подтверждён сайтом. '
            'Нужна проверка интерфейса сайта.'
        )

    async def _fill_code(self, code):
        self._check_origin()
        fields = self.page.locator(self.selectors['code'])
        visible = [fields.nth(i) for i in range(min(await fields.count(), 10))
                   if await fields.nth(i).is_visible()]
        if not visible:
            raise LoginProblem('Поле кода больше не доступно. Начни вход заново.')
        if len(visible) == len(code):
            for field, digit in zip(visible, code):
                await field.fill(digit)
        else:
            await visible[0].fill(code)
        await self._submit(visible[-1], code=True)

    async def login(self, email, password, request_code):
        await self.page.goto(self.login_url, wait_until='domcontentloaded', timeout=60000)
        self._check_origin()
        email_field, password_field = await self._open_form()
        self._check_origin()
        await email_field.fill(email)
        self._check_origin()
        await password_field.fill(password)
        password = None
        await self._submit(password_field)
        result = await self._wait_result()
        for attempt in range(3):
            if result == 'success':
                return await self.context.storage_state(indexed_db=True)
            code = await request_code(attempt > 0)
            await self._fill_code(code)
            code = None
            result = await self._wait_result(after_code=True)
        if result == 'success':
            return await self.context.storage_state(indexed_db=True)
        raise LoginProblem('Код не принят после трёх попыток. Начни вход заново.')

