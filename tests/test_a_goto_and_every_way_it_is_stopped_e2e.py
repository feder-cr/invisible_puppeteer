"""A `goto` that something stops or replaces on its way raises at once or
answers with the document the page ends on; never a response for a page it is
not on, never the timeout ([B302], the class).

Firefox lets the page being left stop a navigation the browser started
(document.open, window.stop, a location.replace, a form submit, an iframe
navigating the top, history.back); pyppeteer's Chromium lets the browser's win
over every one but going back (measured, `C:/tmp/b230/oracle_pyppeteer_class.py`,
2026-10-14: goto answered on the page asked for in 1.5 s, and for back in
0.3 s on the page gone back to). The engines differ, so what this client does
is pyppeteer's NavigatorWatcher rule on Firefox's events: a document that took
the frame in place of the one asked for is waited on in its place, and its
response is the answer; a stop with nothing in its place is PageError
"<reason> at <url>", at once.

Until firefox-46 the engine dropped the stop and took the page's
document.write() for the navigation's commit: `goto` answered on the OLD page,
or waited out its timeout. Same site (one process) and another site
(`localhost`, a Fission process swap), twice, back to back; the page being left
acts 300 ms after it loads, and the server holds the page asked for 1.5 s.
"""
from __future__ import annotations

import asyncio
import http.server
import threading
import time

import pytest

pytestmark = pytest.mark.e2e

HELD_S = 1.5
ACT = {
    "docopen": "document.open(); document.write('<html><body><h1>rewritten</h1></body></html>'); document.close();",
    "stop": "window.stop();",
    "replace": "location.replace('/other?from=replace');",
    "assign": "location.assign('/other?from=assign');",
    "pushstate": "history.pushState({}, '', '/pushed');",
    "back": "history.back();",
    "submit": "document.getElementById('f').submit();",
    "meta": None,
    "iframe_top": None,
    "none": "",
}
#: What this client answers on Firefox, and where the page then is.
EXPECT = {
    "docopen": ("raised", "/old"),
    "stop": ("raised", "/old"),
    "replace": ("followed", "/other?from=replace"),
    "assign": ("followed", "/other?from=assign"),
    "back": ("followed", "/start"),
    "submit": ("followed", "/other?from=submit"),
    "iframe_top": ("followed", "/other?from=iframe"),
    "pushstate": ("landed", None),
    "meta": ("landed", None),
    "none": ("landed", None),
}


def _old_page(kind):
    if kind == "meta":
        return ("<html><head><meta http-equiv=refresh content='0.3;url=/other?from=meta'>"
                "</head><body><p>old</p></body></html>")
    if kind == "iframe_top":
        return ("<html><body><p>old</p><iframe srcdoc=\"<script>setTimeout(() => "
                "{top.location = '/other?from=iframe'}, 300)</script>\"></iframe></body></html>")
    return ("<html><body><p>old</p><form id=f action='/other' method=get>"
            "<input name=from value=submit></form><script>setTimeout(() => {%s}, 300)"
            "</script></body></html>" % ACT[kind])


def _serve():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/new":
                time.sleep(HELD_S)
                body = "<html><body><p>new</p></body></html>"
            elif path == "/old":
                body = _old_page(self.path.split("k=")[1].split("&")[0])
            else:
                body = "<html><body><p>%s</p></body></html>" % path
            body = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def handle(self):
            try:
                super().handle()
            except ConnectionError:
                pass  # the browser let go of a response it stopped

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


@pytest.mark.parametrize("kind", list(ACT))
def test_a_goto_stopped_on_its_way_raises_or_answers_where_it_is(firefox_binary, kind):
    from invisible_puppeteer import errors, launch

    srv, port = _serve()
    same, cross = "http://127.0.0.1:%d" % port, "http://localhost:%d" % port
    answer, end = EXPECT[kind]

    async def run():
        b = await launch(seed=4294, executablePath=firefox_binary, headless=True)
        try:
            page = await b.newPage()
            for site, target_base in (("same", same), ("cross", cross)):
                for i in range(2):
                    await page.goto(same + "/start?k=%s&i=%d" % (kind, i))
                    await page.goto(same + "/old?k=%s&i=%d" % (kind, i))
                    target = target_base + "/new?k=%s&i=%d" % (kind, i)
                    began = time.monotonic()
                    error, response = None, None
                    try:
                        response = await page.goto(target, waitUntil="domcontentloaded",
                                                   timeout=10000)
                    except errors.PageError as e:
                        error = str(e)
                    took = time.monotonic() - began
                    where = (kind, site, i, error, took, page.url)
                    if answer == "raised":
                        assert error == "NS_BINDING_ABORTED at %s" % target, where
                        assert took < HELD_S, where
                        await page.waitForSelector("h1" if kind == "docopen" else "p")
                        assert page.url == same + "/old?k=%s&i=%d" % (kind, i), where
                        continue
                    assert error is None, where
                    if answer == "followed":
                        assert took < HELD_S, where
                        expected = same + end
                        if end == "/start":
                            expected = same + "/start?k=%s&i=%d" % (kind, i)
                        assert page.url == expected, where
                    else:
                        assert took >= HELD_S - 0.1, where
                        assert page.url == target, where
                    # The answer names the page it is on: never another's.
                    assert response is None or response.url == page.url, (where, response.url)
        finally:
            await b.close()

    try:
        asyncio.run(run())
    finally:
        srv.shutdown()
