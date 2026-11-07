"""Telegram native home-screen shortcut: visible to guests, user-initiated, status-aware."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from mini_app import response


def test_home_screen_shortcut_in_telegram():
    from playwright.async_api import async_playwright

    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            telegram = """
                window.__shortcut={calls:0, handlers:{}};
                window.Telegram={WebApp:{
                  initData:'fixture', ready(){}, expand(){},
                  setHeaderColor(){}, setBackgroundColor(){},
                  isVersionAtLeast(v){return v==='8.0';},
                  addToHomeScreen(){window.__shortcut.calls++;},
                  checkHomeScreenStatus(cb){cb('missed');},
                  onEvent(name,cb){window.__shortcut.handlers[name]=cb;},
                  BackButton:{onClick(){},hide(){},show(){}},
                  HapticFeedback:{selectionChanged(){}}
                }};
            """

            async def route(req):
                url = req.request.url
                if url.startswith("https://example.test/app"):
                    await req.fulfill(content_type="text/html", body=response("/app")[2])
                elif url.startswith("https://telegram.org/js/telegram-web-app.js"):
                    await req.fulfill(content_type="application/javascript", body=telegram)
                elif url.endswith(("/history.js", "/controls.js", "/admin.js", "/site.js", "/access.js")):
                    await req.fulfill(content_type="application/javascript", body=response("/" + url.split("/")[-1])[2])
                elif "/api/access" in url:
                    await req.fulfill(content_type="application/json", body=json.dumps({
                        "verified": False, "registered": False, "premium": False, "username": None
                    }))
                elif "/api/public" in url:
                    await req.fulfill(content_type="application/json", body=json.dumps({
                        "coefficients": ["1.25x"], "last_round_at": None, "limit": 20
                    }))
                else:
                    await req.fulfill(content_type="application/javascript", body="")

            await page.route("**/*", route)
            await page.goto("https://example.test/app")
            await page.locator("nav [data-tab=account]").click()
            button = page.locator("#install-shortcut")
            assert await button.is_visible()
            assert await button.is_enabled()
            assert await page.locator("#install-shortcut-label").inner_text() == "На главный экран"
            await button.click()
            assert await page.evaluate("window.__shortcut.calls") == 1
            assert "Подтвердите" in await page.locator("#install-shortcut-status").inner_text()
            await page.evaluate("window.__shortcut.handlers.homeScreenAdded()")
            assert await button.is_disabled()
            assert "уже добавлен" in await page.locator("#install-shortcut-label").inner_text()
            assert not errors, errors
            await browser.close()

    asyncio.run(run())


def test_shortcut_not_offered_when_telegram_unsupported():
    from playwright.async_api import async_playwright

    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))

            async def route(req):
                url = req.request.url
                if url.startswith("https://example.test/app"):
                    await req.fulfill(content_type="text/html", body=response("/app")[2])
                elif url.startswith("https://telegram.org/js/telegram-web-app.js"):
                    await req.fulfill(content_type="application/javascript", body="""
                        window.Telegram={WebApp:{
                          ready(){}, expand(){}, setHeaderColor(){}, setBackgroundColor(){},
                          isVersionAtLeast(){return false;},
                          BackButton:{onClick(){},hide(){},show(){}},
                          HapticFeedback:{selectionChanged(){}}
                        }};
                    """)
                elif url.endswith(("/history.js", "/controls.js", "/admin.js", "/site.js", "/access.js")):
                    await req.fulfill(content_type="application/javascript", body=response("/" + url.split("/")[-1])[2])
                elif "/api/access" in url:
                    await req.fulfill(content_type="application/json", body=json.dumps({
                        "verified": False, "registered": False, "premium": False
                    }))
                elif "/api/public" in url:
                    await req.fulfill(content_type="application/json", body=json.dumps({
                        "coefficients": [], "last_round_at": None, "limit": 20
                    }))
                else:
                    await req.fulfill(content_type="application/javascript", body="")

            await page.route("**/*", route)
            await page.goto("https://example.test/app")
            await page.locator("nav [data-tab=account]").click()
            assert await page.locator("#install-shortcut").is_visible()
            assert await page.locator("#install-shortcut").is_disabled()
            assert "Telegram версии 8.0+" in await page.locator("#install-shortcut-status").inner_text()
            assert not errors, errors
            await browser.close()

    asyncio.run(run())
