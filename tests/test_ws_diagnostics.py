import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from luckyjet_signal.ws_diagnostics import summarize_ws_payload, wrap_ws_handler


def test_summarize_ws_payload_redacts_sensitive_values_and_keeps_shape():
    payload = '42["a",{"event":"round_end","coefficient":1.73,"session":"secret-token","nested":{"status":"finished"}}]'
    summary = summarize_ws_payload(payload)

    assert '"coefficient": 1.73' in summary
    assert '"session": "<redacted>"' in summary
    assert '"status": "finished"' in summary
    assert "secret-token" not in summary


def test_wrap_ws_handler_logs_only_limited_samples(caplog):
    calls = []

    async def delegate(payload, capture, source):
        calls.append((payload, capture, source))

    wrapped = wrap_ws_handler(delegate, max_samples=2)
    caplog.set_level(logging.INFO, logger="luckyjet.wsdiag")

    async def run():
        for n in range(3):
            await wrapped(f'42["a",{{"n":{n}}}]', object(), "ws:test")

    asyncio.run(run())

    messages = [r.message for r in caplog.records if "WS frame sample" in r.message]
    assert len(messages) == 2
    assert len(calls) == 3
