"""`Page`, `Frame` and the objects around them, with pyppeteer's methods.

Every method is a coroutine, as in pyppeteer, and every engine call it makes
runs off the event loop (`_bridge.run`). The engine page underneath is the one
invisible_playwright drives: the same lifecycle, the same injected script in
the same utility world, the same `Actions`.
"""
from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

from . import _bridge, errors
from ._events import EventEmitter
from ._juggler._profile import _domain_matches, _host_of
from ._juggler.lifecycle import NavigationError
from .element_handle import ElementHandle, JSHandle

#: pyppeteer's `waitUntil` values, as the engine's four load states.
_WAIT_UNTIL = {"load": "load", "domcontentloaded": "domcontentloaded",
               "networkidle0": "networkidle", "networkidle2": "networkidle",
               "commit": "commit"}
_ORDER = ["commit", "domcontentloaded", "load", "networkidle"]


def _state(wait_until) -> str:
    """One engine state for a pyppeteer `waitUntil`, which may be a list: the
    latest of them, since waiting for it waits for the others too."""
    names = wait_until if isinstance(wait_until, (list, tuple)) else [wait_until or "load"]
    states = []
    for name in names:
        if name not in _WAIT_UNTIL:
            raise ValueError("Unknown value for options.waitUntil: %s" % name)
        states.append(_WAIT_UNTIL[name])
    return max(states, key=_ORDER.index)


class ExecutionContext:
    """What `frame.executionContext()` hands back: evaluation in the page."""

    def __init__(self, frame: "Frame") -> None:
        self._frame = frame

    @property
    def frame(self) -> "Frame":
        return self._frame

    async def evaluate(self, pageFunction: str, *args: Any, force_expr: bool = False) -> Any:
        return await self._frame.evaluate(pageFunction, *args, force_expr=force_expr)

    async def evaluateHandle(self, pageFunction: str, *args: Any,
                             force_expr: bool = False) -> JSHandle:
        return await self._frame.evaluateHandle(pageFunction, *args,
                                                force_expr=force_expr)

    async def queryObjects(self, prototypeHandle: JSHandle) -> JSHandle:
        raise errors.PyppeteerError("queryObjects is not supported: the engine "
                                    "has no heap walk")


