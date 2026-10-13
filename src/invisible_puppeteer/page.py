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
import logging
import mimetypes
import time
import urllib.parse
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

from . import _bridge, errors
from ._events import EventEmitter
from invisible_core.juggler.deadline import Deadline
from invisible_core.juggler.injected import ContextDestroyed
from invisible_core.juggler.lifecycle import NavigationError
from .element_handle import ElementHandle, JSHandle

#: How every navigation wait here is decided: pyppeteer's `NavigatorWatcher`
#: completes when the frame carries a document other than the one it had when
#: the wait began and that document and every child frame have the expected
#: events (`_checkLifecycleComplete`, `_checkLifecycle`). It never says
#: interrupted. The core counts only the children that have started loading,
#: puppeteer's own later rule: a lazy iframe out of view never starts.
_FOLLOW = {"replaced": "follow", "children": True}

#: pyppeteer's `waitUntil` values (`pyppeteerToProtocolLifecycle`), as the
#: engine's load states. `documentloaded` is pyppeteer's old name, accepted
#: with a warning. `networkidle2` is the engine's own networkidle2, "at most
#: two requests for 500 ms" as in pyppeteer; until 0.5.0 it was networkidle,
#: none in flight, and a page that keeps two requests open never reached it.
_WAIT_UNTIL = {"load": "load", "domcontentloaded": "domcontentloaded",
               "documentloaded": "domcontentloaded",
               "networkidle0": "networkidle", "networkidle2": "networkidle2"}
#: networkidle implies networkidle2 (none in flight is at most two), so it is
#: the later of the two.
_ORDER = ["commit", "domcontentloaded", "load", "networkidle2", "networkidle"]


def _state(options: dict) -> str:
    """One engine state for a navigation's options, validated as pyppeteer's
    `NavigatorWatcher._validate_options` does, before anything is sent: the
    same errors in the same order, and the latest of the states asked for,
    since waiting for it waits for the others too."""
    if "networkIdleTimeout" in options:
        raise ValueError("`networkIdleTimeout` option is no longer supported.")
    if "networkIdleInflight" in options:
        raise ValueError("`networkIdleInflight` option is no longer supported.")
    if options.get("waitUntil") == "networkidle":
        # pyppeteer's words, its spelling included.
        raise ValueError("`networkidle` option is no logner supported. "
                         "Use `networkidle2` instead.")
    if options.get("waitUntil") == "documentloaded":
        logging.getLogger(__name__).warning(
            "`documentloaded` option is no longer supported. "
            "Use `domcontentloaded` instead.")
    wait_until = options.get("waitUntil", "load")
    if isinstance(wait_until, list):
        names = wait_until
    elif isinstance(wait_until, str):
        names = [wait_until]
    else:
        raise TypeError("`waitUntil` option should be str or list of str, "
                        "but got type %s" % type(wait_until))
    states = []
    for name in names:
        if name not in _WAIT_UNTIL:
            raise ValueError("Unknown value for options.waitUntil: %s" % name)
        states.append(_WAIT_UNTIL[name])
    return max(states, key=_ORDER.index)


