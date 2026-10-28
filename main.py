from __future__ import annotations

import asyncio
import logging
import os

import httpx

from luckyjet_signal.settings import Config
from luckyjet_signal import monitor
from luckyjet_signal.browser_runtime import low_memory_async_playwright
from luckyjet_signal.demo_mode import wrap_click_demo_mode


log = logging.getLogger("luckyjet.telegram_ui")
_COEFFICIENT_PREFIX = "Последний коэффициент: "
_PANEL_TITLE = "🎰 Lucky Jet — история"
_HISTORY_LIMIT = 15
_HISTORY_COLUMNS = 3


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
                    "text": value,
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
    ):
        self.config = config
        self.limit = max(1, int(limit))
        self.columns = max(1, int(columns))
        self.message_id = None
        self._request = request or self._telegram_request
        self._lock = asyncio.Lock()
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

        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()

        if not data.get("ok", False):
            raise RuntimeError(data.get("description", "Telegram API rejected request"))

        return data

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
            "Telegram history panel created: message_id=%s items=%s",
            self.message_id,
            len(self.history),
        )
        return True

    async def _edit_locked(self) -> bool:
        if self.message_id is None:
            return await self._create_locked()

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

            log.warning(
                "Telegram history panel edit rejected: HTTP %s; recreating",
                exc.response.status_code,
            )
            self.message_id = None
            return await self._create_locked()
        except httpx.RequestError as exc:
            log.error(
                "Telegram history panel connection failed: %s",
                type(exc).__name__,
            )
            return False
        except RuntimeError as exc:
            log.warning(
                "Telegram history panel edit failed: %s; recreating",
                type(exc).__name__,
            )
            self.message_id = None
            return await self._create_locked()

        log.info(
            "Telegram history panel updated: message_id=%s latest=%s items=%s",
            self.message_id,
            self.history[0] if self.history else "-",
            len(self.history),
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
    monitor.send = panel.handle_monitor_message

    await asyncio.gather(
        health_server(),
        panel.ensure_panel(),
        monitor.run_monitor(config),
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
