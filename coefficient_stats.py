"""Atomic, deduplicated daily counters with explicitly partial coverage."""
from collections import OrderedDict
import hashlib
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Vladivostok')
THRESHOLDS = (2, 5, 10, 25, 50, 100, 200, 300, 500, 800, 1000, 1500, 2000, 3000, 5000)
UNDER2_FIX_DAY = '2026-11-03'
UNDER2_FIX_MARKER = 'luckyjet:coef:lt2-single-count-fixed-v1'
SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
redis.call('SET', KEYS[1], '1')
local day_existed = redis.call('EXISTS', KEYS[2]) == 1
if day_existed and redis.call('HEXISTS', KEYS[2], 'lt2_tracking') == 0 then redis.call('HSET', KEYS[2], 'lt2_partial', '1') end
redis.call('HSET', KEYS[2], 'lt2_tracking', '1')
if day_existed and redis.call('HEXISTS', KEYS[2], 'new_threshold_tracking') == 0 then redis.call('HSET', KEYS[2], 'new_threshold_partial', '1') end
redis.call('HSET', KEYS[2], 'new_threshold_tracking', '1')
redis.call('HINCRBY', KEYS[2], 'total', 1)
redis.call('HSET', KEYS[2], 'partial', '1')
if tonumber(ARGV[1]) < 2 then redis.call('HINCRBY', KEYS[2], 'lt2', 1) end
if ARGV[19] == '1' then redis.call('HSET', KEYS[2], 'time_uncertain', '1') end
for i=2,16 do
 if tonumber(ARGV[1]) > tonumber(ARGV[i]) then
  redis.call('HINCRBY', KEYS[2], ARGV[i], 1)
 end
end
redis.call('ZADD', KEYS[3], ARGV[17], ARGV[18])
local last = tonumber(redis.call('HGET', KEYS[4], 'stamp') or '-1')
local stamp = tonumber(ARGV[17])
if stamp > last or ARGV[23] == '1' then
 local since = tonumber(redis.call('HGET', KEYS[4], 'since') or '0') + 1
 local previous = redis.call('HGET', KEYS[4], 'last_high')
 local gap = redis.call('HGET', KEYS[4], 'gap') == '1' or ARGV[21] == '1'
 if tonumber(ARGV[1]) > 100 then
  local entry = {id=ARGV[22], value=ARGV[1], stamp=stamp, day=ARGV[18], previous=previous or cjson.null, rounds=cjson.null, uncertain=ARGV[19]=='1'}
  if previous and not gap then entry.rounds=since end
  redis.call('HSET', 'luckyjet:coef:high:entries', ARGV[22], cjson.encode(entry))
  redis.call('LPUSH', KEYS[5], ARGV[22])
  redis.call('ZADD', KEYS[6], stamp, ARGV[18])
  redis.call('HSET', KEYS[4], 'last_high', cjson.encode({value=ARGV[1], stamp=stamp, id=ARGV[22]}), 'gap', '0')
  since=0
 elseif gap then redis.call('HSET', KEYS[4], 'gap', '1') end
 redis.call('HSET', KEYS[4], 'since', since, 'stamp', math.max(stamp, last), 'last_id', ARGV[20])
end
return 1
"""
UNDER2_DOUBLE_FIX = """
if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
local raw = redis.call('HGET', KEYS[2], 'lt2')
if raw then
 local count = tonumber(raw)
 if count and count >= 0 then
  redis.call('HSET', KEYS[2], 'lt2', math.floor(count / 2))
 end