def _sent_to(cookie: dict, url: str) -> bool:
    """Would this cookie be sent to `url`? RFC 6265 section 5.4, which is how
    Chrome's `Network.getCookies` and `Network.deleteCookies` read a URL for
    pyppeteer: a host-only cookie on its host alone and a domain cookie
    (leading dot) on it and its subdomains; the path equal or a prefix that
    ends at a '/'; a `secure` cookie only to https."""
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    path = parts.path or "/"
    domain = (cookie.get("domain") or "").lower()
    if domain.startswith("."):
        if host != domain[1:] and not host.endswith(domain):
            return False
    elif host != domain:
        return False
    cpath = cookie.get("path") or "/"
    if path != cpath and not (path.startswith(cpath) and
                              (cpath.endswith("/") or path[len(cpath)] == "/")):
        return False
    return not (cookie.get("secure") and parts.scheme != "https")


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
        #: The lifecycle's frame, held: it outlives the frame's detachment,
        #: so a detached frame still says its url and name, as pyppeteer's.
        self._known = page._engine.lifecycle.frame(frame_id)

    def _current(self):
        """The lifecycle's frame while it is attached, else the last one."""
        frame = self._engine.lifecycle.frame(self._id)
        if frame is not None:
            self._known = frame
        return self._known

    # ── identity ────────────────────────────────────────────────────────────
    @property
    def _engine(self):
        return self._page._engine

    @property
    def name(self) -> str:
        return getattr(self._current(), "name", "") or ""

    @property
    def url(self) -> str:
        return getattr(self._current(), "url", "") or ""

    @property
    def parentFrame(self) -> Optional["Frame"]:
        frame = self._engine.lifecycle.frame(self._id)
        parent = getattr(frame, "parent", None)
        return self._page._frame(parent) if parent else None

    @property
    def childFrames(self) -> List["Frame"]:
        return [self._page._frame(fid) for fid, f in
                list(self._engine.lifecycle.frames.items())
                if getattr(f, "parent", None) == self._id]

    def isDetached(self) -> bool:
        return self._id not in self._engine.lifecycle.frames

    async def executionContext(self) -> ExecutionContext:
        return self._execution_context

    # ── evaluation ──────────────────────────────────────────────────────────
    def _evaluate_sync(self, page_function: str, args, *, by_value: bool,
                       force_expr: bool) -> dict:
        """Run a page function in the page's world and return the raw result.

        ⛔ THE CALLER'S FUNCTION IS THE ONLY CODE THAT RUNS IN THE PAGE. The
        arguments go in as protocol values; an ElementHandle, which lives in
        the utility world, crosses with `Page.adoptNode`; the result is either
        serialized by the engine's sandbox (by value) or kept as a handle.

        ⛔ AND IT IS SENT ONCE. A world that went under it does not say whether
        it ran (a function waiting on a promise when its page navigated is
        refused by the new page in the same words as one that never ran), and
        until 0.3.7 it was sent again into the new page, which ran it twice
        ([B285]). pyppeteer raises NetworkError at once, and so does this."""
        engine = self._engine
        inj = engine.injected

        def attempt():
            ctx = inj.main_context(self._id)
            owned: List[str] = []
            try:
                if force_expr or not _bridge.is_jsfunc(page_function):
                    return inj.evaluate_raw(ctx, page_function,
                                            by_value=by_value,
                                            timeout=self._page._timeout_s)
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
        return attempt()

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
            adopted = inj.adopt(self._id, remote["objectId"])
            inj.release(inj.main_context(self._id), remote["objectId"])
            if adopted:
                return ElementHandle(self, adopted)
        return JSHandle(self, remote.get("objectId") or "", remote)

    # ── finding ─────────────────────────────────────────────────────────────
    async def _query(self, root: Optional[str], by: str, value: str) -> List[ElementHandle]:
        def run():
            return self._engine.injected.in_current_world(
                lambda _: _bridge.query(self._engine, self._id, root, by, value),
                timeout=30.0)
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
        return await _bridge.run(lambda: self._engine.injected.content(self._id))

    async def setContent(self, html: str) -> None:
        """Replaces the document with ``html``, and returns: pyppeteer's
        `setContent` is the evaluation alone, and does not wait for the
        document's `load` (until 0.3.7 this did).

        ⛔ Through `document.open/write/close` in the PAGE's world: Gecko wants
        `document.open()` under the document's own principal, and the utility
        world's extended principal is refused with "The operation is insecure"."""
        await _bridge.run(
            self._engine.injected.evaluate_in_main,
            self._id, "(() => { document.open(); document.write(%s);"
            " document.close(); })()" % json.dumps(html), by_value=True)

    async def title(self) -> str:
        return await _bridge.run(lambda: self._engine.injected.title(self._id)) or ""

    # pyppeteer's page functions for the two tags (frame_manager.py), as they
    # are: a tag by `url` resolves once it has loaded and rejects when it does
    # not; a script by content throws an error its `onerror` saw at once.
    _ADD_SCRIPT_URL = """async function addScriptUrl(url, type) {
    const script = document.createElement('script');
    script.src = url;
    if (type)
        script.type = type;
    const promise = new Promise((res, rej) => {
        script.onload = res;
        script.onerror = rej;
    });
    document.head.appendChild(script);
    await promise;
    return script;
}"""
    _ADD_SCRIPT_CONTENT = """function addScriptContent(content, type = 'text/javascript') {
    const script = document.createElement('script');
    script.type = type;
    script.text = content;
    let error = null;
    script.onerror = e => error = e;
    document.head.appendChild(script);
    if (error)
        throw error;
    return script;
}"""
    _ADD_STYLE_URL = """async function (url) {
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = url;
    const promise = new Promise((res, rej) => {
        link.onload = res;
        link.onerror = rej;
    });
    document.head.appendChild(link);
    await promise;
    return link;
}"""
    _ADD_STYLE_CONTENT = """async function (content) {
    const style = document.createElement('style');
    style.type = 'text/css';
    style.appendChild(document.createTextNode(content));
    const promise = new Promise((res, rej) => {
        style.onload = res;
        style.onerror = rej;
    });
    document.head.appendChild(style);
    await promise;
    return style;
}"""

    async def addScriptTag(self, options: Dict = None, **kwargs: str) -> ElementHandle:
        """pyppeteer's `addScriptTag`: by `url` it waits for the script to
        load and raises PageError when it does not; by `path` or `content`
        the tag is `text/javascript` unless `type` says otherwise."""
        options = _bridge.merge(options, kwargs)
        if isinstance(options.get("url"), str):
            url = options["url"]
            args = [url] + ([options["type"]] if "type" in options else [])
            try:
                return await self.evaluateHandle(self._ADD_SCRIPT_URL, *args)
            except errors.ElementHandleError as e:
                raise errors.PageError("Loading script from %s failed" % url) from e
        if isinstance(options.get("path"), str):
            with open(options["path"], encoding="utf-8") as f:
                contents = f.read()
            contents += "//# sourceURL=%s" % options["path"].replace("\n", "")
            args = [contents] + ([options["type"]] if "type" in options else [])
            return await self.evaluateHandle(self._ADD_SCRIPT_CONTENT, *args)
        if isinstance(options.get("content"), str):
            args = [options["content"]] + ([options["type"]] if "type" in options else [])
            return await self.evaluateHandle(self._ADD_SCRIPT_CONTENT, *args)
        raise ValueError(
            "Provide an object with a `url`, `path` or `content` property")

    async def addStyleTag(self, options: Dict = None, **kwargs: str) -> ElementHandle:
        """pyppeteer's `addStyleTag`: the tag resolves once its stylesheet has
        loaded, and by `url` a stylesheet that does not load is PageError."""
        options = _bridge.merge(options, kwargs)
        if isinstance(options.get("url"), str):
            url = options["url"]
            try:
                return await self.evaluateHandle(self._ADD_STYLE_URL, url)
            except errors.ElementHandleError as e:
                raise errors.PageError("Loading style from %s failed" % url) from e
        if isinstance(options.get("path"), str):
            with open(options["path"], encoding="utf-8") as f:
                contents = f.read()
            contents += "/*# sourceURL=%s*/" % options["path"].replace("\n", "")
            return await self.evaluateHandle(self._ADD_STYLE_CONTENT, contents)
        if isinstance(options.get("content"), str):
            return await self.evaluateHandle(self._ADD_STYLE_CONTENT, options["content"])
        raise ValueError(
            "Provide an object with a `url`, `path` or `content` property")

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
        """Chooses the options with these values and returns the values now
        selected, as pyppeteer's `select`: every value must be a string, an
        unknown value is ignored, the first match only unless the select is
        `multiple`, and nothing matching leaves nothing selected. No check
        that the select is visible or enabled: a hidden native select behind
        a styled widget is the ordinary case.

        ⛔ ONE DEPARTURE: the engine selects through the dropdown's own path,
        so `input` and `change` are trusted and fire when the selection
        changed. pyppeteer dispatches both from the page, untrusted, every
        time - the signal [B175] removed."""
        for value in values:
            if not isinstance(value, str):
                raise TypeError("Values must be string. Found %s of type %s"
                                % (value, type(value)))
        element = await self.querySelector(selector)
        if element is None:
            raise errors.ElementHandleError(
                'Error: failed to find element matching selector "%s"' % selector)
        indices = await element._read(
            "(el, values) => { if (el.nodeName.toLowerCase() !== 'select') return null;"
            " const out = []; const options = Array.from(el.options);"
            " for (let i = 0; i < options.length; i++) {"
            "  if (values.includes(options[i].value)) {"
            "   out.push(i); if (!el.multiple) break; } }"
            " return out; }", list(values))
        if indices is None:
            raise errors.ElementHandleError(
                "Evaluation failed: Error: Element is not a <select> element.")
        await _bridge.run(lambda: self._engine.send(
            "Page.selectOptions", {"frameId": self._id, "objectId": element._oid,
                                   "indices": indices}))
        return await element._read(
            "(el) => Array.from(el.options).filter(o => o.selected).map(o => o.value)")

    # ── waiting ─────────────────────────────────────────────────────────────────────
    #: pyppeteer's selector predicate inside its polling page function
    #: (frame_manager.py, `_waitForSelectorOrXPath` and
    #: `waitForPredicatePageFunction`), as one function the UTILITY world
    #: runs: polling on animation frames when visibility is asked, on DOM
    #: mutations otherwise, and resolving undefined when its own timer runs
    #: out. In the utility world the page sees no MutationObserver and no
    #: requestAnimationFrame of its own being called.
    _WAIT_FOR_SELECTOR = """async (selectorOrXPath, isXPath, waitForVisible, waitForHidden, polling, timeout) => {
  const predicate = () => {
    const node = isXPath
        ? document.evaluate(selectorOrXPath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue
        : document.querySelector(selectorOrXPath);
    if (!node)
      return waitForHidden;
    if (!waitForVisible && !waitForHidden)
      return node;
    const element = node.nodeType === Node.TEXT_NODE ? node.parentElement : node;
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    const isVisible = style && style.visibility !== 'hidden' &&
        !!(rect.top || rect.bottom || rect.width || rect.height);
    const success = (waitForVisible === isVisible || waitForHidden === !isVisible);
    return success ? node : null;
  };
  let timedOut = false;
  if (timeout)
    setTimeout(() => timedOut = true, timeout);
  const first = predicate();
  if (first)
    return first;
  return await new Promise(fulfill => {
    if (polling === 'raf') {
      const onRaf = () => {
        if (timedOut)
          return fulfill();
        const success = predicate();
        if (success)
          return fulfill(success);
        requestAnimationFrame(onRaf);
      };
      requestAnimationFrame(onRaf);
      return;
    }
    const observer = new MutationObserver(() => {
      if (timedOut) {
        observer.disconnect();
        return fulfill();
      }
      const success = predicate();
      if (success) {
        observer.disconnect();
        fulfill(success);
      }
    });
    observer.observe(document, {childList: true, subtree: true, attributes: true});
    if (timeout)
      setTimeout(() => { observer.disconnect(); fulfill(); }, timeout);
  });
}"""

    # ⛔ NOT `async def`, AS IN PYPPETEER: the wait starts when it is CALLED,
    # so `w = frame.waitForSelector(...)` sees an element that comes and goes
    # before `await w`. Until 0.4.0 nothing ran until the await.
    #
    # ⛔ AND ON THE BROWSER'S LOOP, as pyppeteer's `WaitTask` creates its task
    # on its connection's loop: a call made with no loop running (the
    # `loop.run_until_complete(page.waitForSelector(...))` of a synchronous
    # caller) belongs to the loop the browser was launched on, not to a new
    # one `asyncio` would make up.
    def waitForSelector(self, selector: str, options: dict = None, **kwargs: Any) -> Awaitable:
        return self._page._loop.create_task(
            self._wait_for_element("css", selector, _bridge.merge(options, kwargs)))

    def waitForXPath(self, xpath: str, options: dict = None, **kwargs: Any) -> Awaitable:
        return self._page._loop.create_task(
            self._wait_for_element("xpath", xpath, _bridge.merge(options, kwargs)))

    async def _wait_for_element(self, by: str, value: str, options: dict):
        """pyppeteer's `WaitTask` for a selector or an XPath: the element, or
        with `hidden` the element still there but hidden, or a handle to
        `true` when it is gone; again in the new document when the frame
        navigates; PageError when the frame is detached."""
        timeout = options.get("timeout", 30000)
        visible = bool(options.get("visible"))
        hidden = bool(options.get("hidden"))
        polling = "raf" if visible or hidden else "mutation"
        title = '%s "%s"%s' % ("XPath" if by == "xpath" else "selector", value,
                               " to be hidden" if hidden else "")
        deadline = Deadline(timeout / 1000.0 if timeout else None)
        engine = self._engine

        def run():
            while True:
                if engine.lifecycle.frame(self._id) is None:
                    raise errors.PageError("waitForFunction failed: frame got detached.")
                left = deadline.left()
                if left is not None and left <= 0:
                    return None
                try:
                    ctx = engine.injected.context_id(self._id)
                    return engine.injected.call_raw(
                        ctx, self._WAIT_FOR_SELECTOR,
                        [{"value": value}, {"value": by == "xpath"},
                         {"value": visible}, {"value": hidden},
                         {"value": polling},
                         {"value": 0 if left is None else max(1, int(left * 1000))}],
                        by_value=False, timeout=None)
                except ContextDestroyed:
                    continue  # the document went: ask the next one
        remote = await _bridge.run(run)
        if not remote or remote.get("type") == "undefined":
            raise errors.TimeoutError(
                "Waiting for %s failed: timeout %sms exceeds." % (title, timeout))
        if remote.get("subtype") == "node" and remote.get("objectId"):
            return ElementHandle(self, remote["objectId"])
        return JSHandle(self, remote.get("objectId") or "", remote)

    async def waitForFunction(self, pageFunction: str, options: dict = None,
                              *args: Any, **kwargs: Any) -> JSHandle:
        options = _bridge.merge(options, kwargs)
        polling = options.get("polling", "raf")
        # pyppeteer's `WaitTask` refuses these before anything is sent.
        if isinstance(polling, str):
            if polling not in ("raf", "mutation"):
                raise ValueError("Unknown polling: %s" % polling)
        elif isinstance(polling, (int, float)):
            if polling <= 0:
                raise ValueError(
                    "Cannot poll with non-positive interval: %s" % polling)
        else:
            raise ValueError("Unknown polling option: %s" % polling)
        interval = (float(polling) / 1000.0 if isinstance(polling, (int, float))
                    else 0.05)
        deadline = Deadline(_bridge.ms(options))

        def poll() -> Optional[JSHandle]:
            # ⛔ A NAVIGATION DOES NOT END THE WAIT: pyppeteer's WaitTask runs
            # again in the new document. The predicate is sent once per turn,
            # and a world that went under it means the next turn asks the new
            # document ([B285]). A frame that is gone ends it, in pyppeteer's
            # words (`WaitTask` on `_detach`).
            if self._engine.lifecycle.frame(self._id) is None:
                raise errors.PageError("waitForFunction failed: frame got detached.")
            try:
                remote = self._evaluate_sync(pageFunction, args, by_value=False,
                                             force_expr=False)
            except ContextDestroyed:
                return None
            if "objectId" in remote or _bridge.value_of(remote):
                return self._handle_from_remote(remote)
            return None

        while True:
            # ⛔ A PREDICATE THAT THROWS ENDS THE WAIT WITH ITS ERROR, as
            # pyppeteer's `WaitTask` rejects with it. Until 0.3.7 the error was
            # swallowed and the wait polled on to its timeout, which then
            # reported a timeout for a ReferenceError.
            handle = await _bridge.run(poll)
            if handle is not None:
                return handle
            if deadline.passed():
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
        self.keyboard = Keyboard(self)
        self.mouse = Mouse(self)
        self.touchscreen = Touchscreen(self)
        engine_page.on_event(self._on_event)
        # ⛔ THE FRAME TREE IS THE LIFECYCLE'S. It says when a frame is born
        # and when it is gone, including the children of a document that is
        # replaced, which the engine does not report after a back-forward cache
        # restore or a move to another process. Until 0.3.5 this page kept a
        # set of its own frames "of documents it had left" for the first case,
        # and announced the main frame again at every cross-process navigation.
        engine_page.lifecycle.announce = self._on_lifecycle

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
            self.emit_threadsafe("framenavigated", self._frame(params["frameId"]))
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

    def _on_lifecycle(self, what: str, frame_id: str,
                      url: Optional[str] = None) -> None:
        """The lifecycle's announcements: a frame born, a frame gone. Called on
        the reader thread under the lifecycle's lock, so each is handed to the
        loop as it comes."""
        if what == "frameAttached":
            # Never the main frame: the lifecycle announces an id it sees for
            # the first time, and this page is built after its main frame.
            self.emit_threadsafe("frameattached", self._frame(frame_id))
        elif what == "frameDetached":
            frame = self._frames.pop(frame_id, None)
            if frame is not None:
                self.emit_threadsafe("framedetached", frame)
        elif what == "sameDocument":
            # pyppeteer emits `framenavigated` for a same-document
            # navigation too (`_onFrameNavigatedWithinDocument`).
            self.emit_threadsafe("framenavigated", self._frame(frame_id))

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
        return [self._frame(fid) for fid in list(self._engine.lifecycle.frames)]

    @property
    def workers(self) -> list:
        return []

    @property
    def url(self) -> str:
        return self.mainFrame.url

    @property
    def viewport(self) -> Optional[Dict]:
        if self._viewport is not None:
            # The very dict the caller passed, as pyppeteer's `page.viewport`.
            return self._viewport
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
        state = _state(options)
        timeout = (_bridge.ms(options) if "timeout" in options
                   else self._navigation_timeout_s)
        engine = self._engine

        def run():
            # ⛔ `_FOLLOW`, and the answer is the response of the document the
            # wait ended on (`requests.get(mainFrame._navigationURL)`). Until
            # 0.6.0 a replacement before commit was PageError "interrupted",
            # the response was the first document's after the page replaced
            # itself, and the children were not waited for.
            try:
                return engine.lifecycle.goto(url, frame_id=engine.main_frame_id,
                                             until=state, timeout=timeout,
                                             referer=options.get("referer"),
                                             **_FOLLOW)
            except NavigationError as e:
                raise errors.PageError("%s at %s" % (e, url)) from None
            except TimeoutError:
                raise errors.TimeoutError(
                    "Navigation Timeout Exceeded: %s ms exceeded."
                    % int(timeout * 1000)) from None
        result = await _bridge.run(run)
        return self._navigation_response(result.get("landed"))

    async def _history(self, command: str, options: dict) -> Optional[Response]:
        state = _state(options)
        timeout = (_bridge.ms(options) if "timeout" in options
                   else self._navigation_timeout_s)
        engine = self._engine

        def run():
            fid = engine.main_frame_id
            since = engine.lifecycle.mark(fid)
            result = engine.send(command, {"frameId": fid}
                                 if command != "Page.reload" else {}) or {}
            if command != "Page.reload" and not result.get("success"):
                return None
            # ⛔ pyppeteer's `NavigatorWatcher`: a same-document entry (a
            # hash, a pushState) completes it at once with no response, and a
            # timeout is an error. Until 0.3.7 the first waited the whole
            # timeout and every timeout, the second case too, answered None.
            try:
                return engine.lifecycle.wait_for_new_navigation(
                    fid, since, state, timeout=timeout, **_FOLLOW)
            except TimeoutError:
                raise errors.TimeoutError(
                    "Navigation Timeout Exceeded: %s ms exceeded."
                    % int(timeout * 1000)) from None
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
        state = _state(options)
        timeout = (_bridge.ms(options) if "timeout" in options
                   else self._navigation_timeout_s)
        engine = self._engine
        fid = engine.main_frame_id
        # A same-document navigation completes the wait too, with no
        # response: pyppeteer's `NavigatorWatcher`.
        since = engine.lifecycle.mark(fid)

        def run():
            try:
                return engine.lifecycle.wait_for_new_navigation(
                    fid, since, state, timeout=timeout, **_FOLLOW)
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
        """The cookies that would be sent to these URLs (the page's URL when
        none is given), as Chrome's `Network.getCookies` answers pyppeteer:
        by domain, path and `secure` (`_sent_to`)."""
        urls = urls or (self.url,)
        all_cookies = await _bridge.run(self._context._engine.cookies)
        out = []
        for c in all_cookies:
            if any(_sent_to(c, url) for url in urls):
                item = dict(c)
                item.setdefault("size", len(c.get("name", "")) + len(c.get("value", "")))
                out.append(item)
        return out

    async def setCookie(self, *cookies: dict) -> None:
        """pyppeteer's `setCookie`: a cookie with no `url` takes the page's
        when the page is http(s); a blank page or a data: URL cannot have one;
        each cookie is deleted first, as pyppeteer does, so on a page that is
        not http(s) a cookie with neither url nor domain is that deletion's
        protocol error."""
        page_url = self.url
        items = []
        for cookie in cookies:
            item = dict(cookie)
            if "url" not in item and page_url.startswith("http"):
                item["url"] = page_url
            if item.get("url") == "about:blank":
                raise errors.PageError('Blank page can not have cookie "%s"' % item.get("name", ""))
            if (item.get("url") or "").startswith("data:"):
                raise errors.PageError('Data URL page can not have cookie "%s"' % item.get("name", ""))
            items.append(item)
        await self.deleteCookie(*items)
        writes = []
        for item in items:
            item = dict(item)
            # ⛔ A session cookie goes WITHOUT `expires`: Juggler turns -1 into
            # an expiry in 1969 and drops the cookie silently (70-known-bugs
            # [B224] in the workbench).
            if item.get("expires") in (None, -1):
                item.pop("expires", None)
            writes.append(item)
        if writes:
            await _bridge.run(self._context._engine.set_cookies, writes)

    async def deleteCookie(self, *cookies: dict) -> None:
        """pyppeteer's `deleteCookie`, as Chrome's `Network.deleteCookies`:
        the cookies with this name that would be sent to `url` (the page's
        when none is given and the page is http(s)), or with exactly this
        `domain`, and with exactly this `path` when one is given. Deleted by
        expiring them: Juggler can only clear a whole context, which is more
        than this call asks for."""
        page_url = self.url
        current = None
        doomed = []
        for spec in cookies:
            spec = dict(spec)
            if not spec.get("url") and page_url.startswith("http"):
                spec["url"] = page_url
            if not spec.get("url") and not spec.get("domain"):
                raise errors.NetworkError(
                    "Protocol error (Network.deleteCookies): At least one of "
                    "the url and domain needs to be specified")
            if current is None:
                current = await _bridge.run(self._context._engine.cookies)
            for c in current:
                if c.get("name") != spec.get("name"):
                    continue
                if spec.get("url") and not _sent_to(c, spec["url"]):
                    continue
                if spec.get("domain") and (c.get("domain") or "").lstrip(".") != spec["domain"].lstrip("."):
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
        a different one is the caller's choice, and it is applied as asked.

        `width` and `height` are checked as pyppeteer's `get_positive_int`
        checks them: an int, not negative. `isMobile`, `hasTouch` and
        `deviceScaleFactor` are not emulated: the session's profile declares
        them, and a page that read a touch screen or a pixel ratio its screen
        does not have would read a device that does not exist."""
        size = {}
        for name in ("width", "height"):
            value = viewport[name]
            if not isinstance(value, int):
                raise TypeError("%s must be integer: %s" % (name, type(value)))
            if value < 0:
                raise ValueError("%s must be positive integer: %s" % (name, value))
            size[name] = value
        self._viewport = viewport
        await _bridge.run(self._engine.send, "Page.setViewportSize",
                          {"viewportSize": size})

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
        """pyppeteer's `close`. With `runBeforeUnload` the page runs its
        `beforeunload` handlers and may ask (a `dialog` of type
        `beforeunload`); it closes when that is accepted, and `close` is
        emitted then. A page already closed raises, as pyppeteer's does once
        its session is gone."""
        options = _bridge.merge(options, kwargs)
        if self._closed:
            raise errors.PageError("Protocol Error: Connection Closed. "
                                   "Most likely the page has been closed.")
        if options.get("runBeforeUnload"):
            await _bridge.run(self._engine.send, "Page.close", {"runBeforeUnload": True})
            return
        self._closed = True
        await _bridge.run(self._engine.close)
        self.emit("close")


__all__ = ["Page", "Frame", "ExecutionContext", "Keyboard", "Mouse",
           "Touchscreen", "Dialog", "ConsoleMessage", "Request", "Response"]
