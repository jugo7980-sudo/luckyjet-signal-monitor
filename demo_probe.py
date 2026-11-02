"""Bounded, read-only demo page diagnostic; never places bets or signs in."""
import asyncio
import logging
import time
from datetime import datetime, timezone

from luckyjet_signal.browser_runtime import low_memory_async_playwright

log = logging.getLogger('luckyjet.demo_probe')
DEMO_URL = 'https://1whnva.com/casino/play/v_1wGames:LuckyJet'


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

    def start(self):
        if self.task and not self.task.done():
            return self.task
        if time.monotonic() - self.started_at < 60:
            return self.task
        self.started_at = time.monotonic()
        self.task = asyncio.create_task(self._run())
        return self.task

    def text(self):
        messages = {
            'not_checked': 'Проверка ещё не выполнена.',
            'region_blocked': 'Сайт блокирует доступ из региона сервера.',
            'verification': 'Сайт требует проверку браузера. Проверка остановлена.',
            'demo_available': 'На странице найдена кнопка деморежима. Запуск игры и виртуальный баланс ещё не подтверждены.',
            'unconfirmed': 'Страница открыта, но деморежим не подтверждён.',
            'timeout': 'Страница не загрузилась за отведённое время.',
            'error': 'Не удалось открыть страницу с сервера.',
        }
        status = 'Проверка выполняется…' if self.task and not self.task.done() else messages[self.result]
        stamp = f'\nПроверено: {self.checked_at} UTC' if self.checked_at else ''
        return f'🧪 Проверка деморежима\nИсточник: 1whnva.com\n{status}{stamp}\nСтавки не выполняются.'

    async def _inspect(self):
        async with low_memory_async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page(locale='ru-RU')
                await page.goto(DEMO_URL, wait_until='domcontentloaded', timeout=25000)
                for _ in range(6):
                    texts = []
                    for frame in page.frames:
                        try:
                            texts.append(await frame.locator('body').inner_text(timeout=1500))
                        except Exception:
                            pass
                    result = classify_page('\n'.join(texts))
                    if result != 'unconfirmed':
                        return result
                    await asyncio.sleep(2)
                return 'unconfirmed'
            finally:
                await browser.close()

    async def _run(self):
        try:
            self.result = await asyncio.wait_for(self._inspect(), timeout=45)
        except asyncio.TimeoutError:
            self.result = 'timeout'
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.result = 'error'
            log.warning('Demo probe failed: %s', type(exc).__name__)
        self.checked_at = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        log.info('Demo probe completed: status=%s', self.result)

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
