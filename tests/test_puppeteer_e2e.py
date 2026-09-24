"""pyppeteer's contract, against the real patched Firefox.

One event loop and one browser for the module, a local HTTP server for the
pages. The assertions are on what a PAGE observes or on what pyppeteer's
contract returns.
"""
from __future__ import annotations

import asyncio
import http.server
import threading

import pytest

pytestmark = pytest.mark.e2e

PAGES = {
    "/": """<!doctype html><html><head><title>Home page</title></head><body>
<h1 id="head" class="big title">Hello   world</h1>
<a id="next" href="/second">Go to second</a>
<a href="/second" target="_blank" id="blank">Open in new tab</a>
<input id="name" value="">
<button id="btn" onclick="window.clicks = (window.clicks||0)+1">Press</button>
<select id="pick"><option value="a">Alpha</option><option value="b">Beta</option>
<option value="c">Gamma</option></select>
<div id="hidden" style="display:none">secret</div>
<iframe id="frm" name="frm" src="/frame"></iframe>
<div id="late"></div>
<script>
window.events = []; window.moves = 0;
document.addEventListener('mousemove', () => { window.moves++; }, true);
for (const t of ['mousedown','mouseup','click','keydown','keyup','input','change'])
  document.addEventListener(t, e => window.events.push(t + ':' + e.isTrusted), true);
setTimeout(() => { const s = document.createElement('span'); s.id = 'appeared';
  s.textContent = 'here'; document.getElementById('late').appendChild(s); }, 1200);
console.log('hello', 42);
</script></body></html>""",
    "/second": """<!doctype html><html><head><title>Second page</title></head>
<body><p id="where">second</p></body></html>""",
    "/frame": """<!doctype html><html><body><p id="inner">inside the frame</p></body></html>""",
    "/alerts": """<!doctype html><html><head><title>Alerts</title></head><body>
<button id="a" onclick="window.answer = prompt('name?', 'x')">p</button></body></html>""",
    "/json": '{"ok": true, "n": 3}',
}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        body = PAGES.get(path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json" if path == "/json"
                         else "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def site():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % server.server_address[1]
    server.shutdown()


@pytest.fixture(scope="module")
def loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="module")
def browser(loop, firefox_binary):
    from invisible_puppeteer import launch
    b = loop.run_until_complete(launch(seed=42, executablePath=firefox_binary,
                                       headless=True, locale="en-US",
                                       timezone="Europe/London"))
    yield b
    loop.run_until_complete(b.close())


@pytest.fixture
def run(loop):
    return loop.run_until_complete


@pytest.fixture
def page(browser, run, site):
    pages = run(browser.pages())
    p = pages[0]
    run(p.goto(site + "/"))
    return p


def test_launch_leaves_one_page_and_the_flag_is_off(browser, run, page):
    assert len(run(browser.pages())) >= 1
    assert run(page.evaluate("() => navigator.webdriver")) is False
    assert run(page.title()) == "Home page"
    assert run(browser.version()).startswith("Firefox/")


def test_goto_returns_the_documents_response(page, run, site):
    response = run(page.goto(site + "/json"))
    assert response is not None and response.status == 200 and response.ok
    assert run(response.json()) == {"ok": True, "n": 3}
    assert page.url == site + "/json"
    run(page.goto(site + "/second"))
    run(page.goBack())
    assert run(page.title()) != "Second page"


def test_selectors_and_evaluation(page, run):
    head = run(page.querySelector("#head"))
    assert run(page.evaluate("(el) => el.textContent", head)) == "Hello   world"
    assert len(run(page.querySelectorAll("option"))) == 3
    assert run(page.querySelector("#nothing")) is None
    assert len(run(page.xpath("//button"))) == 1
    assert run(page.querySelectorEval("#head", "el => el.id")) == "head"
    assert run(page.querySelectorAllEval("option", "els => els.length")) == 3
    assert run(page.evaluate("1 + 2")) == 3
    assert run(page.evaluate("(a, b) => a * b", 6, 7)) == 42
    assert run(page.evaluate("() => Promise.resolve('later')")) == "later"
    handle = run(page.evaluateHandle("() => ({a: 1, b: [2]})"))
    assert run(handle.jsonValue()) == {"a": 1, "b": [2]}
    props = run(handle.getProperties())
    assert set(props) == {"a", "b"}
    element = run(page.evaluateHandle("() => document.getElementById('btn')"))
    assert element.asElement() is not None
    assert run(element.boundingBox())["width"] > 0


def test_evaluation_errors(page, run):
    from invisible_puppeteer.errors import ElementHandleError
    with pytest.raises(ElementHandleError):
        run(page.evaluate("() => { throw new Error('boom') }"))


