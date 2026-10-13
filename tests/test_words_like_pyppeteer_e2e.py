"""Four errors in pyppeteer's words and classes (pyppeteer 1.0.2):

* `page.type` on a selector that matches nothing: PageError
  "Cannot find #nope on this page" (`frame_manager.py`; click, hover and tap
  say "No node found for selector", type alone says this);
* a key that is not on the layout: PyppeteerError "Unknown key: NoSuchKey",
  the base class itself (`input.py`);
* a request handled while interception is off: NetworkError
  "Request interception is not enabled." (`network_manager.py`);
* closing the default context: BrowserError
  "Non-incognito profile cannot be closed" (`browser.py`).

Until 0.6.0 they were "No node found for selector: #nope", a ValueError
"unknown key: 'NoSuchKey'. Valid names are ...", "... is not enabled!" and
"Non-incognito profiles cannot be closed!".
"""
from __future__ import annotations

import asyncio
import http.server
import threading

import pytest

pytestmark = pytest.mark.e2e


def test_four_errors_are_pyppeteer_s(firefox_binary):
    from invisible_puppeteer import errors, launch

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"<!doctype html><title>x</title><input id=i>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/" % srv.server_address[1]

    async def run():
        b = await launch(seed=4295, executablePath=firefox_binary,
                         headless=True)
        said = {}
        try:
            page = await b.newPage()
            requests = []
            page.on("request", requests.append)
            await page.goto(url)
            try:
                await page.type("#nope", "x")
            except Exception as e:  # noqa: BLE001 - its class is asserted
                said["type"] = (type(e), str(e))
            try:
                await page.keyboard.press("NoSuchKey")
            except Exception as e:  # noqa: BLE001
                said["key"] = (type(e), str(e))
            try:
                await requests[0].continue_()
            except Exception as e:  # noqa: BLE001
                said["continue"] = (type(e), str(e))
            try:
                await b.browserContexts[0].close()
            except Exception as e:  # noqa: BLE001
                said["context"] = (type(e), str(e))
            # And a character with no key is typed, not refused, with a delay
            # too: pyppeteer sends it with `sendCharacter`. Until 0.6.0 the
            # delay path raised the core's ValueError.
            await page.focus("#i")
            await page.keyboard.type("aéb", delay=5)
            said["typed"] = await page.evaluate(
                "document.getElementById('i').value")
        finally:
            await b.close()
        return said

    try:
        said = asyncio.run(run())
    finally:
        srv.shutdown()
    assert said["type"] == (errors.PageError, "Cannot find #nope on this page")
    assert said["key"] == (errors.PyppeteerError, "Unknown key: NoSuchKey")
    assert said["continue"] == (errors.NetworkError,
                                "Request interception is not enabled.")
    assert said["context"] == (errors.BrowserError,
                               "Non-incognito profile cannot be closed")
    assert said["typed"] == "aéb"
