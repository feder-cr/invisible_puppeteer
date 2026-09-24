"""invisible_puppeteer - pyppeteer's API for a patched Firefox with a stealth profile.

Quickstart:

    import asyncio
    from invisible_puppeteer import launch

    async def main():
        browser = await launch(seed=42)          # same seed, same fingerprint
        page = await browser.newPage()
        await page.goto("https://example.com")
        await page.click("a")                    # the pointer travels there
        await browser.close()

    asyncio.run(main())

A replica of invisible_playwright with pyppeteer's contract: the same engine,
the same binary, the same profile and the same human-paced input, driven over
Juggler - with no Chromium, no DevTools protocol and no Marionette.
"""
# ── Import-time core assertion, and repair ───────────────────────────────────
# Runs BEFORE every other import, exactly as in invisible_playwright: is the
# installed invisible-core the version this distribution declares? See `_pin.py`.
from ._pin import enforce_core_pin as _enforce_core_pin
_enforce_core_pin()

from invisible_core import BINARY_VERSION, FIREFOX_UPSTREAM_VERSION

from . import errors
from .browser import Browser, BrowserContext
from .element_handle import ElementHandle, JSHandle
from .launcher import connect, defaultArgs, executablePath, launch
from .page import Page
from ._version import __install_record_version__, __version__

version = __version__
version_info = tuple(int(p) for p in __version__.split("+")[0].split(".")[:3]
                     if p.isdigit())

__all__ = [
    "launch", "connect", "executablePath", "defaultArgs",
    "Browser", "BrowserContext", "Page", "ElementHandle", "JSHandle", "errors",
    "BINARY_VERSION", "FIREFOX_UPSTREAM_VERSION",
    "version", "version_info", "__version__", "__install_record_version__",
]