def test_click_is_a_trusted_pointer_that_travels(page, run):
    run(page.evaluate("() => { window.events = []; window.moves = 0; window.clicks = 0; }"))
    run(page.click("#btn"))
    assert run(page.evaluate("() => window.clicks")) == 1
    events = run(page.evaluate("() => window.events"))
    assert "click:true" in events and not any(e.endswith(":false") for e in events)
    assert run(page.evaluate("() => window.moves")) > 3
    run(page.mouse.move(10, 10))
    run(page.click("#btn", {"clickCount": 2}))
    assert run(page.evaluate("() => window.clicks")) == 3


def test_typing_keys_and_select(page, run):
    run(page.evaluate("() => { window.events = []; }"))
    run(page.type("#name", "hello"))
    assert run(page.evaluate("() => document.getElementById('name').value")) == "hello"
    run(page.keyboard.press("Backspace"))
    assert run(page.evaluate("() => document.getElementById('name').value")) == "hell"
    events = run(page.evaluate("() => window.events"))
    assert "keydown:true" in events and not any(e.endswith(":false") for e in events)
    assert run(page.select("#pick", "c")) == ["c"]
    assert "change:true" in run(page.evaluate("() => window.events"))


def test_waiting(page, run):
    el = run(page.waitForSelector("#appeared", {"visible": True, "timeout": 5000}))
    assert run(page.evaluate("(e) => e.textContent", el)) == "here"
    assert run(page.waitForSelector("#hidden", {"hidden": True})) is None
    handle = run(page.waitForFunction("() => window.clicks !== undefined || true"))
    assert handle is not None
    from invisible_puppeteer.errors import TimeoutError
    with pytest.raises(TimeoutError):
        run(page.waitForSelector("#never", {"timeout": 500}))


def test_frames(page, run):
    frames = page.frames
    assert len(frames) >= 2
    child = [f for f in frames if f is not page.mainFrame][0]
    el = run(child.waitForSelector("#inner"))
    assert run(child.evaluate("(e) => e.textContent", el)) == "inside the frame"
    frame_el = run(page.querySelector("#frm"))
    assert run(frame_el.contentFrame()) is not None


def test_dialogs_and_console(browser, run, site):
    p = run(browser.newPage())
    seen = []

    async def on_dialog(dialog):
        seen.append((dialog.type, dialog.message, dialog.defaultValue))
        await dialog.accept("Ada")

    p.on("dialog", on_dialog)
    run(p.goto(site + "/alerts"))
    run(p.click("#a"))
    run(asyncio.sleep(0.5))
    assert seen == [("prompt", "name?", "x")]
    assert run(p.evaluate("() => window.answer")) == "Ada"
    messages = []
    p.on("console", lambda m: messages.append((m.type, m.text)))
    run(p.goto(site + "/"))
    run(asyncio.sleep(0.5))
    assert ("log", "hello 42") in messages
    run(p.close())
    assert p.isClosed()


def test_popups(page, run):
    popups = []
    page.on("popup", lambda p: popups.append(p))
    run(page.click("#blank"))
    for _ in range(50):
        if popups:
            break
        run(asyncio.sleep(0.1))
    assert popups, "no popup event"
    popup = popups[0]
    run(popup.waitForSelector("#where"))
    assert run(popup.title()) == "Second page"
    run(popup.close())


def test_cookies(page, run, site):
    run(page.setCookie({"name": "k", "value": "v"}))
    cookies = run(page.cookies())
    assert any(c["name"] == "k" and c["value"] == "v" for c in cookies)
    assert "k=v" in run(page.evaluate("() => document.cookie"))
    run(page.deleteCookie({"name": "k"}))
    assert not any(c["name"] == "k" for c in run(page.cookies()))


def test_screenshots(page, run, tmp_path):
    png = run(page.screenshot())
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    target = tmp_path / "shot.png"
    run(page.screenshot({"path": str(target)}))
    assert target.stat().st_size > 0
    el = run(page.querySelector("#btn"))
    assert run(el.screenshot())[:4] == b"\x89PNG"


def test_incognito_context_has_its_own_cookies(browser, run, site):
    ctx = run(browser.createIncognitoBrowserContext())
    p = run(ctx.newPage())
    run(p.goto(site + "/second"))
    run(p.setCookie({"name": "only_here", "value": "1"}))
    main = run(browser.pages())[0]
    run(main.goto(site + "/second"))
    assert not any(c["name"] == "only_here" for c in run(main.cookies()))
    run(ctx.close())


def test_the_profile_reaches_the_page(page, run):
    assert run(page.evaluate("() => navigator.language")) == "en-US"
    assert run(page.evaluate(
        "() => Intl.DateTimeFormat().resolvedOptions().timeZone")) == "Europe/London"
    assert run(page.evaluate("() => screen.width")) >= run(
        page.evaluate("() => window.innerWidth")) > 0
