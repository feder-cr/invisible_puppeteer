"""`select`, `waitForSelector`, the script and style tags and `setViewport`
behave as pyppeteer 1.0.2's, on a real engine.

What 0.4.0 did instead, each one checked below:

- `select` waited for a visible, enabled select through the engine's
  Playwright loop and raised on an unknown value; pyppeteer selects in a
  hidden or disabled select, ignores unknown values, raises TypeError on a
  value that is not a string and ElementHandleError when nothing matches the
  selector;
- `waitForSelector` started at the await, polled from Python every 100 ms,
  answered None for `hidden`, used Playwright's idea of visible (width AND
  height), and never said " to be hidden" when it timed out; pyppeteer starts
  at the call, polls in the page, answers the hidden element or a handle to
  true;
- `addScriptTag` and `addStyleTag` returned before a tag by `url` had loaded
  and never said it failed;
- `setViewport` coerced its width and height with int().
"""
from __future__ import annotations

import asyncio
import http.server
import threading
import time

import pytest

pytestmark = pytest.mark.e2e

EMPTY = b"<!doctype html><html><head><title>empty</title></head><body></body></html>"
SELECTS = b"""<!doctype html><html><body>
<select id=s style="display:none"><option value=a>A</option><option value=b>B</option></select>
<select id=m multiple><option value=x>X</option><option value=y>Y</option></select>
<select id=d disabled><option value=p>P</option><option value=q>Q</option></select>
<div id=notselect></div>
</body></html>"""


def _serve():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            ctype, status = "text/html; charset=utf-8", 200
            if self.path.startswith("/slow.js"):
                time.sleep(0.5)
                body, ctype = b"window.__loaded = 42;", "application/javascript"
            elif self.path.startswith("/nonexist"):
                body, status = b"not here", 404
            elif self.path.startswith("/selects"):
                body = SELECTS
            else:
                body = EMPTY
            self.send_response(status)
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


def test_select_works_as_pyppeteers(firefox_binary):
    from invisible_puppeteer import errors

    async def body(page, url):
        await page.goto(url + "selects")
        began = time.monotonic()
        assert await page.select("#s", "b") == ["b"]
        assert time.monotonic() - began < 2.0
        assert await page.select("#m", "y", "zzz") == ["y"]
        assert await page.select("#d", "q") == ["q"]
        assert await page.select("#s", "zzz") == []
        with pytest.raises(TypeError, match="^Values must be string"):
            await page.select("#s", 1)
        with pytest.raises(errors.ElementHandleError,
                           match='^Error: failed to find element matching selector "#nope"$'):
            await page.select("#nope", "a")
        with pytest.raises(errors.ElementHandleError,
                           match="^Evaluation failed: Error: Element is not a <select> element."):
            await page.select("#notselect", "a")
    _with_page(firefox_binary, 4260, body)


def test_wait_for_selector_starts_at_the_call(firefox_binary):
    async def body(page, url):
        w = page.waitForSelector("#flash", timeout=3000)
        await page.evaluate(
            "() => setTimeout(() => { const d = document.createElement('div');"
            " d.id = 'flash'; document.body.appendChild(d);"
            " setTimeout(() => d.remove(), 30); }, 300)")
        await asyncio.sleep(1)
        assert await page.evaluate("e => e.id", await w) == "flash"
    _with_page(firefox_binary, 4261, body)


def test_wait_for_selector_visible_and_hidden_as_pyppeteer(firefox_binary):
    from invisible_puppeteer import errors

    async def body(page, url):
        await page.setContent('<div id="e"></div><div id="h" style="display:none">x</div>')
        # Full width and no height is visible to pyppeteer.
        assert await page.waitForSelector("#e", visible=True, timeout=1000)
        hidden = await page.waitForSelector("#h", hidden=True)
        assert await page.evaluate("e => e.id", hidden) == "h"
        gone = await page.waitForSelector("#absent", hidden=True)
        assert await gone.jsonValue() is True
        with pytest.raises(errors.TimeoutError,
                           match=r'^Waiting for selector "#e" to be hidden failed: '
                                 r'timeout 300ms exceeds\.$'):
            await page.waitForSelector("#e", hidden=True, timeout=300)
    _with_page(firefox_binary, 4262, body)


def test_script_and_style_tags_wait_for_their_load(firefox_binary):
    from invisible_puppeteer import errors

    async def body(page, url):
        await page.addScriptTag(url="/slow.js")
        assert await page.evaluate("() => window.__loaded") == 42
        with pytest.raises(errors.PageError, match=r"^Loading script from /nonexistfile\.js failed$"):
            await page.addScriptTag(url="/nonexistfile.js")
        with pytest.raises(errors.PageError, match=r"^Loading style from /nonexist\.css failed$"):
            await page.addStyleTag(url="/nonexist.css")
        with pytest.raises(ValueError, match="Provide an object with a `url`, `path` or `content` property"):
            await page.addScriptTag()
        tag = await page.addScriptTag(content="1")
        assert await page.evaluate("s => s.type", tag) == "text/javascript"
    _with_page(firefox_binary, 4263, body)


def test_set_viewport_checks_its_size_as_pyppeteer(firefox_binary):
    async def body(page, url):
        with pytest.raises(TypeError, match="^width must be integer: <class 'float'>$"):
            await page.setViewport({"width": 800.5, "height": 600})
        with pytest.raises(ValueError, match="^height must be positive integer: -1$"):
            await page.setViewport({"width": 800, "height": -1})
        viewport = {"width": 800, "height": 600}
        await page.setViewport(viewport)
        assert page.viewport is viewport
    _with_page(firefox_binary, 4264, body)


BEFOREUNLOAD = ("<p>bye</p><script>addEventListener('beforeunload', e => {"
                " e.preventDefault(); e.returnValue = 'x'; })</script>")


def test_close_with_run_before_unload_asks_the_page(firefox_binary):
    """pyppeteer's `close(runBeforeUnload=True)` lets the page ask, and the
    page closes when the `beforeunload` dialog is accepted; a page already
    closed raises."""
    from invisible_puppeteer import errors

    async def body(page, url):
        await page.setContent(BEFOREUNLOAD)
        await page.click("p")  # sticky activation, which the prompt needs
        dialogs = []
        page.on("dialog", dialogs.append)
        await page.close(runBeforeUnload=True)
        deadline = time.monotonic() + 5
        while not dialogs and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert dialogs and dialogs[0].type == "beforeunload"
        assert not page.isClosed()
        await dialogs[0].accept()
        deadline = time.monotonic() + 5
        while not page.isClosed() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert page.isClosed()
        with pytest.raises(errors.PageError,
                           match=r"^Protocol Error: Connection Closed\. Most likely "
                                 r"the page has been closed\.$"):
            await page.close()
    _with_page(firefox_binary, 4265, body)
