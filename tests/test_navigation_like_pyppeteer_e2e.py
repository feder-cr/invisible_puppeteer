"""Navigation waits as pyppeteer's `NavigatorWatcher` decides them.

pyppeteer completes `goto`, `waitForNavigation`, `reload` and the history
calls when the main frame carries a document other than the one it had when
the wait began and that document, and every child frame, has the expected
lifecycle events (`navigator_watcher.py` `_checkLifecycleComplete`,
`_checkLifecycle`). So:

* it never says "interrupted": a document that committed in place of the one
  asked for is waited on in its place;
* `goto` answers with the response of the document the frame ends on
  (`requests.get(mainFrame._navigationURL)`), the final one after the page
  replaced itself;
* `domcontentloaded` and the others are waited for on the child frames too,
  those that started loading (a lazy iframe out of view never does, and is
  not waited for, as in puppeteer since `_hasStartedLoading`).

Until 0.6.0 the first raised PageError "is interrupted by another
navigation", the second answered the first document's response, and the third
returned on the main frame alone.
"""
from __future__ import annotations

import asyncio
import http.server
import threading
import time

import pytest

pytestmark = pytest.mark.e2e

PAGES = {
    # Leaves on its own 300 ms after loading: a goto sent right after it is
    # still waiting for /held when this navigation replaces it.
    "/leaves": b"<!doctype html><title>leaves</title><script>"
               b"addEventListener('load', () => setTimeout("
               b"() => { location.href = '/landed'; }, 300));</script>",
    "/held": b"<!doctype html><title>held</title>",
    "/landed": b"<!doctype html><title>landed</title>",
    "/replaces": b"<!doctype html><title>replaces</title>"
                 b"<script>location.replace('/final');</script>",
    "/final": b"<!doctype html><title>final</title>",
    "/parent": b"<!doctype html><title>parent</title>"
               b"<iframe id=slow src='/slow-child'></iframe>"
               b"<div style='height:6000px'></div>"
               b"<iframe id=lazy loading=lazy src='/lazy-child'></iframe>",
    "/slow-child": b"<!doctype html><title>slow child</title><p>slow</p>",
    "/lazy-child": b"<!doctype html><title>lazy child</title>",
}
HOLD_S = {"/held": 3.0, "/slow-child": 1.5}


def _serve():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(HOLD_S.get(self.path, 0))
            body = PAGES.get(self.path, b"<!doctype html>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


def _run(firefox_binary, body):
    from invisible_puppeteer import launch

    srv, base = _serve()

    async def run():
        b = await launch(seed=4294, executablePath=firefox_binary,
                         headless=True)
        try:
            page = await b.newPage()
            return await body(page, base)
        finally:
            await b.close()

    try:
        return asyncio.run(run())
    finally:
        srv.shutdown()


def test_a_navigation_replaced_before_it_committed_is_waited_on_in_its_place(
        firefox_binary):
    async def body(page, base):
        await page.goto(base + "/leaves")
        response = await page.goto(base + "/held", timeout=10000)
        return response.url if response else None, page.url, await page.title()

    response_url, url, title = _run(firefox_binary, body)
    assert url.endswith("/landed"), url
    assert title == "landed"
    assert response_url and response_url.endswith("/landed"), response_url


def test_goto_answers_with_the_document_the_page_replaced_itself_with(
        firefox_binary):
    async def body(page, base):
        response = await page.goto(base + "/replaces", timeout=10000)
        return response.url if response else None, page.url

    response_url, url = _run(firefox_binary, body)
    assert url.endswith("/final"), url
    assert response_url and response_url.endswith("/final"), response_url


def test_domcontentloaded_waits_for_the_child_frames_that_started(
        firefox_binary):
    async def body(page, base):
        began = time.monotonic()
        await page.goto(base + "/parent", waitUntil="domcontentloaded",
                        timeout=10000)
        took = time.monotonic() - began
        child = [f for f in page.frames if f.url.endswith("/slow-child")]
        text = (await child[0].evaluate("document.body.innerText")
                if child else None)
        return took, text

    took, text = _run(firefox_binary, body)
    # The slow child is held 1.5 s; the lazy one below the fold never
    # starts, and waiting for it would have run to the 10 s timeout.
    assert 1.2 < took < 8, took
    assert text == "slow"
