"""Runtime guards that keep Lucky Jet round history stable across network sources."""
from __future__ import annotations

import time


def install_source_guard(monitor, *, direct_hold_seconds=15.0):
    """Patch monitor once so recent direct history wins over noisy socket snapshots."""
    if getattr(monitor, "_luckyjet_source_guard_installed", False):
        return

    hold_seconds = max(1.0, float(direct_hold_seconds))
    original_ingest = monitor.LiveCapture.ingest
    original_direct_targets = monitor.LiveCapture.direct_targets
    original_process_network_history = monitor.process_network_history

    async def guarded_ingest(self, payload, source, event_name=""):
        now = time.monotonic()
        stable_source = getattr(self, "_stable_source", "")
        stable_at = float(getattr(self, "_stable_source_at", 0.0) or 0.0)
        incoming_direct = str(source).startswith("direct:")
        direct_is_fresh = (
            str(stable_source).startswith("direct:")
            and now - stable_at < hold_seconds
        )

        if direct_is_fresh and not incoming_direct:
            return len(getattr(self, "_rounds", []))

        result = await original_ingest(self, payload, source, event_name=event_name)
        if result:
            self._stable_source = str(source)
            self._stable_source_at = now
        return result

    async def guarded_direct_targets(self):
        urls, headers = await original_direct_targets(self)

        def priority(url):
            low = str(url).lower()
            if "history" in low:
                rank = 0
            elif "/state" in low or low.endswith("state"):
                rank = 1
            elif "round" in low:
                rank = 2
            else:
                rank = 3
            return rank, low

        return sorted(urls, key=priority), headers

    async def guarded_process_network_history(config, state, rounds):
        if rounds and state.last_round_id and rounds[0].get("id"):
            ids = [item.get("id") for item in rounds]
            if state.last_round_id not in ids:
                coefficients = [float(item["coefficient"]) for item in rounds]
                newest_id = rounds[0].get("id")

                if coefficients == list(state.previous or []):
                    state.last_round_id = newest_id
                    state.previous = coefficients
                    await monitor.save_state(config, state)
                    return True

                results = monitor.new_results(list(state.previous or []), coefficients)
                if results:
                    for multiplier in results:
                        await monitor.process_completed_round(
                            config,
                            state,
                            float(multiplier),
                        )
                    state.last_round_id = newest_id
                    state.previous = coefficients
                    await monitor.save_state(config, state)
                    return True

        return await original_process_network_history(config, state, rounds)

    monitor.LiveCapture.ingest = guarded_ingest
    monitor.LiveCapture.direct_targets = guarded_direct_targets
    monitor.process_network_history = guarded_process_network_history
    monitor._luckyjet_source_guard_installed = True