class Frame:
    def __init__(self, page: "Page", frame_id: str) -> None:
        self._page = page
        self._id = frame_id
        self._execution_context = ExecutionContext(self)

    # ── identity ────────────────────────────────────────────────────────────
    @property
    def _engine(self):
        return self._page._engine

    @property
    def name(self) -> str:
        return ""

    @property
    def url(self) -> str:
        frame = self._engine.lifecycle.frame(self._id)
        return getattr(frame, "url", "") or ""

    @property
    def parentFrame(self) -> Optional["Frame"]:
        frame = self._engine.lifecycle.frame(self._id)
        parent = getattr(frame, "parent", None)
        return self._page._frame(parent) if parent else None

    @property
    def childFrames(self) -> List["Frame"]:
        return [self._page._frame(fid) for fid, f in
                list(self._engine.lifecycle.frames.items())
                if getattr(f, "parent", None) == self._id
                and fid not in self._page._gone]

    def isDetached(self) -> bool:
        return (self._id not in self._engine.lifecycle.frames
                or self._id in self._page._gone)

    async def executionContext(self) -> ExecutionContext:
        return self._execution_context

    # ── evaluation ──────────────────────────────────────────────────────────
    def _evaluate_sync(self, page_function: str, args, *, by_value: bool,
                       force_expr: bool) -> dict:
        """Run a page function in the page's world and return the raw result.

        ⛔ THE CALLER'S FUNCTION IS THE ONLY CODE THAT RUNS IN THE PAGE. The
        arguments go in as protocol values; an ElementHandle, which lives in
        the utility world, crosses with `Page.adoptNode`; the result is either
        serialized by the engine's sandbox (by value) or kept as a handle."""
        engine = self._engine
        inj = engine.injected

        def attempt():
            ctx = inj.main_context(self._id)
            owned: List[str] = []
            try:
                if force_expr or not _bridge.is_jsfunc(page_function):
                    return inj._result(engine.conn.send(
                        "Runtime.evaluate",
                        {"executionContextId": ctx, "expression": page_function,
                         "returnByValue": by_value},
                        session=engine.session, timeout=self._page._timeout_s))
                protocol_args = []
                for a in args:
                    if isinstance(a, ElementHandle):
                        if a._frame._page is not self._page or a._frame._id != self._id:
                            raise errors.ElementHandleError(
                                "JSHandles can be evaluated only in the context "
                                "they were created!")
                        oid = inj.node_to_main(self._id, a._oid)
                        if not oid:
                            raise errors.ElementHandleError(
                                "Node is detached from document")
                        owned.append(oid)
                        protocol_args.append({"objectId": oid})
                    elif isinstance(a, JSHandle):
                        protocol_args.append(a._as_argument())
                    else:
                        protocol_args.append(_bridge.plain_argument(a))
                return inj.call_raw(ctx, page_function, protocol_args,
                                    by_value=by_value,
                                    timeout=self._page._timeout_s)
            finally:
                for oid in owned:
                    inj.release(ctx, oid)
        return _bridge.across_navigation(attempt)

    async def evaluate(self, pageFunction: str, *args: Any, force_expr: bool = False) -> Any:
        """Evaluates a function (called with ``args``) or an expression in the
        page, and returns its JSON value. A returned promise is awaited."""
        remote = await _bridge.run(self._evaluate_sync, pageFunction, args,
                                   by_value=True, force_expr=force_expr)
        return _bridge.value_of(remote)

    async def evaluateHandle(self, pageFunction: str, *args: Any,
                             force_expr: bool = False) -> JSHandle:
        """Like evaluate, but returns a handle; a node comes back as an
        ElementHandle."""
        def run():
            remote = self._evaluate_sync(pageFunction, args, by_value=False,
                                         force_expr=force_expr)
            return self._handle_from_remote(remote)
        return await _bridge.run(run)

    def _handle_from_remote(self, remote: dict) -> JSHandle:
        """A handle for a page-world RemoteObject. Blocking for a node, which is
        carried into the utility world; call it off the loop."""
        if remote.get("subtype") == "node" and remote.get("objectId"):
            inj = self._engine.injected
            adopted = inj.adopt(self._id, None, remote["objectId"])
            inj.release(inj.main_context(self._id), remote["objectId"])
            if adopted:
                return ElementHandle(self, adopted)
        return JSHandle(self, remote.get("objectId") or "", remote)

    # ── finding ─────────────────────────────────────────────────────────────
    async def _query(self, root: Optional[str], by: str, value: str) -> List[ElementHandle]:
        def run():
            return _bridge.across_navigation(
                lambda: _bridge.query(self._engine, self._id, root, by, value))
        oids = await _bridge.run(run)
        return [ElementHandle(self, oid) for oid in oids]

    async def querySelector(self, selector: str) -> Optional[ElementHandle]:
        found = await self._query(None, "css", selector)
        return found[0] if found else None

    async def querySelectorAll(self, selector: str) -> List[ElementHandle]:
        return await self._query(None, "css", selector)

    async def xpath(self, expression: str) -> List[ElementHandle]:
        return await self._query(None, "xpath", expression)

    async def querySelectorEval(self, selector: str, pageFunction: str, *args: Any) -> Any:
        element = await self.querySelector(selector)
        if element is None:
            raise errors.ElementHandleError(
                'Error: failed to find element matching selector "%s"' % selector)
        return await self.evaluate(pageFunction, element, *args)

    async def querySelectorAllEval(self, selector: str, pageFunction: str, *args: Any) -> Any:
        elements = await self.querySelectorAll(selector)
        array = await self.evaluateHandle("(...els) => els", *elements)
        return await self.evaluate(pageFunction, array, *args)

    J = querySelector
    Jx = xpath
    Jeval = querySelectorEval
    JJ = querySelectorAll
    JJeval = querySelectorAllEval

    async def _element(self, selector: str) -> ElementHandle:
        element = await self.querySelector(selector)
        if element is None:
            raise errors.PageError("No node found for selector: " + selector)
        return element

    # ── the document ────────────────────────────────────────────────────────
    async def content(self) -> str:
        return await _bridge.run(lambda: _bridge.across_navigation(
            lambda: self._engine.injected.content(self._id)))

    async def setContent(self, html: str) -> None:
        """Replaces the document with ``html``.

        ⛔ Through `document.open/write/close` in the PAGE's world: Gecko wants
        `document.open()` under the document's own principal, and the utility
        world's extended principal is refused with "The operation is insecure"."""
        def run():
            self._engine.injected.evaluate_in_main(
                self._id, "(() => { document.open(); document.write(%s);"
                " document.close(); })()" % json.dumps(html), by_value=True)
            self._engine.lifecycle.wait_for_state(self._id, "load",
                                                  timeout=self._page._timeout_s)
        await _bridge.run(run)

    async def title(self) -> str:
        return await _bridge.run(lambda: _bridge.across_navigation(
            lambda: self._engine.injected.title(self._id))) or ""

    async def addScriptTag(self, options: Dict = None, **kwargs: str) -> ElementHandle:
        options = _bridge.merge(options, kwargs)
        content = options.get("content")
        if options.get("path"):
            with open(options["path"], "r", encoding="utf-8") as f:
                content = f.read() + "\n//# sourceURL=" + options["path"]
        return await self.evaluateHandle(
            "(url, content, type) => { const s = document.createElement('script');"
            " if (type) s.type = type; if (url) s.src = url; else s.text = content;"
            " document.head.appendChild(s); return s; }",
            options.get("url"), content, options.get("type"))

    async def addStyleTag(self, options: Dict = None, **kwargs: str) -> ElementHandle:
        options = _bridge.merge(options, kwargs)
        content = options.get("content")
        if options.get("path"):
            with open(options["path"], "r", encoding="utf-8") as f:
                content = f.read()
        return await self.evaluateHandle(
            "(url, content) => { let el; if (url) { el = document.createElement('link');"
            " el.rel = 'stylesheet'; el.href = url; } else {"
            " el = document.createElement('style'); el.textContent = content; }"
            " document.head.appendChild(el); return el; }",
            options.get("url"), content)

    # ── acting through a selector ───────────────────────────────────────────
    async def click(self, selector: str, options: dict = None, **kwargs: Any) -> None:
        element = await self._element(selector)
        await element.click(options, **kwargs)

    async def focus(self, selector: str) -> None:
        await (await self._element(selector)).focus()

    async def hover(self, selector: str) -> None:
        await (await self._element(selector)).hover()

    async def tap(self, selector: str) -> None:
        await (await self._element(selector)).tap()

    async def type(self, selector: str, text: str, options: dict = None, **kwargs: Any) -> None:
        await (await self._element(selector)).type(text, options, **kwargs)

    async def select(self, selector: str, *values: str) -> List[str]:
        """Chooses the options with these values, with trusted input/change
        events, and returns the values now selected."""
        element = await self._element(selector)
        tag = await element._read("(el) => (el.localName || '').toLowerCase()")
        if tag != "select":
            raise errors.ElementHandleError("Element is not a <select> element.")
        await _bridge.run(element._act, "select_option",
                          [{"value": str(v)} for v in values])
        return await element._read(
            "(el) => Array.from(el.options).filter(o => o.selected).map(o => o.value)")

    # ── waiting ─────────────────────────────────────────────────────────────
    async def waitForSelector(self, selector: str, options: dict = None, **kwargs: Any):
        return await self._wait_for_element("css", selector, _bridge.merge(options, kwargs))

    async def waitForXPath(self, xpath: str, options: dict = None, **kwargs: Any):
        return await self._wait_for_element("xpath", xpath, _bridge.merge(options, kwargs))

    async def _wait_for_element(self, by: str, value: str, options: dict):
        visible = bool(options.get("visible"))
        hidden = bool(options.get("hidden"))
        deadline = time.monotonic() + _bridge.ms(options)
        engine = self._engine
        while True:
            found = await self._query(None, by, value)
            element = found[0] if found else None
            shown = False
            if element is not None and (visible or hidden):
                shown = await _bridge.run(engine.injected.element_state,
                                          self._id, element._oid, "visible")
            if hidden:
                if element is None or not shown:
                    return None
            elif element is not None and (not visible or shown):
                return element
            if time.monotonic() > deadline:
                what = "selector" if by == "css" else "XPath"
                raise errors.TimeoutError(
                    'Waiting for %s "%s" failed: timeout %sms exceeds.'
                    % (what, value, int(options.get("timeout", 30000))))
            await asyncio.sleep(0.1)

    async def waitForFunction(self, pageFunction: str, options: dict = None,
                              *args: Any, **kwargs: Any) -> JSHandle:
        options = _bridge.merge(options, kwargs)
        polling = options.get("polling", "raf")
        interval = (float(polling) / 1000.0 if isinstance(polling, (int, float))
                    else 0.05)
        deadline = time.monotonic() + _bridge.ms(options)
        while True:
            try:
                handle = await self.evaluateHandle(pageFunction, *args)
                remote = handle._remote
                truthy = bool("objectId" in remote or _bridge.value_of(remote))
                if truthy:
                    return handle
            except errors.ElementHandleError:
                pass
            if time.monotonic() > deadline:
                raise errors.TimeoutError(
                    "Waiting for function failed: timeout %sms exceeds."
                    % int(options.get("timeout", 30000)))
            await asyncio.sleep(interval)

    async def waitFor(self, selectorOrFunctionOrTimeout: Union[str, int, float],
                      options: dict = None, *args: Any, **kwargs: Any):
        options = _bridge.merge(options, kwargs)
        target = selectorOrFunctionOrTimeout
        if isinstance(target, (int, float)):
            await asyncio.sleep(float(target) / 1000.0)
            return None
        if not isinstance(target, str):
            raise TypeError("Unsupported target type: " + str(type(target)))
        if _bridge.is_jsfunc(target):
            return await self.waitForFunction(target, options, *args)
        if target.startswith("//"):
            return await self.waitForXPath(target, options)
        return await self.waitForSelector(target, options)


