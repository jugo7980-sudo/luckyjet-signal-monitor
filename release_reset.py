"""One-time, atomic release reset executed only by the Telegram polling owner."""
import json
import logging

from luckyjet_signal.demo_ledger import DemoLedger

RELEASE_ID = "2026-11-02-release-1"
MARKER_KEY = "luckyjet:release_version"

_RESET = """
if redis.call('get', KEYS[1]) == ARGV[1] then return {0, 0, 0} end
local users = redis.call('scard', KEYS[2])
local sessions = redis.call('hgetall', KEYS[3])
for i = 1, #sessions, 2 do
  local fresh = cjson.decode(ARGV[2])
  local ok, old = pcall(cjson.decode, sessions[i + 1])
  if ok and type(old) == 'table' then
    if type(old.trigger_streak) == 'number' then fresh.trigger_streak = old.trigger_streak end
    if type(old.base_stake_cents) == 'number' then
      fresh.base_stake_cents = math.min(fresh.balance_cents, math.max(1000, old.base_stake_cents))
      fresh.next_stake_cents = fresh.base_stake_cents
    end
  end
  redis.call('hset', KEYS[3], sessions[i], cjson.encode(fresh))
end
redis.call('del', KEYS[2], KEYS[4])
redis.call('set', KEYS[1], ARGV[1])
return {1, users, #sessions / 2}
"""


async def apply_release_reset(redis):
    result = await redis.eval(
        _RESET, 4, MARKER_KEY, "luckyjet:authorized_users",
        "luckyjet:demo_sessions", "luckyjet:history_hidden",
        RELEASE_ID, json.dumps(DemoLedger().dump()),
    )
    changed, users, sessions = map(int, result)
    logging.getLogger("luckyjet.release").info(
        "Release %s reset: applied=%s authorized_users=%s demo_sessions=%s",
        RELEASE_ID, bool(changed), users, sessions,
    )
    return bool(changed)
