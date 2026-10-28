from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx

from luckyjet_password_gate import TelegramPasswordGate
from luckyjet_signal.settings import Config
from luckyjet_signal import monitor
from luckyjet_signal.browser_runtime import low_memory_async_playwright
from luckyjet_signal.demo_mode import wrap_click_demo_mode


log = logging.getLogger("luckyjet.telegram_ui")
_COEFFICIENT_PREFIX = "Последний коэффициент: "
_PANEL_TITLE = "🎰 Lucky Jet — история"
_HISTORY_LIMIT = 20
_HISTORY_COLUMNS = 5
_DEFAULT_HISTORY_MESSAGE_ID = 149


def coefficient_from_message(message: str) -> str | None:
    if not message.startswith(_COEFFICIENT_PREFIX):
        return None

    value = message[len(_COEFFICIENT_PREFIX):].strip()

    if not value.endswith("x"):
        return None

    return value


def format_history_coefficient(value) -> str | None:
    try:
        number = float(str(value).strip().rstrip("xX").replace(",", "."))
    except (TypeError, ValueError):
        return None

    if number < 1:
        return None

    return f"{number:.2f}x"


def prepend_history(
    history: list[str],
    coefficient: str,
    *,
    limit: int = _HISTORY_LIMIT,
) -> list[str]:
    size = max(1, int(limit))
    return [coefficient, *list(history)][:size]


def colored_coefficient_label(value: str) -> str:
    """Use screenshot coefficient tiers with compact Telegram labels."""
    formatted = format_history_coefficient(value)
    if formatted is None:
        return str(value)
    coefficient = float(formatted[:-1])
    marker = "🟦" if coefficient < 2 else "🟪" if coefficient < 10 else "🟧"
    return f"{marker}{formatted}"


def build_history_keyboard(
    history: list[str],
    *,
    columns: int = _HISTORY_COLUMNS,
) -> dict:
    width = max(1, int(columns))
    rows = []

    for start in range(0, len(history), width):
        row = []

        for value in history[start:start + width]:
            row.append(
                {
                    "text": colored_coefficient_label(value),
                    "callback_data": "noop",
                }
            )

        rows.append(row)

    return {"inline_keyboard": rows}


