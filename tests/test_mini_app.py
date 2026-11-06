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