end
redis.call('SET', KEYS[1], '1')
return 1
"""


def event_time(item):
    for key in ('finished_at', 'finishedAt', 'ended_at', 'endedAt', 'timestamp'):
        raw = item.get(key)
        if raw is None:
            continue
        try:
            if isinstance(raw, (int, float)) or str(raw).replace('.', '', 1).isdigit():
                value = float(raw)
                if value > 1e12:
                    value /= 1000
                return datetime.fromtimestamp(value, TZ)
            parsed = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
            if parsed.tzinfo is not None:
                return parsed.astimezone(TZ)
        except (ValueError, TypeError, OverflowError, OSError):
            pass
    return None


class CoefficientStats:
    def __init__(self, redis):
        self.redis = redis
        self.seen = OrderedDict()
        self.reset_baseline_pending = False

    @staticmethod
    def day(timestamp=None):
        return (datetime.fromtimestamp(timestamp, TZ) if timestamp is not None else datetime.now(TZ)).strftime('%Y-%m-%d')

    async def ingest(self, rounds):
        if self.redis is None:
            return
        if self.reset_baseline_pending:
            # Do not rebuild recently wiped daily statistics by replaying
            # the game's pre-reset source snapshot. Future rounds count as new.
            ids = {str(item['id']) for item in rounds
                   if isinstance(item, dict) and item.get('id') is not None}
            if ids:
                pipe = self.redis.pipeline()
                for rid in ids:
                    pipe.set(f'luckyjet:coef:seen:{rid}', '1', ex=7948800, nx=True)
                await pipe.execute()
                self.reset_baseline_pending = False
                return
            # A snapshot with no usable IDs is not a valid baseline.
            return
        last_id = await self.redis.hget('luckyjet:coef:high:state', 'last_id')
        if isinstance(last_id, bytes): last_id = last_id.decode()
        ids = [str(item.get('id')) for item in rounds]
        anchor = ids.index(last_id) if last_id in ids else -1
        gap = bool(last_id and anchor == -1)
        last_stamp = float(await self.redis.hget('luckyjet:coef:high:state', 'stamp') or -1)
        for item in reversed(rounds):
            rid = item.get('id')
            if rid is None or not str(rid) or str(rid) in self.seen:
                continue
            stamp = event_time(item)
            # Historical records without event time cannot be assigned a day safely.
            uncertain = stamp is None
            if stamp is None:
                if item is not rounds[0]:
                    if anchor >= 0 and ids.index(str(rid)) < anchor:
                        gap = True
                    continue
                stamp = datetime.now(TZ)
            ordered = anchor >= 0 and ids.index(str(rid)) < anchor
            changed = await self.record(item.get('coefficient'), round_id=str(rid), timestamp=stamp.timestamp(), uncertain=uncertain, gap=gap, ordered=ordered)
            if changed and (ordered or stamp.timestamp() > last_stamp):
                gap = False
                last_stamp = stamp.timestamp()

    async def record(self, coefficient, *, round_id=None, timestamp=None, uncertain=False, gap=False, ordered=False):
        if self.redis is None or not round_id or timestamp is None:
            return False
        try:
            value = Decimal(str(coefficient).rstrip('xX'))
        except InvalidOperation:
            return False
        if not value.is_finite() or value < 1:
            return False
        day = self.day(timestamp)
        result = await self.redis.eval(SCRIPT, 6, f'luckyjet:coef:seen:{round_id}',
            f'luckyjet:coef:{day}', 'luckyjet:coef:days', 'luckyjet:coef:high:state',
            f'luckyjet:coef:high:{day}', 'luckyjet:coef:high:days', str(value),
            *(str(t) for t in THRESHOLDS), str(timestamp), day, '1' if uncertain else '0', str(round_id), '1' if gap else '0', hashlib.sha256(str(round_id).encode()).hexdigest()[:16], '1' if ordered else '0')
        self.seen[str(round_id)] = None
        while len(self.seen) > 1000:
            self.seen.popitem(last=False)
        return bool(result)

    async def bootstrap(self):
        if self.redis is None:
            return
        await self.redis.eval(
            UNDER2_DOUBLE_FIX,
            2,
            UNDER2_FIX_MARKER,
            f'luckyjet:coef:{UNDER2_FIX_DAY}',
        )
        today=datetime.now(TZ).date()
        dates=[(today-timedelta(days=i)).isoformat() for i in range(11)]
        pipe=self.redis.pipeline()
        for day in dates:
            pipe.hget(f'luckyjet:coef:{day}', 'total')
        totals=await pipe.execute()
        values={day:datetime.fromisoformat(day).replace(tzinfo=TZ).timestamp()
                for day,total in zip(dates,totals) if total and int(total)>0}
        if values:
            await self.redis.zadd('luckyjet:coef:days',values,nx=True)

        # Keep existing keys created before the no-expiry policy. Without
        # this one-time migration old daily snapshots and seen-round IDs
        # would still expire at their originally scheduled TTL.
        if hasattr(self.redis, 'scan_iter'):
            await self._preserve_legacy_retention()

    async def _preserve_legacy_retention(self):
        marker = 'luckyjet:coef:retention-upgraded-v1'
        if await self.redis.exists(marker):
            return
        pipe = self.redis.pipeline()
        batched = 0
        async for key in self.redis.scan_iter(match='luckyjet:coef:seen:*', count=500):
            pipe.persist(key)
            batched += 1
            if batched >= 500:
                await pipe.execute()
                pipe = self.redis.pipeline()
                batched = 0
        days = await self.redis.zrange('luckyjet:coef:days', 0, -1)
        for day in days:
            if isinstance(day, bytes):
                day = day.decode('utf-8')
            pipe.persist('luckyjet:coef:' + str(day))
            batched += 1
            if batched >= 500:
                await pipe.execute()
                pipe = self.redis.pipeline()
                batched = 0
        if batched:
            await pipe.execute()
        await self.redis.set(marker, '1')


    async def days(self, page=0):
        if self.redis is None:
            return [], 0
        # Historical daily counters must not vanish after a fixed horizon.
        # Exact archive source IDs also remain deduplicated indefinitely.
        pipe = self.redis.pipeline()
        pipe.zcard('luckyjet:coef:days')
        pipe.zrevrange('luckyjet:coef:days', page * 10, page * 10 + 9)
        total, values = await pipe.execute()
        return [v.decode() if isinstance(v, bytes) else v for v in values], total

    async def summary(self, day):
        values = await self.redis.hgetall(f'luckyjet:coef:{day}') if self.redis else {}
        return {key: int(value) for key, value in values.items() if key not in {'updated'}}
