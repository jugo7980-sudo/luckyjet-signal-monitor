"""Browser regression: a finger move must reach the server BEFORE release."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from mini_app import response


def test_owner_drag_delivers_live_pointer_events_before_finger_release():
    from playwright.async_api import async_playwright

    html = """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head>
    <body data-access="premium">
      <div id="page-account">
        <section id="site-section">
          <p id="site-status-text"></p><p id="site-message"></p>
          <form id="site-login-form"><input id="site-email"><input id="site-password"></form>
          <form id="site-code-form"><input id="site-code"></form>
          <button id="site-cancel"></button><button id="site-forget"></button>
          <button id="site-refresh"></button>
        </section>
      </div>
      <script>
        window.Telegram={WebApp:{initData:"fixture"}};
        window.canUsePremium=()=>true;
      </script>
      <script src="/site.js"></script>
    </body></html>"""

    async def run():
        sent = []
        errors = []
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 430, "height": 850})
            page.on("pageerror", lambda exc: errors.append(str(exc)))

            async def route(req):
                url = req.request.url
                if url.endswith("/site.js"):
                    await req.fulfill(content_type="application/javascript", body=response("/site.js")[2])
                elif "/api/browser" in url:
                    if req.request.method == "POST":
                        sent.append(req.request.post_data_json)
                        await req.fulfill(content_type="application/json",
                            body=json.dumps({"active": True, "message": "ok"}))
                    else:
                        await req.fulfill(content_type="application/json",
                            body=json.dumps({"active": True, "image": "",
                                             "seconds_remaining": 480}))
                elif "/api/site" in url:
                    await req.fulfill(content_type="application/json",
                        body=json.dumps({"phase": "disconnected", "saved_session": False}))
                else:
                    await req.fulfill(content_type="text/html", body=html)

            await page.route("**/*", route)
            await page.goto("https://example.test/index")
            await page.locator("#remote-browser-overlay").wait_for(state="hidden")
            await page.locator("#site-section > button").last.click()
            await page.locator("#remote-browser-overlay").wait_for(state="visible")
            await page.locator("#remote-drag-mode").click()
            assert await page.locator("#remote-drag-mode").get_attribute("aria-pressed") == "true"

            image = page.locator("#remote-browser-image")
            await image.evaluate("""e => {
                const svg='<svg xmlns="http://www.w3.org/2000/svg" width="430" height="932"><rect width="430" height="932" fill="white"/></svg>';
                e.src='data:image/svg+xml;base64,'+btoa(svg);
            }""")
            await image.scroll_into_view_if_needed()
            await page.wait_for_function("document.querySelector('#remote-browser-image').complete")
            rect = await image.bounding_box()
            assert rect, "Remote screen must be visible"
            x = rect["x"] + rect["width"] * .15
            y = max(5, min(800, rect["y"] + 60))
            await page.mouse.move(x, y)
            await page.mouse.down()
            for _ in range(50):
                await asyncio.sleep(.02)
                if any(e.get("phase") == "down" for e in sent):
                    break
            assert any(e.get("phase") == "down" for e in sent), sent
            await page.mouse.move(x + rect["width"] * .35, y, steps=6)
            for _ in range(50):
                await asyncio.sleep(.02)
                if any(e.get("phase") == "move" for e in sent):
                    break
            # This must happen BEFORE page.mouse.up(), the old implementation failed here.
            assert any(e.get("phase") == "move" for e in sent), sent
            assert not any(e.get("phase") == "up" for e in sent)
            await page.mouse.up()
            for _ in range(50):
                await asyncio.sleep(.02)
                if any(e.get("phase") == "up" for e in sent):
                    break
            phases = [e.get("phase") for e in sent if e.get("action") == "pointer"]
            assert phases[0] == "down" and "move" in phases[1:-1] and phases[-1] == "up", sent
            assert not any(e.get("action") == "drag" for e in sent)
            assert not errors, errors
            await browser.close()

    asyncio.run(run())
