"""A `goto` that the page being left stops raises at once ([B302]).

document.open() stops the ongoing navigation of its browsing context in
Firefox, retail included; pyppeteer's Chromium lets a navigation the browser
started run on (measured, `C:/tmp/b230/oracle_b302.py pyppeteer`: goto answers
on the new page in 1.5 s), so here the engines differ and the original cannot
be copied. What this client says is pyppeteer's word for a navigation that
ended without its document, PageError "<reason> at <url>", at once.

Until firefox-46 the engine dropped the stop and took the page's
document.write() for the navigation's commit: `goto` answered on the OLD page,
or waited out its timeout. The race is made certain: the page being left
rewrites itself 300 ms after it loads, and the server holds the next page's
response for 1.5 s.
"""
from __future__ import annotations

import asyncio
import http.server
import threading
import time

import pytest

pytestmark = pytest.mark.e2e

OLD = (b"<html><body><p>first</p><script>setTimeout(() => {document.open();"
       b"document.write('<html><body><h1>rewritten</h1></body></html>');"
       b"document.close()}, 300)</script></body></html>")
NEW = b"<html><body><p>new</p></body></html>"
HELD_S = 1.5


def _serve():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/new"):
                time.sleep(HELD_S)
                body = NEW
            else:
                body = OLD
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def handle(self):
            try:
                super().handle()
            except ConnectionError:
                pass  # the browser let go of the response it stopped

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d/" % srv.server_address[1]


def test_a_goto_the_page_left_stops_raises_at_once(firefox_binary):
    from invisible_puppeteer import errors, launch

    srv, base = _serve()

    async def run():
        b = await launch(seed=4294, executablePath=firefox_binary, headless=True)
        try:
            page = await b.newPage()
            for i in range(2):
                await page.goto(base + "old?i=%d" % i)
                began = time.monotonic()
                with pytest.raises(errors.PageError) as e:
                    await page.goto(base + "new?i=%d" % i,
                                    waitUntil="domcontentloaded", timeout=10000)
                took = time.monotonic() - began
                assert str(e.value) == "NS_BINDING_ABORTED at %snew?i=%d" % (base, i)
                assert took < HELD_S, took
                await page.waitForSelector("h1")
                assert page.url == base + "old?i=%d" % i
        finally:
            await b.close()

    try:
        asyncio.run(run())
    finally:
        srv.shutdown()
