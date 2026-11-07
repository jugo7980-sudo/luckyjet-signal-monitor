"""Owner-only interactive Playwright browser, reached via signed Telegram Mini App.
No public debugging port, no passwords in logs, no automated betting.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from urllib.parse import parse_qs

from playwright.async_api import Error as BrowserError, TimeoutError as BrowserTimeout

from mini_app_api import json_response, telegram_user
from site_login_browser import trusted_origin

log = logging.getLogger("luckyjet.remote_browser")
VIEWPORT_WIDTH = 430
VIEWPORT_HEIGHT = 932
IDLE_SECONDS = 120
MAX_SESSION_SECONDS = 540
MAX_POINTER_SECONDS = 8
POINTER_IDLE_SECONDS = 3


class RemoteSession:
    def __init__(self, account):
        self.account = account
        self.driver = None
        self.lock = asyncio.Lock()
        self.started = 0.0
        self.last_seen = 0.0
        self.watchdog = None
        self.pointer_active = False
        self.pointer_id = None
        self.pointer_started = 0.0
        self.pointer_last = 0.0

    def _active(self):
        now = time.monotonic()
        return bool(self.driver and now - self.started < MAX_SESSION_SECONDS
                    and now - self.last_seen < IDLE_SECONDS)

    async def _release_pointer(self):
        if self.pointer_active and self.driver:
            try:
                await self.driver.page.mouse.up()
            except BrowserError:
                pass
        self.pointer_active = False
        self.pointer_id = None

    async def _stop_unlocked(self):
        await self._release_pointer()
        driver, self.driver = self.driver, None
        if driver:
            try:
                await driver.__aexit__(None, None, None)
            except Exception:
                log.warning("Remote browser close failed (no private details)")
        task, self.watchdog = self.watchdog, None
        if task and task is not asyncio.current_task():
            task.cancel()

    async def close(self):
        async with self.lock:
            await self._stop_unlocked()

    async def _expire(self):
        try:
            while True:
                await asyncio.sleep(1)
                async with self.lock:
                    if (self.pointer_active
                            and time.monotonic() - self.pointer_last > POINTER_IDLE_SECONDS):
                        await self._release_pointer()
                    if not self._active():
                        await self._stop_unlocked()
                        return
        except asyncio.CancelledError:
            return

    async def start(self):
        async with self.lock:
            await self._stop_unlocked()
            driver = self.account.driver_factory()
            try:
                await driver.__aenter__()  # Verifies Japan SOCKS before browser opens.
                await driver.page.goto(driver.login_url, wait_until="domcontentloaded", timeout=45000)
                if not trusted_origin(driver.page.url, driver.login_url):
                    raise ValueError("Unexpected login origin")
            except BaseException:
                try:
                    await driver.__aexit__(None, None, None)
                except Exception:
                    pass
                raise
            self.driver = driver
            self.started = self.last_seen = time.monotonic()
            self.watchdog = asyncio.create_task(self._expire())

    async def status(self, *, capture=False):
        async with self.lock:
            if not self._active():
                await self._stop_unlocked()
                return {"active": False, "saved_session": self.account.store.load() is not None}
            self.last_seen = time.monotonic()
            result = {
                "active": True, "saved_session": self.account.store.load() is not None,
                "seconds_remaining": int(MAX_SESSION_SECONDS - (time.monotonic() - self.started)),
                "width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT,
                "dragging": self.pointer_active,
            }
            page = self.driver.page
        # Screenshot is intentionally OUTSIDE the input lock. A slow image
        # capture must never block finger events sent to Playwright.
        if capture:
            frame = await page.screenshot(
                type="jpeg", quality=43, scale="css", timeout=4000,
                mask=[page.locator('input, textarea, [contenteditable="true"]')],
                mask_color="#1b1823",
            )
            result["image"] = base64.b64encode(frame).decode("ascii")
        return result

    async def action(self, payload):
        async with self.lock:
            if not self._active():
                await self._stop_unlocked()
                return {"error": "Браузер закрыт или время вышло. Открой заново."}
            page = self.driver.page
            if not trusted_origin(page.url, self.driver.login_url):
                await self._release_pointer()
                return {"error": "Сайт перенаправил на другой домен. Вход приостановлен."}
            op = payload["action"]
            if self.pointer_active and op != "pointer":
                return {"error": "Сначала отпусти ползунок."}
            if op == "pointer" and set(payload) == {"action", "phase", "gesture", "x", "y"}:
                phase, gesture = payload["phase"], payload["gesture"]
                x, y = payload["x"], payload["y"]
                if (phase not in {"down", "move", "up"}
                        or type(gesture) is not int or not 1 <= gesture <= 2147483647
                        or any(type(v) not in (int, float) or not 0 <= v <= 1 for v in (x, y))):
                    return {"error": "Некорректное движение пальца."}
                mouse = page.mouse
                if phase == "down":
                    if self.pointer_active:
                        return {"error": "Ползунок уже удерживается."}
                    await mouse.move(float(x) * VIEWPORT_WIDTH, float(y) * VIEWPORT_HEIGHT)
                    await mouse.down()
                    self.pointer_active = True
                    self.pointer_id = gesture
                    self.pointer_started = self.pointer_last = time.monotonic()
                else:
                    if not self.pointer_active or gesture != self.pointer_id:
                        return {"error": "Перетаскивание не начато или завершилось."}
                    if time.monotonic() - self.pointer_started > MAX_POINTER_SECONDS:
                        await self._release_pointer()
                        return {"error": "Превышено время удержания ползунка."}
                    try:
                        await mouse.move(float(x) * VIEWPORT_WIDTH, float(y) * VIEWPORT_HEIGHT)
                    finally:
                        self.pointer_last = time.monotonic()
                        if phase == "up":
                            await self._release_pointer()
                self.last_seen = time.monotonic()
                return {"message": "Движение передано."}
            if op == "tap" and set(payload) == {"action", "x", "y"}:
                x, y = payload["x"], payload["y"]
                if any(type(v) not in (int, float) for v in (x, y)) or not (0 <= x <= 1 and 0 <= y <= 1):
                    return {"error": "Неверные координаты."}
                await page.mouse.click(float(x) * VIEWPORT_WIDTH, float(y) * VIEWPORT_HEIGHT)
            elif op == "drag" and set(payload) == {"action", "points", "duration_ms"}:
                points = payload["points"]
                duration = payload["duration_ms"]
                if (not isinstance(points, list) or not 2 <= len(points) <= 32
                        or type(duration) is not int or not 100 <= duration <= 4000):
                    return {"error": "Некорректный жест перетаскивания."}
                for point in points:
                    if (not isinstance(point, list) or len(point) != 2
                            or any(type(n) not in (int, float) or not 0 <= n <= 1
                                   for n in point)):
                        return {"error": "Некорректные координаты перетаскивания."}
                start, finish = points[0], points[-1]
                if (abs(finish[0] - start[0]) * VIEWPORT_WIDTH
                        + abs(finish[1] - start[1]) * VIEWPORT_HEIGHT < 12):
                    return {"error": "Перемести ползунок дальше."}
                # Relay the owner's gesture into Chromium. This does not
                # identify the CAPTCHA target or solve the puzzle automatically.
                mouse = page.mouse
                await mouse.move(start[0] * VIEWPORT_WIDTH,
                                 start[1] * VIEWPORT_HEIGHT)
                await mouse.down()
                try:
                    delay = min(0.12, duration / 1000 / (len(points) - 1))
                    for x, y in points[1:]:
                        await mouse.move(x * VIEWPORT_WIDTH, y * VIEWPORT_HEIGHT)
                        await asyncio.sleep(delay)
                finally:
                    await mouse.up()
                self.last_seen = time.monotonic()
                return {"message": "Жест выполнен. Проверь ответ CAPTCHA на экране."}
            elif op in {"fill_email", "fill_password"} and set(payload) == {"action", "text"}:
                value = payload["text"]
                if not isinstance(value, str) or not 1 <= len(value) <= 512:
                    return {"error": "Текст должен содержать 1–512 символов."}
                target = "email" if op == "fill_email" else "password"
                field = await self.driver._field(target)
                if field is None:
                    return {"error": "Нужное поле на странице не найдено. Проверь вкладку «Почта»."}
                # Fill the named field, not whichever input has keyboard focus.
                # This also replaces an accidentally inserted value or autofill.
                await field.fill(value, timeout=6000)
                await field.press("Tab", timeout=3000)
                value = None
                self.last_seen = time.monotonic()
                return {"message": "Почта введена в Email." if target == "email"
                        else "Пароль введён в Password."}
            elif op == "type" and set(payload) == {"action", "text"}:
                value = payload["text"]
                if not isinstance(value, str) or not 1 <= len(value) <= 512:
                    return {"error": "Текст должен содержать 1–512 символов."}
                # Generic input is reserved for one-time codes or other controls.
                await page.keyboard.insert_text(value)
                value = None
            elif op == "key" and set(payload) == {"action", "key"}:
                if payload["key"] not in {"Enter", "Tab", "Backspace", "Escape", "ArrowDown", "ArrowUp"}:
                    return {"error": "Неподдерживаемая клавиша."}
                await page.keyboard.press(payload["key"])
            elif op == "scroll" and set(payload) == {"action", "direction"}:
                if payload["direction"] not in {"up", "down"}:
                    return {"error": "Неверное направление."}
                await page.mouse.wheel(0, 540 if payload["direction"] == "down" else -540)
            elif op == "reload" and set(payload) == {"action"}:
                await page.reload(wait_until="domcontentloaded", timeout=30000)
            elif op == "save" and set(payload) == {"action"}:
                if not await self.driver._success():
                    return {"error": "Сайт ещё не подтвердил вход. Сохранение запрещено."}
                state = await self.driver.context.storage_state(indexed_db=True)
                self.account.store.save(state)
                self.account.generation += 1
                self.account.status = "connected"
                await self._stop_unlocked()
                return {"message": "Вход подтверждён. Сессия сохранена.", "saved": True}
            else:
                return {"error": "Недопустимое действие."}
            self.last_seen = time.monotonic()
            return {"message": "Выполнено."}


async def remote_browser(authorization, gate, token, method="GET", body=None):
    # Never accept a session cookie here: only a signed Telegram Mini App launch.
    if not authorization.startswith("tma "):
        return json_response("401 Unauthorized", {"error": "Открой приложение через Telegram."})
    try:
        init_data = authorization[4:]
        uid = telegram_user(init_data, token)
        q = parse_qs(init_data, keep_blank_values=True, max_num_fields=24)
        values = q.get("auth_date", [])
        age = time.time() - int(values[0]) if len(values) == 1 else float("inf")
        if not -30 <= age <= 600:
            return json_response("401 Unauthorized", {"error": "Переоткрой Mini App в Telegram."})
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        return json_response("401 Unauthorized", {"error": "Неверная авторизация Telegram."})

    account = getattr(gate, "site_account", None) if gate is not None else None
    if not account or not await gate.is_authorized(uid) or not gate.is_admin(uid) or account.owner_id != uid:
        return json_response("403 Forbidden", {"error": "Доступно только владельцу."})
    session = getattr(account, "_remote_browser_session", None)
    if session is None:
        session = RemoteSession(account)
        account._remote_browser_session = session

    if method == "GET":
        try:
            return json_response("200 OK", await session.status(capture=True))
        except (BrowserError, asyncio.TimeoutError):
            return json_response("503 Service Unavailable", {"error": "Не удалось получить изображение браузера."})
    if method != "POST":
        return json_response("405 Method Not Allowed", {"error": "Недопустимый метод."})
    try:
        data = json.loads(body or b"")
        if not isinstance(data, dict) or not isinstance(data.get("action"), str):
            raise ValueError("Invalid action")
    except (ValueError, TypeError, UnicodeDecodeError):
        return json_response("400 Bad Request", {"error": "Недопустимый запрос."})

    op = data["action"]
    if op == "start" and set(data) == {"action"}:
        if account.busy or account.field:
            return json_response("409 Conflict", {"error": "Заверши другой вход через Telegram."})
        current = getattr(account, "_mini_app_web_login", None)
        if current and current.running:
            return json_response("409 Conflict", {"error": "Заверши предыдущий вход через Mini App."})
        try:
            await asyncio.wait_for(session.start(), timeout=70)
            return json_response("200 OK", {"message": "Браузер открыт через японский прокси.", "active": True})
        except Exception as exc:
            # Do not expose site URLs, secrets, or browser exception messages.
            log.warning("Remote browser start failed: %s", type(exc).__name__)
            return json_response("503 Service Unavailable", {"error": "Браузер не открылся. Проверь японский прокси."})
    if op == "close" and set(data) == {"action"}:
        await session.close()
        return json_response("200 OK", {"message": "Удалённый браузер закрыт.", "active": False})
    try:
        result = await asyncio.wait_for(session.action(data), timeout=15)
    except (BrowserError, asyncio.TimeoutError):
        return json_response("503 Service Unavailable", {"error": "Действие не выполнено. Повтори попытку."})
    status = "400 Bad Request" if "error" in result else "200 OK"
    return json_response(status, result)
