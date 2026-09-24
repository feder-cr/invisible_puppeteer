"""`JSHandle` and `ElementHandle`, with pyppeteer's methods.

⛔ TWO WORLDS, ON PURPOSE. A `JSHandle` from `evaluateHandle` is an object of
the PAGE's world, because that is where the caller's code made it. An
`ElementHandle` is a node held in the UTILITY world, because every action on
it goes through the engine's `Actions` - the pointer that travels, the press
with a duration, the trusted input events - and those work on utility-world
handles. A node crosses between the two with `Page.adoptNode`, never by
running code in the page.
"""
from __future__ import annotations

import base64
import os
from typing import Any, Dict, List, Optional

from . import _bridge, errors

_HANDLE = "<element handle>"


class JSHandle:
    def __init__(self, frame, oid: str, remote: dict) -> None:
        self._frame = frame
        self._oid = oid
        self._remote = remote
        self._disposed = False

    @property
    def _page(self):
        return self._frame._page._engine

    def _context(self) -> str:
        return self._page.injected.main_context(self._frame._id)

    def executionContext(self):
        return self._frame._execution_context

    async def getProperty(self, propertyName: str) -> "JSHandle":
        """The property as a handle. Like pyppeteer, this reads the property
        in the page's world, where the object lives."""
        return await self._frame.evaluateHandle(
            "(object, propertyName) => object[propertyName]", self, propertyName)

    async def getProperties(self) -> Dict[str, "JSHandle"]:
        """The object's enumerable properties, as handles.

        Read through the engine's Debugger (`Runtime.getObjectProperties`):
        no getter the page defined is invoked."""
        def read():
            ctx = self._context()
            return ctx, self._page.injected.properties(ctx, self._oid)
        ctx, props = await _bridge.run(read)
        out: Dict[str, JSHandle] = {}
        for p in props:
            value = p.get("value") or {}
            if value:
                out[p["name"]] = self._frame._handle_from_remote(value)
        return out

    async def jsonValue(self) -> Any:
        """The object's JSON value, serialized by the engine's own sandbox."""
        if "objectId" not in self._remote:
            return _bridge.value_of(self._remote)
        return await _bridge.run(lambda: self._page.injected.by_value(
            self._context(), self._oid))

    def asElement(self) -> Optional["ElementHandle"]:
        return None

    async def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        if self._oid:
            await _bridge.run(lambda: self._page.injected.release(
                self._context(), self._oid))

    def toString(self) -> str:
        if "objectId" in self._remote:
            kind = self._remote.get("subtype") or self._remote.get("type") or "object"
            return "JSHandle@" + kind
        return "JSHandle:" + str(_bridge.value_of(self._remote))

    def _as_argument(self) -> dict:
        if "objectId" in self._remote:
            return {"objectId": self._oid}
        return _bridge.plain_argument(_bridge.value_of(self._remote))


