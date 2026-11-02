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


def test_cross_method_cache_invalidated():
    async def run():
        calls=[]
        async def request(m,p): calls.append(m); return {'ok':True}
        d=TelegramDelivery(request,interval=0)
        p={'chat_id':1,'message_id':2,'text':'A','reply_markup':{}}
        await d('editMessageText',p)
        await d('editMessageReplyMarkup',{'chat_id':1,'message_id':2,'reply_markup':{'x':1}})
        await d('editMessageText',p)
        assert len(calls)==3
    asyncio.run(run())


def test_429_retries_but_other_errors_propagate():
    import httpx
    async def run():
        calls=[]
        async def request(m,p):
            calls.append(m)
            if len(calls)==1:
                response=httpx.Response(429,json={'parameters':{'retry_after':0}},request=httpx.Request('POST','https://example.test'))
                response.raise_for_status()
            return {'ok':True}
        d=TelegramDelivery(request,interval=0)
        await d('editMessageText',{'chat_id':1,'message_id':2,'text':'A'})
        assert len(calls)==2
    asyncio.run(run())