class Keyboard:
    """The engine's keyboard: the session's typing rhythm, real key codes."""

    def __init__(self, page: "Page") -> None:
        self._page = page

    @property
    def _kb(self):
        return self._page._engine.actions.keyboard

    async def down(self, key: str, options: dict = None, **kwargs: Any) -> None:
        await _bridge.run(self._kb.down, key)

    async def up(self, key: str) -> None:
        await _bridge.run(self._kb.up, key)

    async def sendCharacter(self, char: str) -> None:
        await _bridge.run(self._kb.insert_text, char)

    async def type(self, text: str, options: Dict = None, **kwargs: Any) -> None:
        """Types text. Without ``delay`` the gaps between keys are the
        session's typing rhythm; with it, that many milliseconds each."""
        options = _bridge.merge(options, kwargs)
        delay = options.get("delay")
        actions = self._page._engine.actions
        if delay:
            await _bridge.run(self._kb.type, text, delay_ms=float(delay))
        else:
            await _bridge.run(actions._type, text)

    async def press(self, key: str, options: Dict = None, **kwargs: Any) -> None:
        options = _bridge.merge(options, kwargs)
        await _bridge.run(self._kb.press, key, dwell_ms=options.get("delay"))


class Mouse:
    """The engine's pointer. A move travels along a path drawn from the
    session seed, whatever ``steps`` says: a straight line in N equal steps is
    exactly the movement a page can recognise as not made by a hand."""

    def __init__(self, page: "Page") -> None:
        self._page = page

    @property
    def _actions(self):
        return self._page._engine.actions

    async def move(self, x: float, y: float, options: dict = None, **kwargs: Any) -> None:
        await self._page._slow()
        await _bridge.run(self._actions.glide_to, float(x), float(y))

    async def click(self, x: float, y: float, options: dict = None, **kwargs: Any) -> None:
        options = _bridge.merge(options, kwargs)
        await self._page._slow()

        def run():
            a = self._actions
            a.glide_to(float(x), float(y))
            a._click_at_point((float(x), float(y)),
                              button=_bridge.button(options.get("button")),
                              clicks=int(options.get("clickCount") or 1),
                              delay_ms=options.get("delay"))
        await _bridge.run(run)

    async def down(self, options: dict = None, **kwargs: Any) -> None:
        options = _bridge.merge(options, kwargs)
        await _bridge.run(self._actions.mouse_down,
                          button=_bridge.button(options.get("button")),
                          clicks=int(options.get("clickCount") or 1))

    async def up(self, options: dict = None, **kwargs: Any) -> None:
        options = _bridge.merge(options, kwargs)
        await _bridge.run(self._actions.mouse_up,
                          button=_bridge.button(options.get("button")),
                          clicks=int(options.get("clickCount") or 1))


class Touchscreen:
    def __init__(self, page: "Page") -> None:
        self._page = page

    async def tap(self, x: float, y: float) -> None:
        await _bridge.run(lambda: self._page._engine.send(
            "Page.dispatchTapEvent", {"x": float(x), "y": float(y), "modifiers": 0}))


