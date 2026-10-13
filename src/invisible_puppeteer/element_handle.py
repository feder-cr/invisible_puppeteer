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

from invisible_core.juggler.connection import ProtocolError

from . import _bridge, errors

_HANDLE = "<element handle>"

#: pyppeteer's `_scrollIntoViewIfNeeded` (element_handle.py, 1.0.2), run in the
#: utility world: a detached node and a node that is not an element are refused
#: with pyppeteer's words, and an element an IntersectionObserver does not see
#: wholly (ratio below 1) is scrolled to the centre. Answers the refusal, or
#: false.
_SCROLL_INTO_VIEW_IF_NEEDED = """async (el) => {
  if (!el.isConnected) return 'Node is detached from document';
  if (el.nodeType !== Node.ELEMENT_NODE) return 'Node is not of type HTMLElement';
  const ratio = await new Promise(resolve => {
    const observer = new IntersectionObserver(entries => {
      resolve(entries[0].intersectionRatio);
      observer.disconnect();
    });
    observer.observe(el);
  });
  if (ratio !== 1.0) {
    el.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
  }
  return false;
}"""


def _quad_area(points) -> float:
    """The area of a quad, by the shoelace formula, as pyppeteer's
    `_computeQuadArea`."""
    area = 0.0
    for i, a in enumerate(points):
        b = points[(i + 1) % len(points)]
        area += (a["x"] * b["y"] - b["x"] * a["y"]) / 2
    return abs(area)


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
    # ⛔ ONE ATTEMPT, AS PYPPETEER'S: `hover`, `click` and `tap` scroll the
    # element into view if needed, take its clickable point, and act there
    # once. There is no check that the element is enabled, stable or the
    # topmost one at the point, and no second attempt. Until 0.3.7 they went
    # through the engine's Playwright loop, which waited for all of that until
    # the timeout.
    def _scroll_into_view_if_needed(self) -> None:
        said = self._page.injected.call_raw(
            self._context(), _SCROLL_INTO_VIEW_IF_NEEDED,
            [{"objectId": self._oid}], by_value=True).get("value")
        if said:
            raise errors.ElementHandleError(said)

    def _clickable_point(self):
        """pyppeteer's `_clickablePoint`: the centre of the first content quad
        larger than one pixel, in the main frame's coordinates."""
        try:
            r = self._page.send("Page.getContentQuads",
                                {"frameId": self._frame._id, "objectId": self._oid})
        except Exception:  # noqa: BLE001 - pyppeteer: no quads, same error
            r = None
        quads = []
        for q in (r or {}).get("quads") or []:
            points = [q["p1"], q["p2"], q["p3"], q["p4"]]
            if _quad_area(points) > 1:
                quads.append(points)
        if not quads:
            raise errors.ElementHandleError(
                "Node is either not visible or not an HTMLElement")
        points = quads[0]
        return (sum(p["x"] for p in points) / 4, sum(p["y"] for p in points) / 4)

    def _point_in_view(self):
        self._scroll_into_view_if_needed()
        return self._clickable_point()

    async def hover(self) -> None:
        await self._frame._page._slow()

        def run():
            x, y = self._point_in_view()
            self._page.actions.glide_to(x, y)
        await _bridge.run(run)

    async def click(self, options: dict = None, **kwargs: Any) -> None:
        """Clicks the element with the pointer: it travels there along a path
        drawn from the session seed, and the press has the session's hold.

        Options: ``button`` (left/right/middle), ``clickCount``, ``delay``
        (milliseconds between mousedown and mouseup)."""
        options = _bridge.merge(options, kwargs)
        await self._frame._page._slow()

        def run():
            x, y = self._point_in_view()
            self._page.actions.click_at(x, y,
                                        button=_bridge.button(options.get("button")),
                                        clicks=int(options.get("clickCount") or 1),
                                        delay_ms=options.get("delay"))
        await _bridge.run(run)

    async def uploadFile(self, *filePaths: str) -> dict:
        files = [os.path.abspath(p) for p in filePaths]
        await _bridge.run(self._act, "set_input_files", files, once=True)
        return {}

    async def tap(self) -> None:
        def run():
            x, y = self._point_in_view()
            self._page.send("Page.dispatchTapEvent",
                            {"x": x, "y": y, "modifiers": 0})
        await self._frame._page._slow()
        await _bridge.run(run)

    async def focus(self) -> None:
        """`element.focus()`, as pyppeteer's: no check, and nothing at all for
        a node that is no longer in the document."""
        await _bridge.run(lambda: self._page.injected.call_raw(
            self._context(), "(el) => { el.focus(); }", [{"objectId": self._oid}]))

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
        """x, y, width, height in main-frame viewport pixels, or None - also
        when the engine refuses the question, as pyppeteer answers None for a
        protocol error (`_getBoxModel`)."""
        def box():
            try:
                return self._page.injected.bounding_box(self._frame._id, self._oid)
            except ProtocolError:
                return None
        return await _bridge.run(box)

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