class TelegramHistoryPanel:
    def __init__(
        self,
        config: Config,
        *,
        request=None,
        initial_history=None,
        limit: int = _HISTORY_LIMIT,
        columns: int = _HISTORY_COLUMNS,
        message_id=None,
    ):
        self.config = config
        self.limit = max(1, int(limit))
        self.columns = max(1, int(columns))
        if message_id is None:
            raw_message_id = os.getenv(
                "TELEGRAM_HISTORY_MESSAGE_ID",
                str(_DEFAULT_HISTORY_MESSAGE_ID),
            ).strip()
            message_id = int(raw_message_id) if raw_message_id.isdigit() else None
        self.message_id = int(message_id) if message_id is not None else None
        self._request = request or self._telegram_request
        self._lock = asyncio.Lock()
        self._http_client = None
        self.history = []

        for value in list(initial_history or []):
            formatted = format_history_coefficient(value)
            if formatted is not None:
                self.history.append(formatted)

        self.history = self.history[:self.limit]

    async def _telegram_request(self, method: str, payload: dict) -> dict:
        token = self.config.telegram_bot_token
        if not token:
            raise RuntimeError("Telegram bot token missing")

        url = f"https://api.telegram.org/bot{token}/{method}"

        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=15)

        response = await self._http_client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()

        if not data.get("ok", False):
            raise RuntimeError(data.get("description", "Telegram API rejected request"))

        return data

    async def close(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    def _reply_markup(self) -> dict:
        return build_history_keyboard(
            self.history,
            columns=self.columns,
        )

    async def _create_locked(self) -> bool:
        if not self.history:
            return True

        chat_id = self.config.telegram_chat_id
        if not chat_id:
            log.warning("Telegram chat id missing")
            return False

        try:
            data = await self._request(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": _PANEL_TITLE,
                    "reply_markup": self._reply_markup(),
                },
            )
        except (httpx.HTTPError, RuntimeError) as exc:
            log.error(
                "Telegram history panel create failed: %s",
                type(exc).__name__,
            )
            return False

        message_id = data.get("result", {}).get("message_id")
        if message_id is None:
            log.error("Telegram history panel response missing message_id")
            return False

        self.message_id = int(message_id)
        log.info(
            "Telegram history panel created: message_id=%s items=%s; pin this id in TELEGRAM_HISTORY_MESSAGE_ID",
            self.message_id,
            len(self.history),
        )
        return True

    async def _edit_locked(self) -> bool:
        if not self.history:
            return True

        if self.message_id is None:
            return await self._create_locked()

        started = time.monotonic()
        try:
            await self._request(
                "editMessageReplyMarkup",
                {
                    "chat_id": self.config.telegram_chat_id,
                    "message_id": self.message_id,
                    "reply_markup": self._reply_markup(),
                },
            )
        except httpx.HTTPStatusError as exc:
            description = ""
            try:
                description = str(exc.response.json().get("description", ""))
            except Exception:
                description = ""

            if (
                exc.response.status_code == 400
                and "message is not modified" in description.lower()
            ):
                return True

            log.error(
                "Permanent Telegram history edit rejected: HTTP %s message_id=%s; duplicate creation blocked",
                exc.response.status_code,
                self.message_id,
            )
            return False
        except httpx.RequestError as exc:
            log.error(
                "Telegram history panel connection failed: %s",
                type(exc).__name__,
            )
            return False
        except RuntimeError as exc:
            log.error(
                "Permanent Telegram history edit failed: %s message_id=%s; duplicate creation blocked",
                type(exc).__name__,
                self.message_id,
            )
            return False

        log.info(
            "Telegram history panel updated: message_id=%s latest=%s items=%s telegram_request_ms=%.1f",
            self.message_id,
            self.history[0] if self.history else "-",
            len(self.history),
            (time.monotonic() - started) * 1000,
        )
        return True

    async def ensure_panel(self) -> bool:
        async with self._lock:
            return await self._edit_locked()

    async def handle_monitor_message(
        self,
        config_or_message,
        message: str | None = None,
    ) -> bool:
        if message is None:
            message = str(config_or_message)

        coefficient = coefficient_from_message(message)

        if coefficient is None:
            log.info("Telegram monitor notification suppressed")
            return True

        formatted = format_history_coefficient(coefficient)
        if formatted is None:
            log.warning("Invalid coefficient notification suppressed")
            return False

        async with self._lock:
            self.history = prepend_history(
                self.history,
                formatted,
                limit=self.limit,
            )
            return await self._edit_locked()


class TelegramStreakPanel:
    """One temporary full-width button per consecutive sub-2x run."""

    def __init__(self, config, request, initial_history=None):
        self.config = config
        self._request = request
        self._lock = asyncio.Lock()
        self.values = []
        self.message_id = None
        self._deletion_tasks = set()
        for value in reversed(list(initial_history or [])):
            number = format_history_coefficient(value)
            if number is None:
                continue
            if float(number[:-1]) < 2:
                self.values.append(number)
            else:
                self.values.clear()
        self._startup_latest = format_history_coefficient(
            list(initial_history or [None])[0]
        )
        self._startup_pending = True

    def _reply_markup(self, ending=None):
        count = len(self.values)
        latest = ending if ending is not None else self.values[-1]
        title = f"Серия {count} · {colored_coefficient_label(latest)}"
        recent = [*self.values, *([ending] if ending is not None else [])][-5:]
        return {
            "inline_keyboard": [
                [{"text": title, "callback_data": "noop"}],
                [
                    {"text": colored_coefficient_label(value), "callback_data": "noop"}
                    for value in recent
                ],
            ]
        }

    async def _send_or_edit(self, ending=None):
        reply_markup = self._reply_markup(ending=ending)
        if self.message_id is None:
            result = await self._request(
                "sendMessage",
                {
                    "chat_id": self.config.telegram_chat_id,
                    "text": "🎰 Lucky Jet | Серия <2x",
                    "reply_markup": reply_markup,
                },
            )
            self.message_id = int(result["result"]["message_id"])
            log.info("Streak message created: message_id=%s count=%s", self.message_id, len(self.values))
            return

        await self._request(
            "editMessageReplyMarkup",
            {
                "chat_id": self.config.telegram_chat_id,
                "message_id": self.message_id,
                "reply_markup": reply_markup,
            },
        )
        log.info("Streak message updated: message_id=%s count=%s ending=%s", self.message_id, len(self.values), ending)

    async def _delete_later(self, message_id):
        try:
            await asyncio.sleep(30)
            await self._request("deleteMessage", {
                "chat_id": self.config.telegram_chat_id,
                "message_id": message_id,
            })
            log.info("Streak message deleted: message_id=%s", message_id)
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, RuntimeError) as exc:
            log.warning("Streak message deletion failed: %s", type(exc).__name__)

    async def start(self):
        async with self._lock:
            if len(self.values) >= 4:
                try:
                    await self._send_or_edit()
                except (httpx.HTTPError, RuntimeError) as exc:
                    log.warning("Streak startup notification failed: %s", type(exc).__name__)

    async def on_round(self, coefficient):
        value = format_history_coefficient(coefficient)
        if value is None:
            return
        async with self._lock:
            if self._startup_pending:
                self._startup_pending = False
                if value == self._startup_latest:
                    return
            if float(value[:-1]) < 2:
                self.values.append(value)
                if len(self.values) >= 4:
                    try:
                        await self._send_or_edit()
                    except (httpx.HTTPError, RuntimeError) as exc:
                        log.warning("Streak update failed: %s", type(exc).__name__)
            else:
                if len(self.values) >= 4:
                    try:
                        await self._send_or_edit(ending=value)
                        old_message = self.message_id
                        task = asyncio.create_task(self._delete_later(old_message))
                        self._deletion_tasks.add(task)
                        task.add_done_callback(self._deletion_tasks.discard)
                    except (httpx.HTTPError, RuntimeError) as exc:
                        log.warning("Streak completion failed: %s", type(exc).__name__)
                self.values = []
                self.message_id = None

    async def close(self):
        for task in self._deletion_tasks:
            task.cancel()
        if self._deletion_tasks:
            await asyncio.gather(*self._deletion_tasks, return_exceptions=True)