class Dialog:
    """A JavaScript dialog, answered with the engine's `Page.handleDialog`."""

    Type = {"alert": "alert", "beforeunload": "beforeunload",
            "confirm": "confirm", "prompt": "prompt"}

    def __init__(self, page: "Page", params: dict) -> None:
        self._page = page
        self._id = params["dialogId"]
        self._type = params.get("type") or "alert"
        self._message = params.get("message") or ""
        self._default = params.get("defaultValue") or ""
        self._handled = False

    @property
    def type(self) -> str:
        return self._type

    @property
    def message(self) -> str:
        return self._message

    @property
    def defaultValue(self) -> str:
        return self._default

    async def _answer(self, accept: bool, text: Optional[str]) -> None:
        if self._handled:
            raise errors.PyppeteerError("Cannot dismiss dialog which is already handled!")
        self._handled = True
        params = {"dialogId": self._id, "accept": accept}
        if accept and text:
            params["promptText"] = text
        await _bridge.run(self._page._engine.send, "Page.handleDialog", params)

    async def accept(self, promptText: str = "") -> None:
        await self._answer(True, promptText)

    async def dismiss(self) -> None:
        await self._answer(False, None)


class ConsoleMessage:
    def __init__(self, type: str, text: str, args: List[JSHandle] = None) -> None:
        self._type = type
        self._text = text
        self._args = list(args or [])

    @property
    def type(self) -> str:
        return self._type

    @property
    def text(self) -> str:
        return self._text

    @property
    def args(self) -> List[JSHandle]:
        return self._args


class Request:
    def __init__(self, page: "Page", params: dict) -> None:
        self._page = page
        self._id = params.get("requestId")
        self._url = params.get("url") or ""
        self._method = params.get("method") or "GET"
        self._headers = {h["name"].lower(): h["value"]
                         for h in (params.get("headers") or [])}
        self._navigation_id = params.get("navigationId")
        self._frame_id = params.get("frameId")
        self._cause = params.get("cause") or ""
        self._response: Optional[Response] = None
        self._failure: Optional[str] = None
        self._finished = False

    @property
    def url(self) -> str:
        return self._url

    @property
    def method(self) -> str:
        return self._method

    @property
    def headers(self) -> Dict:
        return dict(self._headers)

    @property
    def postData(self) -> Optional[str]:
        return None

    @property
    def resourceType(self) -> str:
        cause = self._cause.upper()
        for key, name in (("DOCUMENT", "document"), ("SCRIPT", "script"),
                          ("STYLESHEET", "stylesheet"), ("IMAGE", "image"),
                          ("FONT", "font"), ("XMLHTTPREQUEST", "xhr"),
                          ("FETCH", "fetch"), ("MEDIA", "media"),
                          ("WEBSOCKET", "websocket")):
            if key in cause:
                return name
        return "other"

    @property
    def response(self) -> Optional["Response"]:
        return self._response

    @property
    def frame(self) -> Optional[Frame]:
        return self._page._frame(self._frame_id) if self._frame_id else None

    def isNavigationRequest(self) -> bool:
        return bool(self._navigation_id)

    @property
    def redirectChain(self) -> List["Request"]:
        return []

    def failure(self) -> Optional[Dict]:
        return {"errorText": self._failure} if self._failure else None

    async def continue_(self, overrides: Dict = None) -> None:
        raise errors.NetworkError("Request interception is not enabled!")

    async def respond(self, response: Dict) -> None:
        raise errors.NetworkError("Request interception is not enabled!")

    async def abort(self, errorCode: str = "failed") -> None:
        raise errors.NetworkError("Request interception is not enabled!")


class Response:
    def __init__(self, request: Request, params: dict) -> None:
        self._request = request
        self._status = int(params.get("status") or 0)
        self._status_text = params.get("statusText") or ""
        self._headers = {h["name"].lower(): h["value"]
                         for h in (params.get("headers") or [])}
        self._from_cache = bool(params.get("fromCache"))
        self._from_sw = bool(params.get("fromServiceWorker"))

    @property
    def url(self) -> str:
        return self._request.url

    @property
    def ok(self) -> bool:
        return self._status == 0 or 200 <= self._status <= 299

    @property
    def status(self) -> int:
        return self._status

    @property
    def headers(self) -> Dict:
        return dict(self._headers)

    @property
    def securityDetails(self):
        return None

    @property
    def request(self) -> Request:
        return self._request

    @property
    def fromCache(self) -> bool:
        return self._from_cache

    @property
    def fromServiceWorker(self) -> bool:
        return self._from_sw

    async def buffer(self) -> bytes:
        """The body, from the engine (`Network.getResponseBody`), once the
        response has finished arriving."""
        page = self._request._page

        def run():
            deadline = time.monotonic() + 10.0
            while not self._request._finished and time.monotonic() < deadline:
                time.sleep(0.02)
            answer = page._engine.send("Network.getResponseBody",
                                       {"requestId": self._request._id}) or {}
            if answer.get("evicted"):
                raise errors.NetworkError(
                    "Response body is unavailable: the engine evicted it")
            return base64.b64decode(answer.get("base64body") or "")
        return await _bridge.run(run)

    async def text(self) -> str:
        return (await self.buffer()).decode("utf-8", errors="replace")

    async def json(self) -> dict:
        return json.loads(await self.text())


