import asyncio
from telegram_delivery import TelegramDelivery


def test_unchanged_edits_skipped_changes_sent_and_failed_edits_retried():
    async def run():
        calls = []
        async def request(method, payload):
            calls.append((method, payload.copy()))
            if payload.get('text') == 'fail': raise RuntimeError('offline')
            return {'ok': True}
        send = TelegramDelivery(request, interval=0)
        p = {'chat_id': 1, 'message_id': 2, 'text': 'a'}
        await send('editMessageText', p)
        await send('editMessageText', p)
        assert len(calls) == 1
        p['text'] = 'b'
        await send('editMessageText', p)
        assert len(calls) == 2
        p['text'] = 'fail'
        for _ in range(2):
            try: await send('editMessageText', p)
            except RuntimeError: pass
        assert len(calls) == 4
    asyncio.run(run())