class ElementHandle(JSHandle):
    """A DOM node, acted on with a human hand."""

    def __init__(self, frame, oid: str) -> None:
        super().__init__(frame, oid, {"objectId": oid, "type": "object",
                                      "subtype": "node"})

    def _context(self) -> str:
        return self._page.injected.context_id(self._frame._id)

    def asElement(self) -> "ElementHandle":
        return self

    def toString(self) -> str:
        return "JSHandle@node"

    def _act(self, what: str, *args, **kwargs):
        actions = self._page.actions
        kwargs.setdefault("timeout", self._frame._page._action_timeout)
        return getattr(actions, what)(_HANDLE, *args, frame_id=self._frame._id,
                                      element_id=self._oid, **kwargs)

    async def _read(self, declaration: str, *args):
        """`declaration(el, *args)` in the utility world, by value."""
        def call():
            remote = self._page.injected.call_raw(
                self._context(),
                "(el, ...rest) => { if (!el.isConnected) return {s: 1};"
                " return {v: (%s)(el, ...rest)}; }" % declaration,
                [{"objectId": self._oid}] + [_bridge.plain_argument(a) for a in args],
                by_value=True)
            return remote.get("value") or {}
        answer = await _bridge.run(call)
        if answer.get("s"):
            raise errors.ElementHandleError("Node is detached from document")
        return answer.get("v")

    # ── frames ──────────────────────────────────────────────────────────────
    async def contentFrame(self):
        """The frame an `<iframe>` element contains, or None."""
        described = await _bridge.run(lambda: self._page.send(
            "Page.describeNode", {"frameId": self._frame._id,
                                  "objectId": self._oid}))
        inner = (described or {}).get("contentFrameId")
        if not inner:
            return None
        return self._frame._page._frame(inner)

    # ── acting ──────────────────────────────────────────────────────────────
    async def hover(self) -> None:
        await self._frame._page._slow()
        await _bridge.run(self._act, "hover")

    async def click(self, options: dict = None, **kwargs: Any) -> None:
        """Clicks the element with the pointer: it travels there along a path
        drawn from the session seed, and the press has the session's hold.

        Options: ``button`` (left/right/middle), ``clickCount``, ``delay``
        (milliseconds between mousedown and mouseup)."""
        options = _bridge.merge(options, kwargs)
        await self._frame._page._slow()
        await _bridge.run(self._act, "click",
                          button=_bridge.button(options.get("button")),
                          clicks=int(options.get("clickCount") or 1),
                          delay_ms=options.get("delay"))

    async def uploadFile(self, *filePaths: str) -> dict:
        files = [os.path.abspath(p) for p in filePaths]
        await _bridge.run(self._act, "set_input_files", files)
        return {}

    async def tap(self) -> None:
        def run():
            self._page.injected.scroll_into_view(self._frame._id, self._oid)
            point = self._page.actions._center_point(self._frame._id, self._oid)
            if point is None:
                raise errors.ElementHandleError(
                    "Node is either not visible or not an HTMLElement")
            self._page.send("Page.dispatchTapEvent",
                            {"x": point[0], "y": point[1], "modifiers": 0})
        await self._frame._page._slow()
        await _bridge.run(run)

    async def focus(self) -> None:
        await _bridge.run(self._act, "focus")

    async def type(self, text: str, options: Dict = None, **kwargs: Any) -> None:
        """Focuses the element and types the text, with the session's rhythm
        (or a fixed ``delay`` in milliseconds between keys)."""
        options = _bridge.merge(options, kwargs)
        await self.focus()
        await self._frame._page.keyboard.type(text, options)

    async def press(self, key: str, options: Dict = None, **kwargs: Any) -> None:
        options = _bridge.merge(options, kwargs)
        await self.focus()
        await self._frame._page.keyboard.press(key, options)

    # ── geometry ────────────────────────────────────────────────────────────
    async def boundingBox(self) -> Optional[Dict[str, float]]:
        """x, y, width, height in main-frame viewport pixels, or None."""
        return await _bridge.run(self._page.injected.bounding_box,
                                 self._frame._id, self._oid)

    async def boxModel(self) -> Optional[Dict]:
        """The element's quad, repeated for each box: this engine reports the
        border box only."""
        r = await _bridge.run(lambda: self._page.send(
            "Page.getContentQuads", {"frameId": self._frame._id,
                                     "objectId": self._oid}))
        quads = (r or {}).get("quads") or []
        if not quads:
            return None
        q = quads[0]
        points = [q["p1"], q["p2"], q["p3"], q["p4"]]
        quad = [{"x": p["x"], "y": p["y"]} for p in points]
        xs = [p["x"] for p in points]
        ys = [p["y"] for p in points]
        return {"content": quad, "padding": quad, "border": quad,
                "margin": quad, "width": int(max(xs) - min(xs)),
                "height": int(max(ys) - min(ys))}

    async def isIntersectingViewport(self) -> bool:
        box = await self.boundingBox()
        if not box:
            return False
        size = await self._frame._page._viewport_now()
        return (box["x"] < size["width"] and box["y"] < size["height"]
                and box["x"] + box["width"] > 0 and box["y"] + box["height"] > 0)

    async def screenshot(self, options: Dict = None, **kwargs: Any) -> bytes:
        options = _bridge.merge(options, kwargs)

        def clip():
            self._page.injected.scroll_into_view(self._frame._id, self._oid)
            box = self._page.injected.bounding_box(self._frame._id, self._oid)
            if box is None:
                raise errors.ElementHandleError(
                    "Node is either not visible or not an HTMLElement")
            scroll = self._page.injected.evaluate(
                self._page.main_frame_id, "({x: window.scrollX, y: window.scrollY})")
            return {"x": box["x"] + scroll["x"], "y": box["y"] + scroll["y"],
                    "width": max(1, box["width"]), "height": max(1, box["height"])}
        options["clip"] = await _bridge.run(clip)
        return await self._frame._page.screenshot(options)

    # ── finding ─────────────────────────────────────────────────────────────
    async def querySelector(self, selector: str) -> Optional["ElementHandle"]:
        found = await self._frame._query(self._oid, "css", selector)
        return found[0] if found else None

    async def querySelectorAll(self, selector: str) -> List["ElementHandle"]:
        return await self._frame._query(self._oid, "css", selector)

    async def querySelectorEval(self, selector: str, pageFunction: str,
                                *args: Any) -> Any:
        element = await self.querySelector(selector)
        if element is None:
            raise errors.ElementHandleError(
                'Error: failed to find element matching selector "%s"' % selector)
        return await self._frame.evaluate(pageFunction, element, *args)

    async def querySelectorAllEval(self, selector: str, pageFunction: str,
                                   *args: Any) -> Any:
        elements = await self.querySelectorAll(selector)
        array = await self._frame.evaluateHandle("(...els) => els", *elements)
        return await self._frame.evaluate(pageFunction, array, *args)

    async def xpath(self, expression: str) -> List["ElementHandle"]:
        return await self._frame._query(self._oid, "xpath", expression)

    J = querySelector
    JJ = querySelectorAll
    Jeval = querySelectorEval
    JJeval = querySelectorAllEval
    Jx = xpath


__all__ = ["JSHandle", "ElementHandle"]
