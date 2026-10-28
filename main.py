from __future__ import annotations

import asyncio
import logging
import os

from luckyjet_signal.settings import Config
from luckyjet_signal import monitor
from luckyjet_signal.browser_diagnostics import wrap_diagnose_page
from luckyjet_signal.browser_runtime import low_memory_async_playwright
from luckyjet_signal.ws_diagnostics import wrap_ws_handler


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
    monitor.handle_ws_frame = wrap_ws_handler(
        monitor.handle_ws_frame,
        max_samples=8,
    )
    monitor.diagnose_page = wrap_diagnose_page(
        monitor.diagnose_page,
    )
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
