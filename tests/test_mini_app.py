import asyncio
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from mini_app import response,app_url


def test_static_shell_route_keeps_health_and_rejects_other_paths():
    status, kind, body=response('/app?version=1')
    assert status=='200 OK' and kind.startswith('text/html')
    assert 'page-home' in body.decode() and 'page-history' in body.decode()
    assert response('/')[2]==b'{"status":"ok"}'
    assert response('/app/../../site_account.py')[0]=='404 Not Found'
    assert app_url().startswith('https://')


def test_shell_tabs_mobile_browser():
    from playwright.async_api import async_playwright
    async def run():
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True)
            page=await browser.new_page(viewport={'width':390,'height':844})
            errors=[]
            page.on('pageerror',lambda exc:errors.append(str(exc)))
            async def route(request):
                if request.request.url.startswith('https://example.test/app'):
                    await request.fulfill(content_type='text/html',body=response('/app')[2])
                else:
                    await request.fulfill(content_type='application/javascript',body="window.Telegram={WebApp:{initData:'',ready(){},expand(){},setHeaderColor(){},setBackgroundColor(){},close(){},BackButton:{onClick(){},hide(){},show(){}},HapticFeedback:{selectionChanged(){}}}}")
            await page.route('**/*',route)
            await page.goto('https://example.test/app')
            for tab in ['demo','history','settings','home']:
                await page.locator('nav button[data-tab="'+tab+'"]').click()
                assert await page.locator('#page-'+tab).is_visible()
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert not errors
            assert await page.evaluate("""() => {const e=new MouseEvent('click',{bubbles:true,cancelable:true}); document.getElementById('return-bot').dispatchEvent(e); return !e.defaultPrevented}""")
            await browser.close()
    asyncio.run(run())


def test_launcher_in_main_menu_and_private_chat_menu():
    from luckyjet_password_gate import TelegramPasswordGate
    async def run():
        calls=[]
        async def request(method,payload):calls.append((method,payload));return {'ok':True}
        gate=TelegramPasswordGate(request)
        launch=gate.menu(42)['inline_keyboard'][0][0]
        assert launch['web_app']['url']==app_url() and 'callback_data' not in launch
        await gate._register_app_button(42)
        assert calls[0][0]=='setChatMenuButton'
        assert calls[0][1]['chat_id']==42
        assert calls[0][1]['menu_button']['web_app']['url']==app_url()
        await gate.close()
    asyncio.run(run())


def test_live_read_view_and_access_denied_clear_private_data():
    import json
    from playwright.async_api import async_playwright
    async def run():
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True)
            page=await browser.new_page(viewport={'width':390,'height':844})
            denied=[False]
            async def route(request):
                url=request.request.url
                if '/api/overview' in url:
                    assert request.request.headers['authorization']=='tma fixture'
                    data={'error':'Нет доступа'} if denied[0] else {'coefficients':['1.20x','3.00x','125.00x'],'last_round_at':None,'demo':{'balance_cents':123450,'enabled':True,'active':True,'step':4,'base_stake_cents':1000,'trigger':3}}
                    await request.fulfill(status=403 if denied[0] else 200,content_type='application/json',body=json.dumps(data))
                elif url.startswith('https://example.test/app'):
                    await request.fulfill(content_type='text/html',body=response('/app')[2])
                else:
                    await request.fulfill(content_type='application/javascript',body="window.Telegram={WebApp:{initData:'fixture',ready(){},expand(){},setHeaderColor(){},setBackgroundColor(){},close(){},BackButton:{onClick(){},hide(){},show(){}},HapticFeedback:{selectionChanged(){}}}}")
            await page.route('**/*',route)
            await page.goto('https://example.test/app')
            await page.locator('#live-home').wait_for(state='visible')
            assert await page.locator('.coef').count()==3
            assert not await page.locator('.art').is_visible()
            await page.locator('nav [data-tab=demo]').click()
            assert await page.locator('#demo-step').inner_text()=='Шаг 4'
            assert not await page.locator('#demo-placeholder').is_visible()
            denied[0]=True
            await page.locator('nav [data-tab=home]').click()
            await page.locator('#refresh-data').click()
            await page.locator('#live-home').wait_for(state='hidden')
            assert not await page.locator('#live-demo').is_visible()
            assert await page.locator('#live-status').inner_text()=='Нет доступа'
            await browser.close()
    asyncio.run(run())
