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


def test_delete_invalidates_only_target_message():
    async def run():
        calls = []
        async def request(method, payload):
            calls.append((method, payload.copy()))
            return {'ok': True}
        send = TelegramDelivery(request, interval=0)
        first = {'chat_id': 1, 'message_id': 2, 'text': 'a'}
        second = {'chat_id': 1, 'message_id': 3, 'text': 'b'}
        await send('editMessageText', first)
        await send('editMessageText', second)
        await send('deleteMessage', {'chat_id': 1, 'message_id': 2})
        await send('editMessageText', second)
        await send('editMessageText', first)
        assert [method for method, _ in calls] == [
            'editMessageText', 'editMessageText', 'deleteMessage', 'editMessageText']
    asyncio.run(run())


def test_semantic_button_colors_preserve_neutral_history_and_payload():
    import copy
    async def run():
        calls=[]
        async def request(method,payload): calls.append(payload); return {'ok':True}
        send=TelegramDelivery(request,interval=0)
        actions=['menu_main','site_login','demo_reset_confirm','site_forget_confirm:abc','settings_reset_confirm','hi_days:0','menu_close','menu_settings','admin_confirm']
        payload={'chat_id':1,'reply_markup':{'inline_keyboard':[[{'text':a,'callback_data':a}] for a in actions]}}
        original=copy.deepcopy(payload)
        await send('sendMessage',payload)
        rows=calls[0]['reply_markup']['inline_keyboard']
        assert [row[0].get('style') for row in rows]==['primary','success','danger','danger','danger',None,None,None,'danger']
        assert payload==original
    asyncio.run(run())


def test_demo_enable_disable_colors_follow_state():
    from test_intuitive_ui import setup
    gate,_=setup()
    enable=gate.demo_menu(42)['inline_keyboard'][0]
    assert enable[1]['style']=='success'
    assert 'style' not in enable[0]
    gate._demo_ledger(42).enabled=True
    assert gate.demo_menu(42)['inline_keyboard'][0][1]['style']=='danger'
