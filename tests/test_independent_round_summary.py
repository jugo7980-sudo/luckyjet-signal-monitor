"""Regression tests: archive summary is immutable and independent of DEMO."""
import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import fakeredis.aioredis

from independent_round_summary import _params, analyze, summary
from round_archive import STREAM_KEY


def run(coro):
    return asyncio.run(coro)


async def seed(redis, coefficients, *, gap_at=None):
    """Chronological insertion, stream newest first when analyzed."""
    for i, value in enumerate(coefficients):
        if gap_at is not None and i == gap_at:
            await redis.xadd(STREAM_KEY, {"type": "gap", "previous_round_id": "unknown"})
        await redis.xadd(STREAM_KEY, {
            "type": "round", "round_id": "r" + str(i),
            "coefficient": str(value), "observed_at": str(1794000000000 + i),
            "source_time": "2026-11-08T00:00:00+10:00",
        })


def test_read_only_counts_interval_and_distinct_thresholds():
    async def case():
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        # Newest five: [1.1, 300, 1.5, 160, 2].
        await seed(redis, [2, 100, 150, 2, 2, 2, 160, 1.5, 300, 1.1])
        before = await redis.xlen(STREAM_KEY)
        result = await analyze(redis, 5, 150, 100)
        assert await redis.xlen(STREAM_KEY) == before
        assert result["current"]["count"] == 2
        assert result["rounds_since_target"] == 1
        assert result["rounds_since_since"] == 1
        assert result["current"]["minimum_interval"] == 2
        assert result["current"]["average_interval"] == 2
        assert result["current"]["longest_wait"] == 1
        assert result["current"]["maximum"] == 300
        assert result["previous"]["count"] == 1
        assert result["quality"]["complete_window"] is True
        assert result["quality"]["complete_comparison"] is True
        assert result["matches"][0]["coefficient"] == 300
        assert result["matches"][1]["interval_to_previous"] is None
        assert result["source"] == "append_only_round_archive"
    run(case())


def test_marker_masks_unsafe_distance_without_guessing():
    async def case():
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await seed(redis, [160, 1.2, 1.1, 200], gap_at=2)
        result = await analyze(redis, 4, 150, 150)
        assert result["current"]["count"] == 2
        assert result["quality"]["window_has_discontinuities"]
        assert result["rounds_since_target"] == 0
        assert result["current"]["minimum_interval"] is None
        assert result["matches"][0]["interval_to_previous"] is None
    run(case())


def test_empty_and_incomplete_are_not_reported_as_verified():
    async def case():
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await seed(redis, [1.1, 1.2, 1.3])
        result = await analyze(redis, 150, 150, 100)
        assert result["current"]["count"] == 0
        assert result["rounds_available"] == 3
        assert result["rounds_since_target"] is None
        assert result["rounds_since_since"] is None
        assert not result["quality"]["complete_window"]
        assert not result["quality"]["verified_game_history"]
        assert result["quality"]["archive_exhausted"]
    run(case())


def test_duplicate_source_ids_not_counted_twice():
    async def case():
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await seed(redis, [200, 1.1, 300])
        await redis.xadd(STREAM_KEY, {
            "type": "round", "round_id": "r2", "coefficient": "999",
            "observed_at": "1794000000000",
        })
        result = await analyze(redis, 3, 150, 100)
        assert result["current"]["count"] == 2
        assert result["quality"]["duplicate_entries"] == 1
        assert result["quality"]["window_has_discontinuities"]
    run(case())


def test_query_validation_and_premium_gate():
    assert _params("rounds=150&target=150&since=100") == (150, 150, 100)
    for bad in [
        "rounds=0&target=150&since=100",
        "rounds=150&target=NaN&since=100",
        "rounds=50001&target=150&since=100",
        "rounds=1.5&target=150&since=100",
        "rounds=150&target=150&since=100&reset=1",
    ]:
        try:
            _params(bad)
        except ValueError:
            pass
        else:
            raise AssertionError("Must reject: " + bad)

    class Gate:
        redis = None
        blocked = set()
        async def is_authorized(self, user):
            return user == 9
        def is_admin(self, user):
            return False

    with patch("independent_round_summary.telegram_user", return_value=9):
        status, _type, body = run(summary("tma signed", Gate(), "token",
            "rounds=150&target=150&since=100"))
        assert status.startswith("503")
        assert "архив" in json.loads(body)["error"].lower()


