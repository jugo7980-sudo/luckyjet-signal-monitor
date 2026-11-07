"""One-time total Lucky Jet data reset under the exclusive Telegram polling lease.

Deletes all Lucky Jet Redis data, including all user sessions and game history.
Only technical ownership / migration markers remain. This module MUST be called
after the old monitor stops writing and before monitor.load_state().
"""
import logging

log = logging.getLogger("luckyjet.full_reset")

RESET_ID = "2026-11-07-complete-data-reset-1"
RESET_MARKER_KEY = "luckyjet:complete_data_reset_version"
# The active polling lease must remain intact; deleting it would permit
# multiple bot workers to read updates concurrently during deployment.
PRESERVE = frozenset({
    "luckyjet:telegram_polling_lease",
    "luckyjet:release_version",
    "luckyjet:access_reset_version",
    RESET_MARKER_KEY,
})


async def apply_complete_data_reset(redis, *, scan_count=500):
    """Remove ALL persisted app/user state once without clearing the Redis DB.

    Requires exclusive polling lease. SCAN+UNLINK in multiple passes allows a
    high number of per-round deduplication keys without a blocking KEYS query
    or FLUSHDB that could erase unrelated apps and the active worker lease.
    """
    if redis is None:
        raise RuntimeError("Persistent Redis is required for a verified full reset")
    if await redis.get(RESET_MARKER_KEY) == RESET_ID:
        return False

    total_deleted = 0
    while True:
        removed_this_pass = 0
        cursor = 0
        while True:
            cursor, keys = await redis.scan(
                cursor=cursor, match="luckyjet:*", count=scan_count,
            )
            to_delete = [key for key in keys if key not in PRESERVE]
            if to_delete:
                await redis.unlink(*to_delete)
                removed_this_pass += len(to_delete)
            if cursor == 0:
                break
        total_deleted += removed_this_pass
        if not removed_this_pass:
            break

    # Marker is written only after a complete successful scan; a failed
    # partial reset will continue on the next start.
    await redis.set(RESET_MARKER_KEY, RESET_ID)
    log.warning(
        "Full data reset completed: removed_keys=%d; authorizations, Premium, "
        "DEMO, history, coefficients, notifications, settings and sessions cleared",
        total_deleted,
    )
    return True
