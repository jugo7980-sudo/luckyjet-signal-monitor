"""End-to-end visual navigation and plain-language explanations on all four tabs."""
import asyncio
import json
from urllib.parse import parse_qs, urlparse

from mini_app import APP_VERSION, response


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
                    elif url.split('?', 1)[0].endswith(("/history.js", "/summary.js", "/controls.js")):
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
                                          body=json.dumps({"view": view, "days": ["2026-11-08"],
                                                           "items": [], "total": 1, "page": 0}))
                    else:
                        await req.fulfill(content_type="application/javascript",
                            body="window.Telegram={WebApp:{initData:'fixture',ready(){},"
                                 "expand(){},setHeaderColor(){},setBackgroundColor(){},"
                                 "close(){},BackButton:{onClick(){},hide(){},show(){}},"
                                 "HapticFeedback:{selectionChanged(){}}}}")
                await page.route("**/*", route)
                await page.goto("https://example.test/app")
                await page.locator('#summary-accordion').wait_for(state='visible')
                assert not await page.locator("#summary-accordion").evaluate("(n) => n.open")
                assert not await page.locator("#monitoring-accordion").evaluate("(n) => n.open")
                assert await page.locator("#summary-preview-placeholder").is_visible()
                assert await page.locator("#summary-preview-results").is_hidden()
                assert await page.locator(".recent-results-card #coef-grid").count() == 1
                await page.locator("#summary-accordion summary").click()
                await page.wait_for_function(
                    "document.querySelector('#ind-found').textContent === '1'")
                assert "Нужно хотя бы 2 совпадения" in await page.locator(
                    "#ind-average-interval-hint").inner_text()
                assert "150 сохранённых раундов" in await page.locator(
                    "#ind-summary-context").inner_text()
                assert await page.locator("#ind-summary-cards").count() == 0
                assert await page.locator(".ind-summary-cards .ind-stat").count() == 12
                assert await page.locator("#summary-help-toggle").count() == 1
                assert await page.locator("#summary-preview-placeholder").is_hidden()
                # Preview is purposefully invisible while <details> is open; its
                # values are ready for the compact header once the user closes it.
                assert await page.locator("#summary-preview-results").evaluate("(n) => !n.hidden")
                assert await page.locator(".ind-summary-cards").first.locator(".ind-stat:visible").count() == 4
                assert await page.locator("#summary-extra-stats").is_hidden()
                await page.locator("#summary-stats-toggle").click()
                assert await page.locator("#summary-extra-stats .ind-stat:visible").count() == 8
                assert await page.locator("#summary-stats-toggle").get_attribute("aria-expanded") == "true"
                await page.locator("#summary-stats-toggle").click()
                assert await page.locator("#summary-extra-stats").is_hidden()
                assert await page.locator("#summary-help-body").is_hidden()
                await page.locator("#summary-help-toggle").click()
                assert await page.locator("#summary-help-body").is_visible()
                assert await page.locator("#summary-explainer").evaluate(
                    "(n) => n.closest('#summary-accordion') !== null")
                await page.locator("#summary-help-close").click()
                assert await page.locator("#summary-help-body").is_hidden()
                # All expanded help is separated from the main page into compact ? dialogs.
                help_target = page.locator('[data-summary-help-for="ind-target-hint"]')
                assert await help_target.evaluate("(n) => n.getBoundingClientRect().width >= 44")
                await help_target.click()
                assert "100x и выше" in await page.locator("#summary-metric-text").inner_text()
                await page.keyboard.press("Tab")
                assert await page.locator("#summary-metric-close").evaluate("(n) => document.activeElement === n")
                await page.keyboard.press("Escape")
                assert await page.locator("#summary-metric-popup").is_hidden()
                assert await help_target.evaluate("(n) => document.activeElement === n")
                assert await page.locator("#summary-metric-popup").is_hidden()
                await page.locator('[data-summary-help-for="ind-summary-status-detail"]').click()
                assert "истории самого игрового сайта" in await page.locator(
                    "#summary-metric-text").inner_text()
                await page.locator("#summary-metric-close").click()
                # Changing loading text must never push the card grid down.
                before = await page.locator(".ind-summary-cards .ind-stat").first.bounding_box()
                async with page.expect_response(lambda res: "/api/round-summary" in res.url):
                    await page.locator("#ind-summary-apply").click()
                after = await page.locator(".ind-summary-cards .ind-stat").first.bounding_box()
                assert abs(before["y"] - after["y"]) <= 1
                assert abs(before["height"] - after["height"]) <= 1
                assert await page.locator("#ind-summary-status").evaluate(
                    "(n) => n.getBoundingClientRect().height <= 20")
                await page.locator("#summary-accordion summary").click()
                assert await page.locator("#summary-preview-results").is_visible()
                await page.locator("#monitoring-accordion summary").click()
                assert await page.locator("#monitoring-accordion").evaluate("(n) => n.open")
                assert await page.locator("#monitor-state").count() == 1
                assert await page.locator("#monitor-led").get_attribute("data-state") == "good"
                assert await page.locator("#monitor-header-age").inner_text() == "2 с"
                await page.locator('[data-summary-help-for="monitor-help-text"]').click()
                assert "не гарантирует полноту" in await page.locator("#summary-metric-text").inner_text()
                await page.locator("#summary-metric-close").click()
                assert await page.locator("#home-status-row").evaluate(
                    "(n) => n.getBoundingClientRect().height >= 44")
                assert await page.locator(".topbar .brand").count() == 0
                assert await page.locator(".topbar .app-context-label").inner_text() == "DEMO"
                assert await page.locator("#header-premium").inner_text() == "Premium"
                assert await page.locator(".below-tiles #refresh-data").evaluate(
                    "(n) => n.getBoundingClientRect().width <= 150")
                assert not await page.locator("#page-home .home-disclaimer").is_visible()
                assert await page.locator("#home-status-row").evaluate(
                    "(n) => n.closest('.home-freshness-row') !== null")
                assert await page.locator("#coef-grid .coef").first.evaluate(
                    "(n) => parseFloat(getComputedStyle(n).fontSize) >= 11")
                await page.locator("#monitor-uptime").scroll_into_view_if_needed()
                await page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
                assert await page.evaluate("""() => {
                    const footer = document.querySelector('nav').getBoundingClientRect();
                    const last = document.querySelector('#live-status').getBoundingClientRect();
                    return last.bottom < footer.top - 6;
                }""")
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth")

                await page.locator('nav [data-tab="demo"]').click()
                assert not await page.locator("#demo-classic-accordion").evaluate("(n) => n.open")
                assert await page.locator("#demo-metrics-disclosure").evaluate("(n) => !n.open")
                await page.locator("#demo-classic-accordion summary").click()
                await page.locator("#demo-controls").wait_for(state="visible")
                assert await page.locator("#page-demo .control-row label button").count() == 0
                assert await page.locator("#page-demo .demo-balance-card").count() == 1
                assert await page.locator("#demo-classic-accordion").evaluate(
                    "(n) => n.parentElement.id === 'live-demo'")
                assert await page.locator("#demo-management").evaluate(
                    "(n) => n.parentElement.classList.contains('demo-secondary-controls')")
                secondary = await page.evaluate("""() => {
                    const group = document.querySelector('#page-demo .demo-secondary-controls');
                    const a = document.getElementById('demo-management').getBoundingClientRect();
                    const b = document.getElementById('demo-metrics-disclosure').getBoundingClientRect();
                    const first = document.querySelector('#demo-management > summary').getBoundingClientRect();
                    const second = document.querySelector('#demo-metrics-disclosure > summary').getBoundingClientRect();
                    return {layout:getComputedStyle(group).display,
                      sameLeft:Math.abs(a.left-b.left)<=1,
                      sameWidth:Math.abs(a.width-b.width)<=1,
                      sameHeight:Math.abs(first.height-second.height)<=1,
                      stacked:b.top>=a.bottom-1,
                      targetHeight:Math.min(first.height,second.height),
                      short:Math.max(a.height,b.height)<=55,
                      groupWithinViewport:group.getBoundingClientRect().right<=innerWidth};
                }""")
                assert secondary["layout"] == "flex", secondary
                assert all(secondary[k] for k in ("sameLeft","sameWidth","sameHeight",
                              "stacked","short","groupWithinViewport")), secondary
                assert secondary["targetHeight"] >= 44, secondary
                assert await page.locator("#demo-status-row").count() == 0
                assert await page.locator("#page-demo .demo-status-row").is_hidden()
                assert "серия" in (await page.locator("#demo-classic-accordion").inner_text()).lower()
                assert await page.locator("#demo-quality").inner_text() == "По архиву"
                await page.locator('[data-summary-help-for="demo-quality-help"]').click()
                assert "Полнота" in await page.locator("#summary-metric-text").inner_text()
                await page.locator("#summary-metric-close").click()
                assert not await page.locator("#demo-management").evaluate("(n) => n.open")
                assert "виртуальным балансом" in await page.locator(
                    "#page-demo .screen-subtitle").text_content()
                assert await page.locator("#page-demo .screen-subtitle").is_hidden()
                assert await page.locator("#high-gap-panel").is_hidden()
                await page.locator("#demo-high-accordion summary").click()
                await page.locator("#high-gap-panel").wait_for(state="visible")
                await page.locator("#high-gap-help-toggle").click()
                assert "Без догона" in await page.locator(
                    "#high-gap-help-body").inner_text()
                await page.locator('[data-summary-help-for="high-gap-reference-hint"]').click()
                assert "строго больше" in await page.locator("#summary-metric-text").inner_text()
                await page.locator("#summary-metric-close").click()
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth")

                await page.locator('nav [data-tab="history"]').click()
                await page.locator("#history-content .history-date-search").wait_for(state="attached")
                assert not await page.locator("#history-content .history-date-search").evaluate("(n) => n.open")
                assert await page.locator("#history-content .history-item").count() >= 1
                assert await page.locator("#history-date").is_hidden()
                await page.locator("#history-content .history-date-search summary").click()
                await page.locator("#history-date").wait_for(state="visible")
                assert await page.locator("#history-date").input_value() != ""
                assert await page.locator("#history-content .history-date-row label").inner_text() == "Дата"
                assert await page.locator("#history-content .history-heading").count() == 0
                assert await page.locator("#history-pager").is_hidden()
                assert await page.locator("#history-date").evaluate(
                    "(n) => n.getBoundingClientRect().width >= 105")
                assert await page.evaluate("""() => {
                    const refresh = document.querySelector('#history-refresh').getBoundingClientRect();
                    const search = document.querySelector('.history-date-search').getBoundingClientRect();
                    return search.top - refresh.bottom < 65 && search.top >= refresh.top;
                }""")
                assert not await page.locator("#history-management").evaluate("(n) => n.open")
                assert await page.locator("#history-reset-scope").is_hidden()
                await page.wait_for_function(
                    "document.body.dataset.access === 'premium' && !document.getElementById('page-history').hidden")
                guide_trigger = page.locator("#history-guide > summary")
                await guide_trigger.wait_for(state="visible")
                assert await guide_trigger.evaluate(
                    "(n) => parseFloat(getComputedStyle(n).minHeight) >= 44")
                await guide_trigger.click()
                await page.locator("#history-explainer").wait_for(state="visible")
                await page.locator('[data-history-view="days"]').focus()
                await page.keyboard.press("ArrowRight")
                assert await page.locator('[data-history-view="high_days"]').get_attribute("aria-selected") == "true"
                await page.locator('[data-history-view="cycles"]').click()
                await page.wait_for_function(
                    "document.querySelector('#history-explainer').textContent.includes('DEMO-циклы')")
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth")
                await page.locator("#history-management summary").click()
                assert await page.locator("#history-reset-scope").is_visible()

                await page.locator('nav [data-tab="account"]').click()
                await page.locator("#open-settings").click()
                await page.locator("#settings-panel").wait_for(state="visible")
                assert await page.locator("#account-username").count() == 1
                assert await page.locator("#account-premium-status").is_hidden()
                assert await page.locator("#page-account .control-row label button").count() == 0
                assert await page.locator("#site-connection-panel").count() == 1
                assert await page.locator("#app-release-notes").count() == 1
                assert APP_VERSION in await page.locator("#app-release-notes").inner_text()
                await page.locator('[data-summary-help-for="settings-notifications-hint"]').click()
                assert "серии ниже 2x" in await page.locator("#summary-metric-text").inner_text()
                await page.locator("#summary-metric-close").click()
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth")
                assert not errors, errors
                await page.close()
            await browser.close()

    asyncio.run(run())