def test_frontend_not_connected_to_demo_or_live_tile_limit():
    html = (Path(__file__).parents[1] / "web" / "mini-app.html").read_text()
    script = (Path(__file__).parents[1] / "web" / "summary.js").read_text()
    backend = (Path(__file__).parents[1] / "independent_round_summary.py").read_text()
    assert 'src="/summary.js"' in html
    assert 'id="ind-rounds"' in html
    assert 'id="ind-target"' in html
    assert 'id="ind-since"' in html
    assert "if(premium&&data.summary){" not in html
    assert "/api/round-summary" in script
    assert "demo_ledgers" not in backend and "history_provider" not in backend
    assert "chosenLimit" not in script


def test_browser_summary_independent_from_recent_tiles_and_demo():
    from playwright.async_api import async_playwright
    from urllib.parse import parse_qs, urlparse
    from mini_app import response

    async def case():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            errors, requests = [], []
            page.on("pageerror", lambda err: errors.append(str(err)))

            async def intercept(route):
                url = route.request.url
                if url.startswith("https://example.test/app"):
                    await route.fulfill(content_type="text/html", body=response("/app")[2])
                elif url.endswith("/access.js"):
                    await route.fulfill(content_type="application/javascript", body=(
                        "window.accessState={premium:true,registered:true};"
                        "document.body.dataset.access='premium';"
                        "window.canUsePremium=()=>true;"
                        "window.refreshAccessOverview?.();"
                    ))
                elif url.endswith("/summary.js"):
                    await route.fulfill(content_type="application/javascript",
                                        body=response("/summary.js")[2])
                elif url.endswith(("/history.js", "/controls.js", "/admin.js", "/site.js")):
                    await route.fulfill(content_type="application/javascript", body="")
                elif "/api/round-summary" in url:
                    requests.append(parse_qs(urlparse(url).query))
                    count = int(requests[-1]["rounds"][0])
                    await route.fulfill(content_type="application/json", body=json.dumps({
                        "rounds_requested": count, "rounds_available": count,
                        "target": float(requests[-1]["target"][0]),
                        "since_threshold": float(requests[-1]["since"][0]),
                        "current": {
                            "count": 3, "longest_wait": 47, "average_interval": 59,
                            "minimum_interval": 21, "maximum_interval": 97,
                            "average": 5.5, "maximum": 305.54, "below_two": 83,
                        },
                        "previous": {"count": 2}, "rounds_since_target": 47,
                        "rounds_since_since": 47, "matches": [{
                            "round_id": "r1", "coefficient": 305.54,
                            "observed_at": "1794000000000", "source_time": None,
                            "rounds_ago": 47, "interval_to_previous": 21,
                        }], "match_rows_limited": False,
                        "quality": {
                            "complete_window": True, "complete_comparison": True,
                            "window_has_discontinuities": False,
                            "comparison_has_discontinuities": False,
                            "duplicate_entries": 0, "unverified_source_timing": True,
                        },
                    }))
                elif "/api/overview" in url or "/api/public" in url:
                    await route.fulfill(content_type="application/json", body=json.dumps({
                        "coefficients": ["1.50x", "3.00x"], "limit": 20,
                        "last_round_at": 1794000000,
                        "status": {"age_seconds": 1, "fresh": True,
                                   "uptime_seconds": 123, "threshold": 3, "streak_count": 1},
                        "demo": {},
                    }))
                else:
                    await route.fulfill(content_type="application/javascript",
                                        body="window.Telegram={WebApp:{initData:'fixture',ready(){},expand(){},setHeaderColor(){},setBackgroundColor(){}}};")

            await page.route("**/*", intercept)
            await page.goto("https://example.test/app")
            await page.locator("#summary-accordion summary").click()
            await page.wait_for_function("document.querySelector('#ind-found').textContent==='3'")
            assert requests[0] == {"rounds": ["150"], "target": ["150"], "since": ["150"]}
            # The summary redraws several independent fields; do not read the
            # second one before its asynchronous DOM update is complete.
            await page.wait_for_function("document.querySelector('#ind-since-count').textContent==='47'")
            assert await page.locator("#ind-since-count").inner_text() == "47"
            assert await page.locator("#ind-compare").inner_text() == "+1"
            assert await page.locator("#ind-summary-matches .ind-summary-match").count() == 1
            await page.locator("#ind-rounds").fill("250")
            await page.locator("#ind-target").fill("200")
            await page.locator("#ind-since").fill("100")
            await page.locator("#ind-summary-apply").click()
            await page.wait_for_function("document.querySelector('#summary-heading').textContent.includes('250')")
            assert requests[-1] == {"rounds": ["250"], "target": ["200"], "since": ["100"]}
            await page.locator("#recent-limit").select_option("50")
            assert "250 раундов" in await page.locator("#summary-heading").inner_text()
            assert not errors, errors
            await browser.close()

    run(case())
