"""Bounded, read-only demo page diagnostic; never places bets or signs in."""
import asyncio
import logging
import time
from datetime import datetime, timezone

from urllib.parse import urlsplit

log = logging.getLogger('luckyjet.demo_probe')


def classify_page(text):
    text = text.casefold()
    if any(s in text for s in ('not available in your region', 'недоступен в вашем регионе')):
        return 'region_blocked'
    if any(s in text for s in ('verify you are human', 'checking your browser', 'captcha', 'проверка безопасности')):
        return 'verification'
    if any(s in text for s in ('play demo', 'demo mode', 'демо-режим', 'демо режим', 'играть бесплатно')):
        return 'demo_available'
    return 'unconfirmed'


class DemoProbe:
    def __init__(self):
        self.task = None
        self.result = 'not_checked'
        self.checked_at = None
        self.started_at = -float('inf')
        self.requested = True
        self.ready = asyncio.Event()
        self.source = 'страница мониторинга'
        self.game_loaded = False

    def start(self):
        if self.task and not self.task.done():
            return self.task
        if time.monotonic() - self.started_at < 60:
            return self.task
        self.started_at = time.monotonic()
        self.ready.clear()
        self.requested = True
        self.task = asyncio.create_task(self.wait_result())
        return self.task

    async def wait_result(self):
        try:
            await asyncio.wait_for(self.ready.wait(), timeout=180)
        except asyncio.TimeoutError:
            self.result = 'timeout'
            self.requested = False
            self.checked_at = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            log.info('Demo probe completed: status=timeout')

    def text(self):
        messages = {
            'not_checked': 'Проверка ещё не выполнена.',
            'region_blocked': 'Сайт блокирует доступ из региона сервера.',
            'verification': 'Сайт требует проверку браузера. Проверка остановлена.',
            'demo_available': 'Найдена кнопка деморежима. Виртуальный баланс не подтверждён.',
            'game_loaded': 'Интерфейс игры загружен. Виртуальный баланс не подтверждён.',
            'unconfirmed': 'Деморежим не подтверждён.',
            'timeout': 'Не удалось дождаться страницы мониторинга за 3 минуты.',
            'error': 'Не удалось прочитать страницу игры.',
        }
        status = 'Ожидаю проверку страницы мониторинга…' if self.requested else messages[self.result]
        stamp = f'\nПроверено: {self.checked_at} UTC' if self.checked_at else ''
        return f'🧪 Проверка деморежима\nИсточник: {self.source}\n{status}{stamp}\nСтавки не выполняются.'

    async def observe(self, page):
        """Read the already loaded monitoring page; no new browser or navigation."""
        if not self.requested:
            return
        try:
            await asyncio.wait_for(self._read_page(page), timeout=10)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.result = 'error'
            log.warning('Demo page observation failed: %s', type(exc).__name__)
        self.checked_at = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        self.requested = False
        self.ready.set()
        log.info('Demo probe completed: status=%s source=%s game_loaded=%s',
                 self.result, self.source, self.game_loaded)

    async def _read_page(self, page):
        self.source = urlsplit(str(page.url)).hostname or 'страница мониторинга'
        texts = []
        self.game_loaded = False
        for frame in page.frames:
            try:
                text = await frame.locator('body').inner_text(timeout=1500)
            except Exception:
                continue
            texts.append(text)
            # A frame URL or completed-round coefficients alone do not prove a game UI.
            if '/lucky2/' in str(frame.url).lower() and any(
                label in text.casefold() for label in ('ставка', 'auto bet', 'cash out')
            ):
                self.game_loaded = True
        self.result = classify_page('\n'.join(texts))
        if self.result == 'unconfirmed' and self.game_loaded:
            self.result = 'game_loaded'

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
