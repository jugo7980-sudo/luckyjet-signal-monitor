import asyncio

from history_refresh import HistoryRefresh
from luckyjet_reliable_gate import ReliableTelegramPasswordGate


def test_manual_and_automatic_refresh_share_one_inflight_request():
    async def run():
        refresh = HistoryRefresh()
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []
        async def fetch():
            calls.append(1)
            entered.set()
            await release.wait()
            return {"ok": True, "changed": True}
        refresh.bind(fetch)
        first = asyncio.create_task(refresh.request())
        await entered.wait()
        others = [asyncio.create_task(refresh.request()) for _ in range(3)]
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first, *others)
        assert len(calls) == 1
        assert all(r == {"ok": True, "changed": True} for r in results)
        await refresh.request()
        assert len(calls) == 2
        await refresh.close()
    asyncio.run(run())


def test_unavailable_source_is_not_reported_as_current():
    async def run():
        refresh = HistoryRefresh()
        assert (await refresh.request())["ok"] is False
        async def fail():
            raise RuntimeError("offline")
        refresh.bind(fail)
        assert (await refresh.request())["ok"] is False
        await refresh.close()
    asyncio.run(run())


def test_history_buttons_refresh_without_chat_messages_or_streak_button_side_effects():
    async def run():
        calls = []
        requests = []
        async def request(method, payload):
            requests.append((method, payload))
            return {"ok": True, "result": True}
        async def refresh():
            calls.append(1)
            return {"ok": True, "changed": False}
        gate = ReliableTelegramPasswordGate(request, history_provider=lambda: ["1.10x"] * 20,
                                             history_refresher=refresh)
        gate.authorized.add(42)
        gate.chats[42] = 42
        gate.history_messages[42] = 100
        buttons = gate._history_markup()["inline_keyboard"]
        assert all(b["callback_data"] == "history_refresh" for row in buttons[:4] for b in row)
        def callback(data, mid=100):
            return {"id": "cb", "data": data, "from": {"id": 42},
                    "message": {"message_id": mid, "chat": {"id": 42, "type": "private"}}}
        await gate.handle_callback(callback("history_refresh"))
        await asyncio.gather(*gate._refresh_tasks)
        assert calls == [1]
        assert any(m == "answerCallbackQuery" and p.get("text") == "История актуальна" for m, p in requests)
        assert not any(m in {"sendMessage", "deleteMessage"} for m, _ in requests)
        await gate.handle_callback(callback("noop", 200))
        assert calls == [1]
        # Buttons on an existing pre-update history panel must work as well.
        await gate.handle_callback(callback("noop"))
        await asyncio.gather(*gate._refresh_tasks)
        assert calls == [1, 1]
        gate.authorized.clear()
        await gate.handle_callback(callback("history_refresh"))
        assert calls == [1, 1]
        await gate.close()
    asyncio.run(run())


def test_manual_refresh_fetches_source_and_processes_each_new_round_once(monkeypatch):
    from types import SimpleNamespace
    from luckyjet_signal import monitor

    async def run():
        capture = monitor.LiveCapture()
        state = monitor.State(previous=[2.0], last_round_id="r1")
        config = SimpleNamespace(threshold=2.0, trigger_streak=4)
        sent = []
        fetched = []
        async def poll(target):
            fetched.append(target)
            target._rounds = [
                {"id": "r3", "coefficient": 1.2},
                {"id": "r2", "coefficient": 1.1},
                {"id": "r1", "coefficient": 2.0},
            ]
            await asyncio.sleep(0)
            return 3
        async def send(_config, message):
            sent.append(message)
        async def save(_config, _state):
            pass
        monkeypatch.setattr(monitor, "poll_direct_gateways", poll)
        monkeypatch.setattr(monitor, "send", send)
        monkeypatch.setattr(monitor, "save_state", save)
        refresh = HistoryRefresh()
        refresh.bind(lambda: monitor.refresh_live_history(config, state, capture, None))
        results = await asyncio.gather(refresh.request(), refresh.request())
        assert len(fetched) == 1
        assert len(sent) == 2
        assert state.streak == 2
        assert state.last_round_id == "r3"
        assert all(result["ok"] and result["changed"] for result in results)
        result = await refresh.request()
        assert result["ok"] and not result["changed"]
        assert len(fetched) == 2
        assert len(sent) == 2
        assert state.streak == 2
        await refresh.close()
    asyncio.run(run())


def test_failed_source_request_does_not_claim_cached_history_is_current(monkeypatch):
    from types import SimpleNamespace
    from luckyjet_signal import monitor

    async def run():
        capture = monitor.LiveCapture()
        capture._rounds = [{"id": "r1", "coefficient": 2.0}]
        state = monitor.State(previous=[2.0], last_round_id="r1")
        async def poll(_):
            return 0
        monkeypatch.setattr(monitor, "poll_direct_gateways", poll)
        outcome = await monitor.refresh_live_history(SimpleNamespace(), state, capture, None)
        assert outcome["worked"] is True
        assert outcome["ok"] is False
        assert outcome["changed"] is False
    asyncio.run(run())
