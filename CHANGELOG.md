# Changelog

## [0.1.1] - 2026-10-02

### Fixed
- **On Linux, a headless session starts on its own virtual display.** The
  display counted as ready as soon as its lockfile existed, which Xvfb writes
  before it opens its sockets, so sessions started together could land on
  each other's display and the browser failed with `cannot open display`.
  The display is also no longer reachable over TCP.

### Requires
- `invisible-core` 34.32.0, which carries the display fix. Same engine
  (firefox-34).

## [0.1.0] - 2026-09-25

First version: a replica of invisible_playwright 0.25.7 with pyppeteer's contract.

- `launch()` starts the same patched Firefox with the same profile, proxy,
  geography and hidden-surface handling as `InvisiblePlaywright`, and drives it
  over Juggler: no Chromium, no DevTools protocol, `navigator.webdriver` false.
- pyppeteer's public API: `Browser`, `BrowserContext`, `Page`, `Frame`,
  `ExecutionContext`, `JSHandle`, `ElementHandle`, `Keyboard`, `Mouse`,
  `Touchscreen`, `Dialog`, `ConsoleMessage`, `Request`, `Response` and the
  exceptions, with the same coroutines and events.
- Every click, hover and mouse move travels along a path drawn from the session
  seed; keys carry the session's typing rhythm; input events are trusted.
