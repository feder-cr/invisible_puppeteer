"""The caller's function runs once when its page navigates while it waits [B285].

A function waiting on a promise when its page navigated was refused by the new
document in the same words as one that never ran (Juggler sends the waiting
message again), and `evaluate` sent it again into the new page: a function
with an effect ran twice. pyppeteer raises NetworkError at once ("Execution
context was destroyed, most likely because of a navigation."); its
`waitForFunction` goes on in the new document.
"""
from __future__ import annotations

import http.server
import threading
import time

import pytest

pytestmark = pytest.mark.e2e

FIRST = b"<!doctype html><html><head><title>first</title></head><body>first</body></html>"
NEXT = b"<!doctype html><html><head><title>next</title></head><body>next</body></html>"


def _serve():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = NEXT if self.path.startswith("/next") else FIRST
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
    return srv, "http://127.0.0.1:%d/" % srv.server_address[1]


async def _settled_on_next(page):
    import asyncio
    deadline = time.monotonic() + 20
    while await page.title() != "next":
        assert time.monotonic() < deadline, "the page never reached /next"
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.5)  # a second run would have navigated and counted


def test_evaluate_interrupted_by_its_navigation_runs_once(firefox_binary):
    import asyncio

    from invisible_puppeteer import errors, launch
    srv, url = _serve()

    async def run():
        b = await launch(seed=4247, executablePath=firefox_binary, headless=True)
        try:
            page = await b.newPage()
            await page.goto(url)
            await page.evaluate("() => localStorage.clear()")
            with pytest.raises(errors.NetworkError,
                               match="Execution context was destroyed"):
                await page.evaluate(
                    "() => { localStorage.runs = String(Number(localStorage.runs || 0) + 1);"
                    " setTimeout(() => { location.href = '/next'; }, 100);"
                    " return new Promise(() => {}); }")
            await _settled_on_next(page)
            assert await page.evaluate("() => localStorage.runs") == "1"
        finally:
            await b.close()

    try:
        asyncio.run(run())
    finally:
        srv.shutdown()


def test_wait_for_function_goes_on_through_a_navigation(firefox_binary):
    import asyncio

    from invisible_puppeteer import launch
    srv, url = _serve()

    async def run():
        b = await launch(seed=4248, executablePath=firefox_binary, headless=True)
        try:
            page = await b.newPage()
            await page.goto(url)
            await page.evaluate("() => { setTimeout(() => { location.href = '/next'; }, 300); }")
            await page.waitForFunction("() => document.title === 'next'",
                                       {"timeout": 15000})
            assert await page.title() == "next"
        finally:
            await b.close()

    try:
        asyncio.run(run())
    finally:
        srv.shutdown()
