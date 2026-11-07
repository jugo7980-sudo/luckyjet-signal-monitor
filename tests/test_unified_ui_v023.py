"""End-to-end visual navigation and plain-language explanations on all four tabs."""
import asyncio
import json
from urllib.parse import parse_qs, urlparse

from mini_app import response


def test_mobile_full_site_design_navigation_and_explanations():
    from playwright.async_api import async_playwright

    async def run():
        async with async_playwright() as browser_tool:
            browser = await browser_tool.chromium.launch(headless=True)
            for width in (390, 320):
                page = await browser.new_page(viewport={"width": width, "height": 800})
                errors = []
                page.on("pageerror", lambda err: errors.append(str(err)))
                high = {"enabled": False, "phase": "waiting_reference",
                        "reference": "100", "target": "100", "wait_rounds": 100,
                        "stake_cents": 1000, "elapsed": 0, "remaining": None,
                        "reference_value": "", "steps": 0, "total_stake_cents": 0}
                demo = {"currency": "KRW", "reset_revision": "test", "enabled": False,
                        "active": False, "step": None, "balance_cents": 1000000,
                        "balance_verified": True, "base_stake_cents": 1000,
                        "trigger": 4, "low_streak": 0, "high_gap": high}
                controls = {"admin": False, "demo": demo, "high_gap": high,
                            "settings": {"notifications": True, "sound": True,
                                         "threshold": 4, "history_size": 20,
                                         "delete_delay": 10}}
                async def route(req):
                    url = req.request.url
                    if url.startswith("https://example.test/app"):
                        await req.fulfill(content_type="text/html", body=response("/app")[2])
                    elif url.endswith("/access.js"):
                        await req.fulfill(content_type="application/javascript", body=(
                            "window.accessState={premium:true,registered:true,username:'tester'};"
                            "document.body.dataset.access='premium';"
                            "window.canUsePremium=()=>true;"
                            "window.refreshAccessOverview?.();"
                        ))
                    elif url.endswith(("/history.js", "/summary.js", "/controls.js")):
                        await req.fulfill(content_type="application/javascript",
                                          body=response("/" + url.split("/")[-1])[2])
                    elif url.endswith(("/admin.js", "/site.js")):
                        await req.fulfill(content_type="application/javascript", body="")
                    elif "/api/round-summary" in url:
                        query = parse_qs(urlparse(url).query)
                        count = int(query["rounds"][0])
                        await req.fulfill(content_type="application/json", body=json.dumps({
                            "rounds_requested": count, "rounds_available": count,
                            "target": float(query["target"][0]),
                            "since_threshold": float(query["since"][0]),
                            "current": {"count": 1, "longest_wait": 13,
                                        "average_interval": None,
                                        "minimum_interval": None,
                                        "maximum_interval": None, "intervals_count": 0,
                                        "average": 11.26, "maximum": 189.0, "below_two": 14},
                            "previous": {"count": 0},
                            "rounds_since_target": 6,
                            "rounds_since_since": 6,
                            "matches": [{"round_id": "1", "coefficient": 189.0,
                                         "observed_at": "1794000000000",
                                         "source_time": None, "rounds_ago": 6,
                                         "interval_to_previous": None}],
                            "match_rows_limited": False,
                            "quality": {"complete_window": True, "complete_comparison": True,
                                        "window_has_discontinuities": False,
                                        "comparison_has_discontinuities": False,
                                        "duplicate_entries": 0,
                                        "unverified_source_timing": True},
                        }))
                    elif "/api/overview" in url:
                        await req.fulfill(content_type="application/json", body=json.dumps({
                            "coefficients": ["1.50x", "12.00x"], "last_round_at": 1794000000,
                            "limit": 20, "demo": demo,
                            "status": {"age_seconds": 2, "fresh": True,
                                       "streak_count": 0, "threshold": 4,
                                       "last": "1.50x", "uptime_seconds": 120},
                        }))
                    elif "/api/controls" in url:
                        await req.fulfill(content_type="application/json", body=json.dumps(controls))
                    elif "/api/history" in url:
                        view = parse_qs(urlparse(url).query).get("view", ["days"])[0]
                        await req.fulfill(content_type="application/json",
                                          body=json.dumps({"view": view, "days": [],
                                                           "items": [], "total": 0, "page": 0}))
                    else:
                        await req.fulfill(content_type="application/javascript",
                            body="window.Telegram={WebApp:{initData:'fixture',ready(){},"
                                 "expand(){},setHeaderColor(){},setBackgroundColor(){},"
                                 "close(){},BackButton:{onClick(){},hide(){},show(){}},"
                                 "HapticFeedback:{selectionChanged(){}}}}")
                await page.route("**/*", route)
                await page.goto("https://example.test/app")
                await page.locator("#summary-accordion summary").click()
                await page.wait_for_function(
                    "document.querySelector('#ind-found').textContent === '1'")
                assert "Нужно хотя бы 2 совпадения" in await page.locator(
                    "#ind-average-interval-hint").inner_text()
                assert "150 сохранённых раундов" in await page.locator(
                    "#ind-summary-context").inner_text()
                assert await page.locator("#ind-summary-cards").count() == 0
                assert await page.locator(".ind-summary-cards .ind-stat").count() == 12
                assert await page.locator("#summary-explainer summary").count() == 1
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth")

                await page.locator('nav [data-tab="demo"]').click()
                await page.locator("#demo-controls").wait_for(state="visible")
                assert "виртуальным балансом" in await page.locator(
                    "#page-demo .screen-subtitle").inner_text()
                assert await page.locator("#high-gap-panel").is_hidden()
                await page.locator("#demo-high-accordion summary").click()
                await page.locator("#high-gap-panel").wait_for(state="visible")
                assert "Без догона" in await page.locator(
                    "#high-gap-panel > .field-help").inner_text()
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth")

                await page.locator('nav [data-tab="history"]').click()
                await page.locator("#history-explainer").wait_for(state="visible")
                await page.locator('[data-history-view="cycles"]').click()
                await page.wait_for_function(
                    "document.querySelector('#history-explainer').textContent.includes('DEMO-циклы')")
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth")

                await page.locator('nav [data-tab="account"]').click()
                await page.locator("#open-settings").click()
                await page.locator("#settings-panel").wait_for(state="visible")
                assert await page.locator("#app-release-notes").count() == 1
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth")
                assert not errors, errors
                await page.close()
            await browser.close()

    asyncio.run(run())