_original_run_monitor = monitor.run_monitor


async def _run_monitor_with_state(config, state=None):
    """Reuse the state already loaded by main so UI and monitor stay synchronized."""
    if state is None:
        return await _original_run_monitor(config)

    original_load_state = monitor.load_state

    async def shared_load_state(_config):
        return state

    monitor.load_state = shared_load_state
    try:
        return await _original_run_monitor(config)
    finally:
        monitor.load_state = original_load_state


monitor.run_monitor = _run_monitor_with_state


async def health_server() -> None:
    port = int(os.getenv("PORT", "10000"))

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await asyncio.wait_for(reader.read(4096), timeout=2)
        except Exception:
            pass
        body = b'{"status":"ok"}'
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "0.0.0.0", port)
    logging.getLogger(__name__).info("Health server listening on port %s", port)
    async with server:
        await server.serve_forever()


async def main_async() -> None:
    config = Config.from_env()
    monitor.async_playwright = low_memory_async_playwright
    monitor.click_demo_mode = wrap_click_demo_mode(
        monitor.click_demo_mode,
    )

    state = await monitor.load_state(config)
    panel = TelegramHistoryPanel(
        config,
        initial_history=state.previous,
    )
    streak = TelegramStreakPanel(config, panel._request, initial_history=state.previous)
    password_gate = TelegramPasswordGate(panel._request)

    async def handle_round(config_or_message, message=None):
        actual = str(config_or_message) if message is None else message
        coefficient = coefficient_from_message(actual)
        if coefficient is not None and len(panel.history) < 2 and len(state.previous) >= 2:
            latest = format_history_coefficient(coefficient)
            saved = format_history_coefficient(state.previous[0])
            if latest == saved:
                panel.history = [
                    formatted
                    for value in state.previous[:panel.limit]
                    if (formatted := format_history_coefficient(value)) is not None
                ]
                streak.values = []
                for value in reversed(state.previous):
                    formatted = format_history_coefficient(value)
                    if formatted is None:
                        continue
                    if float(formatted[:-1]) < 2:
                        streak.values.append(formatted)
                    else:
                        streak.values.clear()
                streak._startup_pending = True
                streak._startup_latest = latest
                result = await panel.ensure_panel()
                await streak.start()
                return result
        result = await panel.handle_monitor_message(config_or_message, message)
        if coefficient is not None:
            await streak.on_round(coefficient)
        return result

    monitor.send = handle_round

    try:
        await asyncio.gather(
            health_server(),
            password_gate.start(),
            panel.ensure_panel(),
            streak.start(),
            monitor.run_monitor(config, state=state),
        )
    finally:
        await streak.close()
        await panel.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
