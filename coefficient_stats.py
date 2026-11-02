"""Compact ten-day coefficient statistics in Redis (Asia/Bangkok days)."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Bangkok')
THRESHOLDS = (10, 25, 50, 100, 200, 300, 500, 800)

class CoefficientStats:
    def __init__(self, redis): self.redis = redis
    @staticmethod
    def day(timestamp=None):
        dt = datetime.fromtimestamp(timestamp, TZ) if timestamp is not None else datetime.now(TZ)
        return dt.strftime('%Y-%m-%d')
    async def record(self, coefficient, *, timestamp=None):
        if self.redis is None: return False
        value = float(str(coefficient).rstrip('xX'))
        timestamp = datetime.now(TZ).timestamp() if timestamp is None else float(timestamp)
        day = self.day(timestamp)
        pipe = self.redis.pipeline()
        pipe.hincrby(f'luckyjet:coef:{day}', 'total', 1)
        pipe.hset(f'luckyjet:coef:{day}', 'updated', str(timestamp))
        pipe.zadd(f'luckyjet:coef:rounds:{day}', {f'{timestamp:.6f}|{value:.2f}': timestamp})
        for threshold in THRESHOLDS:
            if value > threshold: pipe.hincrby(f'luckyjet:coef:{day}', str(threshold), 1)
        pipe.expire(f'luckyjet:coef:{day}', 950400)
        pipe.expire(f'luckyjet:coef:rounds:{day}', 950400)
        await pipe.execute()
        return True
    async def summary(self, day):
        values = await self.redis.hgetall(f'luckyjet:coef:{day}') if self.redis else {}
        return {key: int(value) for key, value in values.items() if key != 'updated'}
    async def rounds(self, day, threshold=10, page=0, limit=10):
        raw = await self.redis.zrevrange(f'luckyjet:coef:rounds:{day}', 0, -1) if self.redis else []
        rows=[]
        for item in raw:
            item=item.decode() if isinstance(item,bytes) else item
            stamp, coefficient=item.split('|',1)
            if float(coefficient)>threshold: rows.append((float(stamp),float(coefficient)))
        total=len(rows)
        return rows[page*limit:(page+1)*limit],total
