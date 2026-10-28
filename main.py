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
_original_send = monitor.send
_COEFFICIENT_PREFIX = "Последний коэффициент: "


def coefficient_from_message(message: str) -> str | None:
    if not message.startswith(_COEFFICIENT_PREFIX):
        return None

    value = message[len(_COEFFICIENT_PREFIX):].strip()

    if not value.endswith("x"):
        return None

    return value


async def send_with_coefficient_button(config: Config, message: str) -> bool:
    coefficient = coefficient_from_message(message)

    if coefficient is None:
        return await _original_send(config, message)

    token = config.telegram_bot_token
    chat_id = config.telegram_chat_id

    if not token or not chat_id:
        log.warning("Telegram credentials missing")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": coefficient,
                    "reply_markup": {
                        "inline_keyboard": [
                            [
                                {
                                    "text": coefficient,
                                    "callback_data": "noop",
                                }
                            ]
                        ]
                    },
                },
            )
            response.raise_for_status()

        log.info("Telegram coefficient button delivered: %s", coefficient)
        return True

    except httpx.HTTPStatusError as exc:
        log.error(
            "Telegram rejected coefficient button: HTTP %s",
            exc.response.status_code,
        )
        return False

    except httpx.RequestError as exc:
        log.error(
            "Telegram coefficient button connection failed: %s",
            type(exc).__name__,
        )
        return False


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
    monitor.send = send_with_coefficient_button
    await asyncio.gather(
        health_server(),
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
