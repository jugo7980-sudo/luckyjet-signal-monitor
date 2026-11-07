"""v0.8 responsive spacing and whole-site compact behavior."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from mini_app import response


def test_v08_guest_layout_320_390_430_and_compact_tabs():
    from playwright.async_api import async_playwright

    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            for width in (320, 390, 430):
                page = await browser.new_page(viewport={"width": width, "height": 844}, device_scale_factor=2)
                errors = []
                page.on("pageerror", lambda err: errors.append(str(err)))

                async def route(req):
                    url = req.request.url
                    if url.startswith("https://example.test/app"):
                        await req.fulfill(content_type="text/html", body=response("/app")[2])
                    elif url.endswith(("/history.js", "/controls.js", "/admin.js", "/site.js", "/access.js")):
                        await req.fulfill(content_type="application/javascript",
                                          body=response("/" + url.split("/")[-1])[2])
                    elif "/api/access" in url:
                        await req.fulfill(content_type="application/json", body=json.dumps({
                            "verified": False, "registered": False, "premium": False,
                            "username": None, "balance_cents": None,
                        }))
                    elif "/api/public" in url:
                        from urllib.parse import urlparse, parse_qs
                        limit = int(parse_qs(urlparse(url).query).get("limit", ["20"])[0])
                        await req.fulfill(content_type="application/json", body=json.dumps({
                            "coefficients": ["1.25x", "3.50x", "11.00x", "112.32x"] * (limit // 4),
                            "last_round_at": None, "limit": limit,
                        }))
                    elif "/api/overview" in url or "/api/history" in url or "/api/controls" in url:
                        raise AssertionError("Guest must never request private API")
                    else:
                        await req.fulfill(content_type="application/javascript",
                            body="window.Telegram={WebApp:{initData:'',ready(){},expand(){},setHeaderColor(){},setBackgroundColor(){},close(){},BackButton:{onClick(){},hide(){},show(){}},HapticFeedback:{selectionChanged(){}}}}")

                await page.route("**/*", route)
                await page.goto("https://example.test/app")
                await page.wait_for_function("document.querySelectorAll('#coef-grid .coef').length === 20")
                layout = await page.evaluate("""() => {
                    const grid=document.querySelector('#coef-grid');
                    const tiles=[...grid.children].map(e=>e.getBoundingClientRect());
                    const refresh=document.querySelector('#refresh-data').getBoundingClientRect();
                    const header=document.querySelector('header.topbar');
                    const gridBox=grid.getBoundingClientRect();
                    return {
                        columns:getComputedStyle(grid).gridTemplateColumns.split(' ').length,
                        sameRow: tiles.slice(0,5).every(x=>Math.abs(x.top-tiles[0].top)<1),
                        nextRow:tiles[5].top > tiles[0].top+5,
                        completeRows:tiles.length===20 && tiles.slice(15).every(x=>Math.abs(x.top-tiles[15].top)<1),
                        refreshWidth:refresh.width,gridWidth:gridBox.width,refreshHeight:refresh.height,
                        refreshLabel:document.querySelector('#refresh-data .refresh-caption').textContent,
                        headerPosition:getComputedStyle(header).position,
                        headerHeight:header.getBoundingClientRect().height,
                        pageWidth:document.documentElement.scrollWidth,
                        viewport:window.innerWidth,
                        tileHeight:tiles[0].height,
                    };
                }""")
                assert layout["columns"] == 5, layout
                assert layout["sameRow"] and layout["nextRow"] and layout["completeRows"], layout
                assert abs(layout["refreshWidth"] - layout["gridWidth"]) <= 2, layout
                assert layout["refreshHeight"] >= 44, layout
                assert layout["refreshLabel"] == "Обновить"
                assert layout["headerPosition"] == "sticky"
                assert layout["headerHeight"] <= 70, layout
                assert layout["tileHeight"] <= 38, layout
                assert layout["pageWidth"] <= layout["viewport"], layout
                assert await page.locator(".below-tiles .refresh-compact").is_visible()

                for tab in ("demo", "history", "account"):
                    await page.locator(f'nav [data-tab="{tab}"]').click()
                    assert await page.locator(f"#page-{tab} .locked-section").is_visible()
                    dimensions = await page.evaluate("""() => ({
                        width:document.documentElement.scrollWidth,
                        viewport:innerWidth,
                        sticky:getComputedStyle(document.querySelector('header.topbar')).position,
                        lockPadding:getComputedStyle(document.querySelector('.tab-page:not([hidden]) .locked-section')).paddingTop,
                        navMinHeight:getComputedStyle(document.querySelector('nav button')).minHeight,
                    })""")
                    assert dimensions["width"] <= dimensions["viewport"], (tab, dimensions)
                    assert dimensions["sticky"] == "sticky"
                    assert float(dimensions["lockPadding"].replace("px", "")) <= 20
                    assert float(dimensions["navMinHeight"].replace("px", "")) <= 54
                await page.locator('nav [data-tab="home"]').click()
                for limit in (30, 50, 100):
                    # Wait for the previous fetch to finish before firing another change event.
                    # The UI deliberately ignores limit changes while a request is in flight.
                    await page.wait_for_function("!document.querySelector('#refresh-data').disabled")
                    await page.locator("#recent-limit").select_option(str(limit))
                    await page.wait_for_function(f"document.querySelectorAll('#coef-grid .coef').length === {limit}")
                    grid_state = await page.evaluate("""() => {
                        const grid = document.querySelector('#coef-grid');
                        const tiles = [...grid.children].map(e => e.getBoundingClientRect());
                        return {
                            columns: getComputedStyle(grid).gridTemplateColumns.split(' ').length,
                            rows: new Set(tiles.map(e => Math.round(e.top))).size,
                            count: tiles.length,
                            fullLastRow: tiles.slice(-5).every(e => Math.abs(e.top - tiles[tiles.length-1].top) < 1),
                            noOverflow: document.documentElement.scrollWidth <= innerWidth
                        };
                    }""")
                    assert grid_state == {
                        "columns": 5, "rows": limit // 5, "count": limit,
                        "fullLastRow": True, "noOverflow": True
                    }, grid_state
                assert not errors, errors
                await page.close()
            await browser.close()
    asyncio.run(run())


def test_v08_premium_styles_compact_history_demo_settings_admin():
    from playwright.async_api import async_playwright

    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            async def route(req):
                url=req.request.url
                if url.startswith("https://example.test/app"):
                    await req.fulfill(content_type="text/html", body=response("/app")[2])
                elif url.endswith("/access.js"):
                    await req.fulfill(content_type="application/javascript",
                        body="window.accessState={premium:true,registered:true};document.body.dataset.access='premium';window.canUsePremium=()=>true")
                elif url.endswith(("/history.js","/controls.js","/admin.js","/site.js")):
                    await req.fulfill(content_type="application/javascript", body=response("/"+url.split("/")[-1])[2])
                elif "/api/overview" in url:
                    await req.fulfill(content_type="application/json", body=json.dumps({
                        "coefficients": ["1.20x"]*20,"limit":20,"last_round_at":None,
                        "summary": {"total":20,"average":1.2,"maximum":1.2,"below_two":20,"streak":20},
                        "status":{"age_seconds":1,"fresh":True,"last":"1.20x","threshold":4,"streak_count":20,"uptime_seconds":123},
                        "demo":{"balance_cents":1000000,"enabled":False,"active":False,"step":None,"base_stake_cents":1000,"trigger":4},
                    }))
                elif "/api/controls" in url:
                    await req.fulfill(content_type="application/json", body=json.dumps({
                        "admin":False,"demo":{"enabled":False,"active":False,"balance_cents":1000000,
                            "base_stake_cents":1000,"trigger":4,"low_streak":0},
                        "settings":{"notifications":True,"sound":True,"threshold":4,
                            "history_size":20,"delete_delay":10},
                    }))
                elif "/api/history" in url:
                    await req.fulfill(content_type="application/json", body='{"view":"days","days":[],"total":0,"page":0}')
                elif "/api/admin" in url or "/api/site" in url:
                    await req.fulfill(status=403,content_type="application/json",body='{"error":"Forbidden"}')
                else:
                    await req.fulfill(content_type="application/javascript",
                        body="window.Telegram={WebApp:{initData:'fixture',ready(){},expand(){},setHeaderColor(){},setBackgroundColor(){},close(){},BackButton:{onClick(){},hide(){},show(){}},HapticFeedback:{selectionChanged(){}}}}")
            await page.route("**/*",route)
            await page.goto("https://example.test/app")
            await page.locator("#coef-grid .coef").first.wait_for()
            await page.locator('nav [data-tab="demo"]').click()
            await page.locator("#demo-controls").wait_for(state="visible")
            assert await page.evaluate("parseFloat(getComputedStyle(document.querySelector('.control-row')).paddingTop)") <= 8
            assert await page.evaluate("parseFloat(getComputedStyle(document.querySelector('.live-panel')).paddingTop)") <= 12
            await page.locator('nav [data-tab="history"]').click()
            await page.locator("#history-refresh").wait_for(state="visible")
            assert await page.evaluate("document.querySelector('#history-refresh').getBoundingClientRect().height") >= 42
            await page.locator('nav [data-tab="account"]').click()
            await page.locator("#open-settings").wait_for(state="visible")
            assert await page.evaluate("document.querySelector('#open-settings').getBoundingClientRect().height") <= 58
            assert await page.evaluate("getComputedStyle(document.querySelector('header.topbar')).position") == "sticky"
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert not errors, errors
            await browser.close()
    asyncio.run(run())
