"""Cookies as pyppeteer answers them, every expectation asked of pyppeteer
1.0.2 and its Chromium r588429 on 2026-10-13 (`C:/tmp/b230/oracle_pp_phase3.py`):

* `cookies()` is Chrome's `Network.getCookies` object for each cookie:
  domain, expires (-1 for a session cookie), httpOnly, name, path, secure,
  session, size and value, and `sameSite` only when the cookie declared it.
  Until 0.6.0 `sameSite: "None"` came with every cookie, the engine's word for
  one that declared nothing;
* the cookies of another URL of the same site include it;
* `deleteCookie` takes it away;
* on a blank page a cookie with neither url nor domain is the protocol error of
  the deletion pyppeteer sends first, and a data: URL is refused.
"""
from __future__ import annotations

import asyncio
import http.server
import threading

import pytest

pytestmark = pytest.mark.e2e


def test_cookies_are_chrome_s_objects(firefox_binary):
    from invisible_puppeteer import errors, launch

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"<!doctype html><title>c</title>"
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
    base = "http://127.0.0.1:%d" % srv.server_address[1]

    async def run():
        b = await launch(seed=4313, executablePath=firefox_binary,
                         headless=True)
        said = {}
        try:
            page = await b.newPage()
            await page.goto(base + "/")
            await page.setCookie({"name": "a", "value": "1"})
            said["cookies"] = await page.cookies()
            said["other"] = [c["name"] for c in await page.cookies(base + "/x")]
            await page.setCookie({"name": "s", "value": "2", "sameSite": "Lax"})
            said["declared"] = [c.get("sameSite") for c in await page.cookies()
                                if c["name"] == "s"]
            await page.deleteCookie({"name": "a"}, {"name": "s"})
            said["deleted"] = [c["name"] for c in await page.cookies()]
            await page.goto("about:blank")
            for key, cookie in (
                    ("blank", {"name": "b", "value": "1"}),
                    ("data", {"name": "c", "value": "1",
                              "url": "data:text/html,x"})):
                try:
                    await page.setCookie(cookie)
                except Exception as e:  # noqa: BLE001 - its class is asserted
                    said[key] = (type(e), str(e))
        finally:
            await b.close()
        return said

    try:
        said = asyncio.run(run())
    finally:
        srv.shutdown()
    assert said["cookies"] == [{
        "domain": "127.0.0.1", "expires": -1, "httpOnly": False, "name": "a",
        "path": "/", "secure": False, "session": True, "size": 2,
        "value": "1"}]
    assert said["other"] == ["a"]
    assert said["declared"] == ["Lax"]
    assert said["deleted"] == []
    assert said["blank"] == (
        errors.NetworkError, "Protocol error (Network.deleteCookies): At least "
        "one of the url and domain needs to be specified")
    assert said["data"] == (errors.PageError,
                            'Data URL page can not have cookie "c"')
