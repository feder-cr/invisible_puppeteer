"""`waitUntil="networkidle2"` is pyppeteer's: at most two requests in flight
for 500 ms, so a page that keeps two requests open reaches it.

Until 0.5.0 it waited for `networkidle0`, none in flight, and such a page
timed out.
"""
from __future__ import annotations

import asyncio
import http.server
import threading

import pytest

pytestmark = pytest.mark.e2e

PAGE = (b"<!doctype html><title>polls</title><script>"
        b"addEventListener('load', () => { fetch('/hang?1'); fetch('/hang?2'); });"
        b"</script>")


def _serve():
    release = threading.Event()

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/hang"):
                release.wait(30)
                body = b"late"
            else:
                body = PAGE
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
    return srv, release, "http://127.0.0.1:%d/" % srv.server_address[1]


def test_two_requests_held_open_reach_networkidle2_not_networkidle0(firefox_binary):
    from invisible_puppeteer import errors, launch

    srv, release, url = _serve()

    async def run():
        b = await launch(seed=4293, executablePath=firefox_binary, headless=True)
        try:
            page = await b.newPage()
            await page.goto(url, waitUntil="networkidle2", timeout=10000)
            with pytest.raises(errors.TimeoutError):
                await page.goto(url, waitUntil="networkidle0", timeout=3000)
        finally:
            await b.close()

    try:
        asyncio.run(run())
    finally:
        release.set()
        srv.shutdown()
