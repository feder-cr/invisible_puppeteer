"""Element actions try once, and the waits end as pyppeteer's do, on a real
engine.

Until 0.3.7 `click`, `hover` and `tap` went through the engine's Playwright
loop: they waited for the element to be visible, enabled, stable and the
topmost one at the point, and tried again until the timeout. pyppeteer 1.0.2
scrolls the element into view if needed, takes its clickable point and acts
there once, with no other check (`ElementHandle.click`). And:

- `goBack` / `goForward` to a same-document entry waited the whole timeout
  and every timeout answered None, where pyppeteer's `NavigatorWatcher`
  completes at once on a same-document navigation and raises on a timeout;
- `waitForNavigation` did not end on a same-document navigation;
- `setContent` waited for `load`, where pyppeteer's is the evaluation alone;
- `waitForFunction` swallowed the predicate's error and reported a timeout,
  where pyppeteer rejects with the error; and an unknown `polling` was
  accepted, where pyppeteer raises ValueError.
"""
from __future__ import annotations

import asyncio
import http.server
import threading
import time

import pytest

pytestmark = pytest.mark.e2e

PAGE = b"""<!doctype html><html><head><title>start</title></head>
<body style="margin:0">
<div style="position:relative;width:200px;height:40px">
  <button id=covered onclick="window.coveredClicks = 1">covered</button>
  <div id=overlay style="position:absolute;left:0;top:0;width:200px;height:40px"
       onclick="window.overlayClicks = (window.overlayClicks || 0) + 1"></div>
</div>
<button id=disabled disabled>disabled</button>
<button id=hidden style="display:none">hidden</button>
<a id=hash href="#a">hash</a>
<div style="height:3000px"></div>
<button id=below onclick="window.belowClicks = 1">below</button>
<div style="height:3000px"></div>
</body></html>"""
#: A 1x1 GIF.
GIF = bytes.fromhex("47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b")


def _serve():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            ctype = "text/html; charset=utf-8"
            if self.path.startswith("/slowimg"):
                time.sleep(1.5)
                body, ctype = GIF, "image/gif"
            elif self.path.startswith("/slow"):
                time.sleep(1.5)
                body = PAGE
            else:
                body = PAGE
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d/" % srv.server_address[1]


def _with_page(firefox_binary, seed, body):
    from invisible_puppeteer import launch
    srv, url = _serve()

    async def run():
        b = await launch(seed=seed, executablePath=firefox_binary, headless=True)
        try:
            page = await b.newPage()
            await page.goto(url)
            await body(page, url)
        finally:
            await b.close()

    try:
        asyncio.run(run())
    finally:
        srv.shutdown()


#: A refusal is answered by one look. The loop it replaced waited for its
#: timeout before giving up.
AT_ONCE_S = 2.5


def test_a_covered_element_is_clicked_where_it_is(firefox_binary):
    """No hit-target check: the press goes to the point, the overlay takes it."""
    async def body(page, url):
        began = time.monotonic()
        await (await page.querySelector("#covered")).click()
        assert time.monotonic() - began < AT_ONCE_S + 2.0
        assert await page.evaluate("() => window.overlayClicks") == 1
        assert await page.evaluate("() => window.coveredClicks") is None
    _with_page(firefox_binary, 4248, body)


def test_a_disabled_button_is_clicked_without_an_error(firefox_binary):
    async def body(page, url):
        began = time.monotonic()
        await (await page.querySelector("#disabled")).click()
        assert time.monotonic() - began < AT_ONCE_S + 2.0
    _with_page(firefox_binary, 4253, body)


def test_an_element_not_rendered_is_refused_at_once(firefox_binary):
    from invisible_puppeteer import errors

    async def body(page, url):
        hidden = await page.querySelector("#hidden")
        began = time.monotonic()
        with pytest.raises(errors.ElementHandleError,
                           match="Node is either not visible or not an HTMLElement"):
            await hidden.click()
        assert time.monotonic() - began < AT_ONCE_S
    _with_page(firefox_binary, 4254, body)


def test_a_detached_element_is_refused_at_once(firefox_binary):
    from invisible_puppeteer import errors

    async def body(page, url):
        hidden = await page.querySelector("#hidden")
        await page.evaluate("() => document.getElementById('hidden').remove()")
        began = time.monotonic()
        with pytest.raises(errors.ElementHandleError,
                           match="Node is detached from document"):
            await hidden.click()
        assert time.monotonic() - began < AT_ONCE_S
    _with_page(firefox_binary, 4255, body)


def test_an_element_below_the_fold_is_scrolled_to_the_centre(firefox_binary):
    async def body(page, url):
        await (await page.querySelector("#below")).click()
        assert await page.evaluate("() => window.belowClicks") == 1
        middle, height = await page.evaluate(
            "() => { const r = document.getElementById('below').getBoundingClientRect();"
            " return [(r.top + r.bottom) / 2, innerHeight]; }")
        assert abs(middle - height / 2) <= 2
    _with_page(firefox_binary, 4256, body)


def test_history_and_navigation_waits_end_on_a_same_document_navigation(firefox_binary):
    async def body(page, url):
        await page.goto(url + "#a")
        assert page.url == url + "#a"
        for go in (page.goBack, page.goForward):
            began = time.monotonic()
            assert await go(timeout=8000) is None
            assert time.monotonic() - began < 1.5
        began = time.monotonic()
        response, _ = await asyncio.gather(
            page.waitForNavigation(timeout=8000),
            page.evaluate("() => { location.hash = 'b'; }"))
        assert response is None
        assert time.monotonic() - began < 1.5
    _with_page(firefox_binary, 4249, body)


def test_a_history_timeout_is_an_error(firefox_binary):
    from invisible_puppeteer import errors

    async def body(page, url):
        await page.goto(url + "slow")
        with pytest.raises(errors.TimeoutError,
                           match=r"Navigation Timeout Exceeded: 300 ms exceeded\."):
            await page.reload(timeout=300)
    _with_page(firefox_binary, 4250, body)


def test_set_content_does_not_wait_for_load(firefox_binary):
    async def body(page, url):
        began = time.monotonic()
        await page.setContent("<p>now</p><img src='%sslowimg'>" % url)
        assert time.monotonic() - began < 1.0
        assert await page.evaluate("() => document.querySelector('p').textContent") == "now"
    _with_page(firefox_binary, 4251, body)


def test_wait_for_function_raises_the_predicates_error_and_checks_polling(firefox_binary):
    from invisible_puppeteer import errors

    async def body(page, url):
        began = time.monotonic()
        with pytest.raises(errors.ElementHandleError, match="notDefined"):
            await page.waitForFunction("() => notDefined.x", timeout=10000)
        assert time.monotonic() - began < AT_ONCE_S
        with pytest.raises(ValueError, match="Unknown polling: sometimes"):
            await page.waitForFunction("() => true", polling="sometimes")
        with pytest.raises(ValueError, match="Cannot poll with non-positive interval: -5"):
            await page.waitForFunction("() => true", polling=-5)
        with pytest.raises(ValueError, match=r"Unknown polling option: \[\]"):
            await page.waitForFunction("() => true", polling=[])
    _with_page(firefox_binary, 4252, body)