class Page(EventEmitter):
    """A tab. Same methods and events as pyppeteer's Page."""

    Events = type("Events", (), {
        "Close": "close", "Console": "console", "Dialog": "dialog",
        "DOMContentLoaded": "domcontentloaded", "Error": "error",
        "PageError": "pageerror", "Request": "request", "Response": "response",
        "RequestFailed": "requestfailed", "RequestFinished": "requestfinished",
        "FrameAttached": "frameattached", "FrameDetached": "framedetached",
        "FrameNavigated": "framenavigated", "Load": "load", "Popup": "popup"})

    def __init__(self, browser, context, engine_page, loop) -> None:
        super().__init__(loop)
        self._browser = browser
        self._context = context
        self._engine = engine_page
        self._frames: Dict[str, Frame] = {}
        self._closed = False
        self._timeout_s = 30.0
        self._navigation_timeout_s = 30.0
        self._action_timeout = 30.0
        self._viewport: Optional[dict] = None
        self._requests: Dict[str, Request] = {}
        self._navigations: Dict[str, Request] = {}
        #: Frames of documents this page has left. See `_forget_children`.
        self._gone: set = set()
        self.keyboard = Keyboard(self)
        self.mouse = Mouse(self)
        self.touchscreen = Touchscreen(self)
        engine_page.on_event(self._on_event)

    # ── plumbing ────────────────────────────────────────────────────────────
    def _frame(self, frame_id: str) -> Frame:
        frame = self._frames.get(frame_id)
        if frame is None:
            frame = self._frames[frame_id] = Frame(self, frame_id)
        return frame

    async def _slow(self) -> None:
        slow_mo = getattr(self._browser, "_slow_mo", 0)
        if slow_mo:
            await asyncio.sleep(slow_mo / 1000.0)

    async def _viewport_now(self) -> dict:
        return await _bridge.run(lambda: self._engine.injected.evaluate(
            self._engine.main_frame_id,
            "({width: window.innerWidth, height: window.innerHeight})"))

    def _on_event(self, method: str, params: dict) -> None:
        """On the pipe's reader thread: record, and hand events to the loop.

        ⛔ NOTHING BLOCKING HERE. A `send` from this thread waits for a reply
        only this thread can deliver; answers go out with `post`."""
        main = self._engine.main_frame_id
        if method == "Page.dialogOpened":
            dialog = Dialog(self, params)
            if self.listener_count("dialog"):
                self.emit_threadsafe("dialog", dialog)
            else:
                # ⛔ A DIALOG NOBODY LISTENS FOR IS DISMISSED, which is
                # Playwright's rule and not Puppeteer's: there the page stays
                # frozen inside `alert()` until the script times out. No
                # program depends on the freeze, and every one is hurt by it.
                self._engine.conn.post(
                    "Page.handleDialog",
                    {"dialogId": params["dialogId"],
                     "accept": params.get("type") == "beforeunload"},
                    session=self._engine.session)
        elif method == "Page.eventFired" and params.get("frameId") == main:
            name = {"load": "load",
                    "DOMContentLoaded": "domcontentloaded"}.get(params.get("name"))
            if name:
                self.emit_threadsafe(name)
        elif method == "Page.navigationCommitted":
            self._forget_children(params["frameId"])
            self.emit_threadsafe("framenavigated", self._frame(params["frameId"]))
        elif method == "Page.frameAttached":
            self.emit_threadsafe("frameattached", self._frame(params["frameId"]))
        elif method == "Page.frameDetached":
            frame = self._frames.pop(params.get("frameId"), None)
            if frame is not None:
                self.emit_threadsafe("framedetached", frame)
        elif method == "Runtime.console":
            args = params.get("args") or []
            text = " ".join(str(_bridge.value_of(a)) if "objectId" not in a
                            else "JSHandle@" + (a.get("subtype") or a.get("type") or "object")
                            for a in args)
            frame_id = self._engine.injected.frame_of_context(
                params.get("executionContextId")) or main
            frame = self._frame(frame_id)
            handles = [JSHandle(frame, a.get("objectId") or "", a) for a in args]
            self.emit_threadsafe("console", ConsoleMessage(
                params.get("type") or "log", text, handles))
        elif method == "Page.uncaughtError":
            self.emit_threadsafe("pageerror", errors.PageError(
                params.get("message") or ""))
        elif method == "Page.crashed":
            self.emit_threadsafe("error", errors.PageError("Page crashed!"))
        elif method == "Network.requestWillBeSent":
            request = Request(self, params)
            self._requests[request._id] = request
            if request._navigation_id:
                self._navigations[request._navigation_id] = request
                while len(self._navigations) > 200:
                    del self._navigations[next(iter(self._navigations))]
            self.emit_threadsafe("request", request)
        elif method == "Network.responseReceived":
            request = self._requests.get(params.get("requestId"))
            if request is not None:
                request._response = Response(request, params)
                self.emit_threadsafe("response", request._response)
        elif method == "Network.requestFinished":
            request = self._requests.pop(params.get("requestId"), None)
            if request is not None:
                request._finished = True
                self.emit_threadsafe("requestfinished", request)
        elif method == "Network.requestFailed":
            request = self._requests.pop(params.get("requestId"), None)
            if request is not None:
                request._finished = True
                request._failure = params.get("errorCode") or "failed"
                self.emit_threadsafe("requestfailed", request)

    def _forget_children(self, frame_id: str) -> None:
        """A frame that commits a new document has none of its old children.

        ⛔ MEASURED, NOT ASSUMED: after a history navigation served from the
        back-forward cache, the engine never sent `Page.frameDetached` for the
        old document's iframe, so the frame stayed in the lifecycle with a
        world that no longer existed, and a query in it waited its full
        timeout. The children a frame has at the moment it commits belong to
        the document being replaced; the new document's frames attach after."""
        frames = self._engine.lifecycle.frames
        stack = [frame_id]
        while stack:
            parent = stack.pop()
            for fid, f in list(frames.items()):
                if getattr(f, "parent", None) == parent and fid not in self._gone:
                    self._gone.add(fid)
                    self._frames.pop(fid, None)
                    stack.append(fid)

    def _navigation_response(self, navigation_id: Optional[str]) -> Optional[Response]:
        if not navigation_id:
            return None
        request = self._navigations.get(navigation_id)
        return request._response if request is not None else None

    # ── identity ────────────────────────────────────────────────────────────
    @property
    def browser(self):
        return self._browser

    @property
    def target(self):
        return self

    @property
    def mainFrame(self) -> Frame:
        return self._frame(self._engine.main_frame_id)

    @property
    def frames(self) -> List[Frame]:
        return [self._frame(fid) for fid in list(self._engine.lifecycle.frames)
                if fid not in self._gone]

    @property
    def workers(self) -> list:
        return []

    @property
    def url(self) -> str:
        return self.mainFrame.url

    @property
    def viewport(self) -> Optional[Dict]:
        if self._viewport is not None:
            return dict(self._viewport)
        vp = (self._context._engine.options or {}).get("viewport")
        return dict(vp) if vp else None

    def isClosed(self) -> bool:
        return self._closed

    def setDefaultNavigationTimeout(self, timeout: int) -> None:
        self._navigation_timeout_s = _bridge.ms({"timeout": timeout})

    def setDefaultTimeout(self, timeout: int) -> None:
        self._timeout_s = _bridge.ms({"timeout": timeout})
        self._action_timeout = self._timeout_s

    # ── delegated to the main frame ─────────────────────────────────────────
    async def querySelector(self, selector: str) -> Optional[ElementHandle]:
        return await self.mainFrame.querySelector(selector)

    async def querySelectorAll(self, selector: str) -> List[ElementHandle]:
        return await self.mainFrame.querySelectorAll(selector)

    async def querySelectorEval(self, selector: str, pageFunction: str, *args: Any) -> Any:
        return await self.mainFrame.querySelectorEval(selector, pageFunction, *args)

    async def querySelectorAllEval(self, selector: str, pageFunction: str, *args: Any) -> Any:
        return await self.mainFrame.querySelectorAllEval(selector, pageFunction, *args)

    async def xpath(self, expression: str) -> List[ElementHandle]:
        return await self.mainFrame.xpath(expression)

    J = querySelector
    Jeval = querySelectorEval
    JJ = querySelectorAll
    JJeval = querySelectorAllEval
    Jx = xpath

    async def evaluate(self, pageFunction: str, *args: Any, force_expr: bool = False) -> Any:
        return await self.mainFrame.evaluate(pageFunction, *args, force_expr=force_expr)

    async def evaluateHandle(self, pageFunction: str, *args: Any) -> JSHandle:
        return await self.mainFrame.evaluateHandle(pageFunction, *args)

    async def content(self) -> str:
        return await self.mainFrame.content()

    async def setContent(self, html: str) -> None:
        await self.mainFrame.setContent(html)

    async def title(self) -> str:
        return await self.mainFrame.title()

    async def plainText(self) -> str:
        return await self.evaluate("() => document.body.innerText")

    async def addScriptTag(self, options: Dict = None, **kwargs: str) -> ElementHandle:
        return await self.mainFrame.addScriptTag(options, **kwargs)

    async def addStyleTag(self, options: Dict = None, **kwargs: str) -> ElementHandle:
        return await self.mainFrame.addStyleTag(options, **kwargs)

    async def click(self, selector: str, options: dict = None, **kwargs: Any) -> None:
        await self.mainFrame.click(selector, options, **kwargs)

    async def hover(self, selector: str) -> None:
        await self.mainFrame.hover(selector)

    async def focus(self, selector: str) -> None:
        await self.mainFrame.focus(selector)

    async def tap(self, selector: str) -> None:
        await self.mainFrame.tap(selector)

    async def select(self, selector: str, *values: str) -> List[str]:
        return await self.mainFrame.select(selector, *values)

    async def type(self, selector: str, text: str, options: dict = None, **kwargs: Any) -> None:
        await self.mainFrame.type(selector, text, options, **kwargs)

    def waitForSelector(self, selector: str, options: dict = None, **kwargs: Any) -> Awaitable:
        return self.mainFrame.waitForSelector(selector, options, **kwargs)

    def waitForXPath(self, xpath: str, options: dict = None, **kwargs: Any) -> Awaitable:
        return self.mainFrame.waitForXPath(xpath, options, **kwargs)

    def waitForFunction(self, pageFunction: str, options: dict = None,
                        *args: str, **kwargs: Any) -> Awaitable:
        return self.mainFrame.waitForFunction(pageFunction, options, *args, **kwargs)

    def waitFor(self, selectorOrFunctionOrTimeout, options: dict = None,
                *args: Any, **kwargs: Any) -> Awaitable:
        return self.mainFrame.waitFor(selectorOrFunctionOrTimeout, options,
                                      *args, **kwargs)

    # ── navigation ──────────────────────────────────────────────────────────
    async def goto(self, url: str, options: dict = None, **kwargs: Any) -> Optional[Response]:
        """Navigates and waits for ``waitUntil`` (default ``load``); answers
        with the main document's Response."""
        options = _bridge.merge(options, kwargs)
        state = _state(options.get("waitUntil"))
        timeout = (_bridge.ms(options) if "timeout" in options
                   else self._navigation_timeout_s)
        engine = self._engine

        def run():
            try:
                return engine.lifecycle.goto(url, frame_id=engine.main_frame_id,
                                             until=state, timeout=timeout,
                                             referer=options.get("referer"))
            except NavigationError as e:
                raise errors.PageError("%s at %s" % (e, url)) from None
            except TimeoutError:
                raise errors.TimeoutError(
                    "Navigation Timeout Exceeded: %s ms exceeded."
                    % int(timeout * 1000)) from None
        result = await _bridge.run(run)
        return self._navigation_response(result.get("navigationId"))

    async def _history(self, command: str, options: dict) -> Optional[Response]:
        state = _state(options.get("waitUntil"))
        timeout = (_bridge.ms(options) if "timeout" in options
                   else self._navigation_timeout_s)
        engine = self._engine

        def run():
            fid = engine.main_frame_id
            frame = engine.lifecycle.frame(fid)
            previous = frame.navigation if frame is not None else None
            result = engine.send(command, {"frameId": fid}
                                 if command != "Page.reload" else {}) or {}
            if command != "Page.reload" and not result.get("success"):
                return None
            try:
                return engine.lifecycle.wait_for_new_navigation(
                    fid, previous, state, timeout=timeout)
            except TimeoutError:
                # A same-document entry (a hash, a pushState) changes the
                # address without a new navigation: nothing to wait for.
                return None
        navigation = await _bridge.run(run)
        return self._navigation_response(navigation)

    async def reload(self, options: dict = None, **kwargs: Any) -> Optional[Response]:
        return await self._history("Page.reload", _bridge.merge(options, kwargs))

    async def goBack(self, options: dict = None, **kwargs: Any) -> Optional[Response]:
        return await self._history("Page.goBack", _bridge.merge(options, kwargs))

    async def goForward(self, options: dict = None, **kwargs: Any) -> Optional[Response]:
        return await self._history("Page.goForward", _bridge.merge(options, kwargs))

    async def waitForNavigation(self, options: dict = None, **kwargs: Any) -> Optional[Response]:
        """Waits for the NEXT navigation of the main frame, started after this
        call - the pattern is `asyncio.gather(page.waitForNavigation(),
        page.click(...))`, as in pyppeteer."""
        options = _bridge.merge(options, kwargs)
        state = _state(options.get("waitUntil"))
        timeout = (_bridge.ms(options) if "timeout" in options
                   else self._navigation_timeout_s)
        engine = self._engine
        fid = engine.main_frame_id
        frame = engine.lifecycle.frame(fid)
        previous = frame.navigation if frame is not None else None

        def run():
            try:
                return engine.lifecycle.wait_for_new_navigation(
                    fid, previous, state, timeout=timeout)
            except TimeoutError:
                raise errors.TimeoutError(
                    "Navigation Timeout Exceeded: %s ms exceeded."
                    % int(timeout * 1000)) from None
        return self._navigation_response(await _bridge.run(run))

    async def _wait_for_event(self, event: str, predicate, options: dict):
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        def listener(obj):
            try:
                if predicate(obj) and not future.done():
                    future.set_result(obj)
            except Exception as e:
                if not future.done():
                    future.set_exception(e)
        self.on(event, listener)
        try:
            return await asyncio.wait_for(future, _bridge.ms(options))
        except asyncio.TimeoutError:
            raise errors.TimeoutError("Timeout exceeded while waiting for event") from None
        finally:
            self.remove_listener(event, listener)

    async def waitForRequest(self, urlOrPredicate, options: dict = None, **kwargs: Any) -> Request:
        options = _bridge.merge(options, kwargs)
        match = ((lambda r: r.url == urlOrPredicate) if isinstance(urlOrPredicate, str)
                 else urlOrPredicate)
        return await self._wait_for_event("request", match, options)

    async def waitForResponse(self, urlOrPredicate, options: dict = None, **kwargs: Any) -> Response:
        options = _bridge.merge(options, kwargs)
        match = ((lambda r: r.url == urlOrPredicate) if isinstance(urlOrPredicate, str)
                 else urlOrPredicate)
        return await self._wait_for_event("response", match, options)

    # ── cookies ─────────────────────────────────────────────────────────────
    async def cookies(self, *urls: str) -> List[Dict[str, Union[str, int, bool]]]:
        """The cookies for these URLs (the page's URL when none is given)."""
        urls = urls or (self.url,)
        engine_context = self._context._engine
        all_cookies = await _bridge.run(engine_context.cookies)
        out = []
        for c in all_cookies:
            for url in urls:
                if _domain_matches(c.get("domain") or "", _host_of(url)):
                    item = dict(c)
                    item.setdefault("size", len(c.get("name", "")) + len(c.get("value", "")))
                    out.append(item)
                    break
        return out

    async def setCookie(self, *cookies: dict) -> None:
        page_url = self.url
        items = []
        for cookie in cookies:
            item = dict(cookie)
            if "name" not in item or "value" not in item:
                raise errors.PageError("A cookie needs a name and a value")
            if not item.get("url") and not item.get("domain"):
                if not page_url.startswith("http"):
                    raise errors.PageError(
                        'At least one of the url and domain needs to be specified')
                item["url"] = page_url
            if item.get("url") and item["url"].startswith("about:"):
                raise errors.PageError('Blank page can not have cookie "%s"' % item["name"])
            # ⛔ A session cookie goes WITHOUT `expires`: Juggler turns -1 into
            # an expiry in 1969 and drops the cookie silently (70-known-bugs
            # [B224] in the workbench).
            if item.get("expires") in (None, -1):
                item.pop("expires", None)
            items.append(item)
        await _bridge.run(self._context._engine.set_cookies, items)

    async def deleteCookie(self, *cookies: dict) -> None:
        """Deletes cookies by expiring them: Juggler can only clear a whole
        context, which is more than this call asks for."""
        current = await self.cookies()
        doomed = []
        for spec in cookies:
            for c in current:
                if c.get("name") != spec.get("name"):
                    continue
                if spec.get("domain") and spec["domain"] != c.get("domain"):
                    continue
                if spec.get("path") and spec["path"] != c.get("path"):
                    continue
                doomed.append({"name": c["name"], "value": "",
                               "domain": c.get("domain"), "path": c.get("path"),
                               "expires": 1})
        if doomed:
            await _bridge.run(self._context._engine.set_cookies, doomed)

    # ── screenshots ─────────────────────────────────────────────────────────
    async def screenshot(self, options: dict = None, **kwargs: Any) -> Union[bytes, str]:
        options = _bridge.merge(options, kwargs)
        kind = options.get("type")
        if kind is None and options.get("path"):
            mime, _ = mimetypes.guess_type(options["path"])
            kind = {"image/png": "png", "image/jpeg": "jpeg"}.get(mime)
            if kind is None:
                raise ValueError("Unsupported screenshot mime type: %s" % mime)
        kind = kind or "png"
        if kind not in ("png", "jpeg"):
            raise ValueError("Unknown type value: %s" % kind)
        engine = self._engine

        def run():
            clip = options.get("clip")
            if not clip:
                # ⛔ DOCUMENT COORDINATES: the viewport is placed where the page
                # is scrolled to, or the image is always the top of the page.
                box = engine.injected.evaluate(
                    engine.main_frame_id,
                    "({x: window.scrollX, y: window.scrollY,"
                    " width: window.innerWidth, height: window.innerHeight,"
                    " fullWidth: Math.max(document.documentElement.scrollWidth,"
                    " document.body ? document.body.scrollWidth : 0),"
                    " fullHeight: Math.max(document.documentElement.scrollHeight,"
                    " document.body ? document.body.scrollHeight : 0)})")
                if options.get("fullPage"):
                    clip = {"x": 0, "y": 0, "width": box["fullWidth"],
                            "height": box["fullHeight"]}
                else:
                    clip = {"x": box["x"], "y": box["y"], "width": box["width"],
                            "height": box["height"]}
            params = {"mimeType": "image/" + kind, "clip": clip,
                      "omitDeviceScaleFactor": False}
            if kind == "jpeg" and options.get("quality") is not None:
                params["quality"] = int(options["quality"])
            return (engine.send("Page.screenshot", params) or {}).get("data") or ""
        data = await _bridge.run(run)
        if options.get("encoding") == "base64":
            return data
        raw = base64.b64decode(data)
        if options.get("path"):
            with open(options["path"], "wb") as f:
                f.write(raw)
        return raw

    # ── emulation, headers, the context ─────────────────────────────────────
    async def setViewport(self, viewport: dict) -> None:
        """Resizes the viewport.

        ⛔ The profile already declares a viewport that agrees with its screen;
        a different one is the caller's choice, and it is applied as asked."""
        self._viewport = dict(viewport)
        await _bridge.run(self._engine.send, "Page.setViewportSize",
                          {"viewportSize": {"width": int(viewport["width"]),
                                            "height": int(viewport["height"])}})

    async def emulate(self, options: dict = None, **kwargs: Any) -> None:
        options = _bridge.merge(options, kwargs)
        if options.get("viewport"):
            await self.setViewport(options["viewport"])
        if options.get("userAgent"):
            await self.setUserAgent(options["userAgent"])

    async def emulateMedia(self, mediaType: str = None) -> None:
        await _bridge.run(self._engine.send, "Page.setEmulatedMedia",
                          {"type": mediaType or ""})

    async def setUserAgent(self, userAgent: str) -> None:
        """⛔ Applies to the whole context (Juggler overrides per context), and
        a User-Agent that disagrees with the profile is a contradiction a page
        can read - the caller's call, applied as asked."""
        await _bridge.run(self._context._engine.send,
                          "Browser.setUserAgentOverride", {"userAgent": userAgent})

    async def setExtraHTTPHeaders(self, headers: Dict[str, str]) -> None:
        await _bridge.run(self._context._engine.send, "Browser.setExtraHTTPHeaders",
                          {"headers": [{"name": k, "value": str(v)}
                                       for k, v in headers.items()]})

    async def authenticate(self, credentials: Dict[str, str]) -> Any:
        await _bridge.run(self._context._engine.send, "Browser.setHTTPCredentials",
                          {"credentials": credentials})

    async def setJavaScriptEnabled(self, enabled: bool) -> None:
        await _bridge.run(self._context._engine.send, "Browser.setJavaScriptDisabled",
                          {"javaScriptDisabled": not enabled})

    async def setBypassCSP(self, enabled: bool) -> None:
        await _bridge.run(self._context._engine.send, "Browser.setBypassCSP",
                          {"bypassCSP": bool(enabled)})

    async def setOfflineMode(self, enabled: bool) -> None:
        await _bridge.run(self._context._engine.send, "Browser.setOnlineOverride",
                          {"override": "offline" if enabled else "online"})

    async def setCacheEnabled(self, enabled: bool = True) -> None:
        await _bridge.run(self._engine.send, "Page.setCacheDisabled",
                          {"cacheDisabled": not enabled})

    async def evaluateOnNewDocument(self, pageFunction: str, *args: str) -> None:
        source = ("(%s)(%s)" % (pageFunction, ", ".join(json.dumps(a) for a in args))
                  if _bridge.is_jsfunc(pageFunction) else pageFunction)
        await _bridge.run(self._engine.injected.add_init_script, source)

    async def setRequestInterception(self, value: bool) -> None:
        if value:
            raise errors.PyppeteerError(
                "setRequestInterception(True) is not supported yet")

    async def exposeFunction(self, name: str, pyppeteerFunction: Callable[..., Any]) -> None:
        raise errors.PyppeteerError("exposeFunction is not supported yet")

    async def pdf(self, options: dict = None, **kwargs: Any) -> bytes:
        raise errors.PyppeteerError("pdf is not supported by this browser")

    async def metrics(self) -> Dict[str, Any]:
        raise errors.PyppeteerError("metrics is not supported by this browser")

    async def queryObjects(self, prototypeHandle: JSHandle) -> JSHandle:
        raise errors.PyppeteerError("queryObjects is not supported by this browser")

    @property
    def coverage(self):
        raise errors.PyppeteerError("coverage is not supported by this browser")

    @property
    def tracing(self):
        raise errors.PyppeteerError("tracing is not supported by this browser")

    # ── the end ─────────────────────────────────────────────────────────────
    async def bringToFront(self) -> None:
        await _bridge.run(self._engine.send, "Page.bringToFront", {})

    async def close(self, options: Dict = None, **kwargs: Any) -> None:
        if self._closed:
            return
        self._closed = True
        await _bridge.run(self._engine.close)
        self.emit("close")


__all__ = ["Page", "Frame", "ExecutionContext", "Keyboard", "Mouse",
           "Touchscreen", "Dialog", "ConsoleMessage", "Request", "Response"]
