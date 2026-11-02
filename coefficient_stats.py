"""Atomic, deduplicated daily counters with explicitly partial coverage."""
from collections import OrderedDict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Bangkok')
THRESHOLDS = (10, 25, 50, 100, 200, 300, 500, 800, 1000, 1500, 2000, 3000, 5000)
SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
redis.call('SET', KEYS[1], '1', 'EX', 7948800)
redis.call('HINCRBY', KEYS[2], 'total', 1)
redis.call('HSET', KEYS[2], 'partial', '1')
if ARGV[17] == '1' then redis.call('HSET', KEYS[2], 'time_uncertain', '1') end
for i=2,14 do
 if tonumber(ARGV[1]) > tonumber(ARGV[i]) then
  redis.call('HINCRBY', KEYS[2], ARGV[i], 1)
 end
end
redis.call('EXPIRE', KEYS[2], 7948800)
redis.call('ZADD', KEYS[3], ARGV[15], ARGV[16])
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

    @staticmethod
    def day(timestamp=None):
        return (datetime.fromtimestamp(timestamp, TZ) if timestamp is not None else datetime.now(TZ)).strftime('%Y-%m-%d')

    async def ingest(self, rounds):
        if self.redis is None:
            return
        for item in reversed(rounds):
            rid = item.get('id')
            if rid is None or not str(rid) or str(rid) in self.seen:
                continue
            stamp = event_time(item)
            # Historical records without event time cannot be assigned a day safely.
            uncertain = stamp is None
            if stamp is None:
                if item is not rounds[0]:
                    continue
                stamp = datetime.now(TZ)
            await self.record(item.get('coefficient'), round_id=str(rid), timestamp=stamp.timestamp(), uncertain=uncertain)

    async def record(self, coefficient, *, round_id=None, timestamp=None, uncertain=False):
        if self.redis is None or not round_id or timestamp is None:
            return False
        try:
            value = Decimal(str(coefficient).rstrip('xX'))
        except InvalidOperation:
            return False
        if not value.is_finite() or value < 1:
            return False
        day = self.day(timestamp)
        result = await self.redis.eval(SCRIPT, 3, f'luckyjet:coef:seen:{round_id}',
            f'luckyjet:coef:{day}', 'luckyjet:coef:days', str(value),
            *(str(t) for t in THRESHOLDS), str(timestamp), day, '1' if uncertain else '0')
        self.seen[str(round_id)] = None
        while len(self.seen) > 1000:
            self.seen.popitem(last=False)
        return bool(result)

    async def bootstrap(self):
        if self.redis is None:
            return
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

    async def days(self, page=0):
        if self.redis is None:
            return [], 0
        cutoff=(datetime.now(TZ)-timedelta(days=90)).timestamp()
        await self.redis.zremrangebyscore('luckyjet:coef:days','-inf',cutoff)
        total = await self.redis.zcard('luckyjet:coef:days')
        values = await self.redis.zrevrange('luckyjet:coef:days', page * 10, page * 10 + 9)
        return [v.decode() if isinstance(v, bytes) else v for v in values], total

    async def summary(self, day):
        values = await self.redis.hgetall(f'luckyjet:coef:{day}') if self.redis else {}
        return {key: int(value) for key, value in values.items() if key not in {'updated'}}
