# Third-party code in invisible_puppeteer

Two files were extracted from Microsoft Playwright's driver bundle and are
redistributed under the Apache License 2.0 (`LICENSE-APACHE`). Everything else
is MIT (`LICENSE`).

| file | what it is |
|---|---|
| `src/invisible_puppeteer/_juggler/injected.js` | the selector engines and the actionability checks that run inside the page's utility world, with the stealth fixes invisible_playwright made to them |
| `src/invisible_puppeteer/_juggler/keylayout.py` | the US keyboard layout (key, code, keyCode, location) |

Both came with the rest of invisible_playwright's engine when this package was
created as its replica (through invisible_selenium's copy, which carries the
same four engine adaptations). The history of the changes made to them is in
invisible_playwright's `THIRD_PARTY_FORK.md`.

## pyppeteer

No pyppeteer code is included. The public API - function, class and method
names, signatures, options, events and exception classes - is re-implemented so
that scripts written for pyppeteer run unchanged. pyppeteer is MIT; names and
interfaces are what is shared, not code.
