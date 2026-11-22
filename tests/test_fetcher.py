import httpx
import respx

from leadgen.http.fetcher import PoliteFetcher

UA = "TestLeadBot/1.0 (+https://test.invalid/bot)"
HTML = {"content-type": "text/html; charset=utf-8"}


def fetcher(crawl_cfg) -> PoliteFetcher:
    return PoliteFetcher(crawl_cfg, UA, "TestLeadBot")


@respx.mock
async def test_robots_disallow_and_allow(crawl_cfg):
    respx.get("https://a.com/robots.txt").respond(200, text="User-agent: *\nDisallow: /private\n")
    respx.get("https://a.com/ok").respond(200, text="<p>hi</p>", headers=HTML)
    private = respx.get("https://a.com/private/x").respond(200, text="secret", headers=HTML)
    async with fetcher(crawl_cfg) as f:
        assert (await f.fetch_page("https://a.com/ok")).text == "<p>hi</p>"
        assert await f.fetch_page("https://a.com/private/x") is None
    assert not private.called


@respx.mock
async def test_bot_specific_group_applies(crawl_cfg):
    respx.get("https://a.com/robots.txt").respond(
        200, text="User-agent: TestLeadBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n"
    )
    async with fetcher(crawl_cfg) as f:
        assert await f.fetch_page("https://a.com/") is None


@respx.mock
async def test_missing_robots_allows_but_server_error_blocks(crawl_cfg):
    respx.get("https://open.com/robots.txt").respond(404)
    respx.get("https://open.com/").respond(200, text="ok", headers=HTML)
    respx.get("https://down.com/robots.txt").respond(503)
    down_page = respx.get("https://down.com/").respond(200, text="ok", headers=HTML)
    async with fetcher(crawl_cfg) as f:
        assert (await f.fetch_page("https://open.com/")).text == "ok"
        assert await f.fetch_page("https://down.com/") is None
    assert not down_page.called


@respx.mock
async def test_crawl_delay_is_honoured(crawl_cfg):
    respx.get("https://slow.com/robots.txt").respond(200, text="User-agent: *\nCrawl-delay: 7\n")
    respx.get("https://slow.com/").respond(200, text="ok", headers=HTML)
    async with fetcher(crawl_cfg) as f:
        await f.fetch_page("https://slow.com/")
        assert f.limiter.delay_for("slow.com") == 7


@respx.mock
async def test_retries_transient_errors(crawl_cfg):
    respx.get("https://flaky.com/robots.txt").respond(404)
    route = respx.get("https://flaky.com/").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, text="finally", headers=HTML)]
    )
    async with fetcher(crawl_cfg) as f:
        assert (await f.fetch_page("https://flaky.com/")).text == "finally"
    assert route.call_count == 2


@respx.mock
async def test_redirect_rechecks_robots_on_new_host(crawl_cfg):
    respx.get("https://old.com/robots.txt").respond(404)
    respx.get("https://old.com/").respond(301, headers={"location": "https://new.com/home"})
    respx.get("https://new.com/robots.txt").respond(200, text="User-agent: *\nDisallow: /\n")
    target = respx.get("https://new.com/home").respond(200, text="x", headers=HTML)
    async with fetcher(crawl_cfg) as f:
        assert await f.fetch_page("https://old.com/") is None
    assert not target.called


@respx.mock
async def test_non_html_and_errors_return_none(crawl_cfg):
    respx.get("https://b.com/robots.txt").respond(404)
    respx.get("https://b.com/file").respond(200, content=b"%PDF", headers={"content-type": "application/pdf"})
    respx.get("https://b.com/missing").respond(404, headers=HTML)
    async with fetcher(crawl_cfg) as f:
        assert await f.fetch_page("https://b.com/file") is None
        assert await f.fetch_page("https://b.com/missing") is None


@respx.mock
async def test_body_size_is_capped(crawl_cfg):
    crawl_cfg.max_bytes = 20_000
    respx.get("https://big.com/robots.txt").respond(404)
    respx.get("https://big.com/").respond(200, text="a" * 100_000, headers=HTML)
    async with fetcher(crawl_cfg) as f:
        assert len((await f.fetch_page("https://big.com/")).text) == 20_000


async def test_api_deadline_caps_a_slow_trickling_server(crawl_cfg):
    """httpx timeouts are per read, so a server that keeps a request open could stall a run for 20+
    minutes (seen with a busy public Overpass server). deadline_s caps each attempt."""
    import asyncio
    import time

    import pytest

    calls = 0

    async def slow(request):
        nonlocal calls
        calls += 1
        await asyncio.sleep(2)
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    f = PoliteFetcher(crawl_cfg, UA, "TestLeadBot", client=client)
    t0 = time.monotonic()
    with pytest.raises(httpx.ReadTimeout, match="no complete response within"):
        await f.api_request("POST", "https://slow.test/api/interpreter", attempts=2, deadline_s=0.2)
    assert calls == 2 and time.monotonic() - t0 < 1.5
    await client.aclose()
