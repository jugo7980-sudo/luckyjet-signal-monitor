import asyncio

from telegram_polling_lease import TelegramPollingLease


class FakeRedis:
    def __init__(self):
        self.data = {}

    async def ping(self):
        return True

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.data:
            return False
        self.data[key] = value
        return True

    async def eval(self, script, numkeys, key, owner, *args):
        if self.data.get(key) != owner:
            return 0
        if "del" in script.lower():
            self.data.pop(key, None)
            return 1
        return 1

    async def aclose(self):
        return None


def test_only_one_polling_lease_owner_at_a_time():
    redis = FakeRedis()
    first = TelegramPollingLease("redis://test", client=redis, owner="first")
    second = TelegramPollingLease("redis://test", client=redis, owner="second")

    async def run():
        assert await first.acquire() is True
        assert await second.acquire() is False
        await first.release()
        assert await second.acquire() is True

    asyncio.run(run())
