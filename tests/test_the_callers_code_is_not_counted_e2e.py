"""Your code runs in a world of its own, where a page that replaced a standard
API cannot count its calls [B322].

Until firefox-49 `evaluate` ran in the page's own realm, so a call to a
standard API went through whatever the page had put in its place. A Cloudflare
challenge wraps `Document.prototype.querySelector` in a Proxy, and one lookup
of its token field from an evaluate got the clearance refused on the reload.
The caller's code now runs in invisible-core's caller's world: a name the
standard defines is the native one, every other name is the page's. This test
holds both halves on a page that counts every call to the APIs it wrapped.
"""
from __future__ import annotations

import http.server
import threading

import pytest

pytestmark = pytest.mark.e2e

PAGE = b"""<!doctype html><html><head><script>
window.__calls = 0;
function wrap(owner, name) {
  const real = owner[name];
  Object.defineProperty(owner, name, {configurable: true, writable: true,
    value: new Proxy(real, {apply(t, self, args) {
      window.__calls++; return Reflect.apply(t, self, args); }})});
}
wrap(Document.prototype, 'querySelector');
wrap(Document.prototype, 'querySelectorAll');
wrap(Element.prototype, 'querySelector');
wrap(Document.prototype, 'getElementById');
wrap(Array.prototype, 'map');
wrap(JSON, 'stringify');
window.__APP = {ready: true};
const pageConst = 'c';
</script></head><body>
<input name="cf-turnstile-response" value="tok"><div id="x">here</div>
<script>document.getElementById('x').__vue__ = {msg: 'hi'}; window.__calls = 0;</script>
</body></html>"""


def _serve():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(PAGE)))
            self.end_headers()
            self.wfile.write(PAGE)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d/" % srv.server_address[1]


def test_the_page_counts_none_of_your_calls_and_its_additions_stay_its_own(firefox_binary):
    import asyncio

    from invisible_puppeteer import launch
    srv, url = _serve()

    async def run():
        b = await launch(seed=4249, executablePath=firefox_binary, headless=True)
        try:
            page = await b.newPage()
            await page.goto(url)
            assert await page.evaluate(
                "() => document.querySelector('input[name=\"cf-turnstile-response\"]').value") == "tok"
            assert await page.evaluate(
                "() => [...document.querySelectorAll('input')].map(i => i.name)") == [
                    "cf-turnstile-response"]
            assert await page.evaluate("() => JSON.stringify(document.getElementById('x').id)") == '"x"'
            await page.waitForFunction("() => document.querySelector('#x')", timeout=5000)
            assert await page.querySelectorEval("#x", "el => el.__vue__.msg") == "hi"
            handle = await page.evaluateHandle("() => document.body.querySelector('#x')")
            assert await page.evaluate("el => el.id", handle) == "x"
            # what the page added is still the page's, its top-level const included
            assert await page.evaluate("() => window.__APP.ready") is True
            assert await page.evaluate("() => pageConst") == "c"
            # a data property of the page: reading it runs none of the page's code
            assert await page.evaluate("() => window.__calls") == 0
        finally:
            await b.close()

    try:
        asyncio.run(run())
    finally:
        srv.shutdown()
