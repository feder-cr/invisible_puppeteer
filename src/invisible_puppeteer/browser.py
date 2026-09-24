"""`Browser` and `BrowserContext`, with pyppeteer's methods and events."""
from __future__ import annotations

import asyncio
from typing import Dict, List, Optional

from . import _bridge, errors
from ._events import EventEmitter
from .page import Page


class BrowserContext(EventEmitter):
    def __init__(self, browser: "Browser", engine_context, incognito: bool) -> None:
        super().__init__(browser._loop)
        self._browser = browser
        self._engine = engine_context
        self._incognito = incognito

    @property
    def browser(self) -> "Browser":
        return self._browser

    def isIncognito(self) -> bool:
        return self._incognito

    isIncognite = isIncognito

    def targets(self) -> list:
        return []

    async def pages(self) -> List[Page]:
        """Every open tab of this context, including those the site opened."""
        return [await self._browser._page_for(self, tid)
                for tid in self._engine.live_targets()]

    async def newPage(self) -> Page:
        await _bridge.run(self._browser._session._before_new_page)
        engine_page = await _bridge.run(self._engine.new_page)
        return self._browser._wrap(self, engine_page)

    async def close(self) -> None:
        if not self._incognito:
            raise errors.BrowserError("Non-incognito profiles cannot be closed!")
        for page in list(self._browser._pages.values()):
            if page._context is self:
                page._closed = True
        await _bridge.run(self._engine.close)
        if self in self._browser._contexts:
            self._browser._contexts.remove(self)


class Browser(EventEmitter):
    """The launched patched Firefox. Same methods as pyppeteer's Browser."""

    Events = type("Events", (), {"TargetCreated": "targetcreated",
                                 "TargetDestroyed": "targetdestroyed",
                                 "TargetChanged": "targetchanged",
                                 "Disconnected": "disconnected"})

    def __init__(self, session, engine_browser, engine_context, loop,
                 *, slow_mo: float = 0.0, context_options: Optional[dict] = None) -> None:
        super().__init__(loop)
        self._session = session
        self._engine = engine_browser
        self._slow_mo = float(slow_mo or 0)
        self._context_options = dict(context_options or {})
        self._pages: Dict[str, Page] = {}
        self._default_context = BrowserContext(self, engine_context, incognito=False)
        self._contexts: List[BrowserContext] = []
        self._closed = False
        engine_browser.conn.add_listener(self._on_browser_event)

    # ── plumbing ────────────────────────────────────────────────────────────
    def _context_of(self, engine_context_id) -> Optional[BrowserContext]:
        for ctx in [self._default_context] + self._contexts:
            if ctx._engine.context_id == engine_context_id:
                return ctx
        return None

    def _wrap(self, context: BrowserContext, engine_page) -> Page:
        page = self._pages.get(engine_page.target_id)
        if page is None:
            page = Page(self, context, engine_page, self._loop)
            self._session._prepare_page(engine_page)
            self._pages[engine_page.target_id] = page
        return page

    async def _page_for(self, context: BrowserContext, target_id: str) -> Page:
        page = self._pages.get(target_id)
        if page is not None:
            return page
        engine_page = await _bridge.run(context._engine.adopt, target_id)
        return self._wrap(context, engine_page)

    def _on_browser_event(self, method: str, params: dict, session) -> None:
        """On the reader thread. A tab the SITE opens becomes a Page and a
        `popup` on its opener; a closed tab emits `close`."""
        if method == "Browser.attachedToTarget":
            info = params.get("targetInfo") or {}
            opener = info.get("openerId")
            if opener and self._loop is not None and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(
                    lambda: asyncio.ensure_future(self._announce_popup(info)))
        elif method == "Browser.detachedFromTarget":
            page = self._pages.pop(params.get("targetId"), None)
            if page is not None and not page._closed:
                page._closed = True
                page.emit_threadsafe("close")

    async def _announce_popup(self, info: dict) -> None:
        context = self._context_of(info.get("browserContextId") or None)
        opener = self._pages.get(info.get("openerId"))
        if context is None:
            return
        try:
            page = await self._page_for(context, info["targetId"])
        except Exception:
            return
        if opener is not None:
            opener.emit("popup", page)
        self.emit("targetcreated", page)

    # ── pyppeteer's surface ─────────────────────────────────────────────────
    @property
    def process(self):
        return None

    @property
    def wsEndpoint(self) -> str:
        raise errors.BrowserError(
            "there is no websocket endpoint: the browser is driven over a pipe")

    @property
    def browserContexts(self) -> List[BrowserContext]:
        return [self._default_context] + list(self._contexts)

    async def createIncognitoBrowserContext(self) -> BrowserContext:
        """A fresh container with its own cookies and storage, created with
        the same profile-derived options as the default one."""
        engine_context = await _bridge.run(self._engine.new_context,
                                           dict(self._context_options))
        context = BrowserContext(self, engine_context, incognito=True)
        self._contexts.append(context)
        return context

    createIncogniteBrowserContext = createIncognitoBrowserContext

    async def newPage(self) -> Page:
        return await self._default_context.newPage()

    async def pages(self) -> List[Page]:
        out: List[Page] = []
        for context in self.browserContexts:
            out.extend(await context.pages())
        return out

    def targets(self) -> list:
        return []

    async def version(self) -> str:
        """Product and version, as pyppeteer answers (`HeadlessChrome/71.0...`):
        here `Firefox/151.0`."""
        version = self._engine.version or ""
        return version if "/" in version else "Firefox/" + version

    async def userAgent(self) -> str:
        info = await _bridge.run(self._engine.conn.send, "Browser.getInfo", {})
        return (info or {}).get("userAgent") or ""

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for page in list(self._pages.values()):
            page._closed = True
        await _bridge.run(self._session._teardown)
        self.emit("disconnected")

    async def disconnect(self) -> None:
        """There is no remote browser to leave running: disconnecting ends it."""
        await self.close()


__all__ = ["Browser", "BrowserContext"]
