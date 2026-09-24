"""The exceptions pyppeteer raises, with the same names and hierarchy."""
import asyncio


class PyppeteerError(Exception):
    """Base exception for invisible_puppeteer."""


class BrowserError(PyppeteerError):
    """Exception raised from the browser."""


class ElementHandleError(PyppeteerError):
    """ElementHandle related exception."""


class NetworkError(PyppeteerError):
    """Network/Protocol related exception."""


class PageError(PyppeteerError):
    """Page/Frame related exception."""


class TimeoutError(asyncio.TimeoutError):
    """Timeout Error class."""


__all__ = ["PyppeteerError", "BrowserError", "ElementHandleError",
           "NetworkError", "PageError", "TimeoutError"]
