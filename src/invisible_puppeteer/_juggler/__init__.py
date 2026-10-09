"""The pyppeteer side of the engine: a launched browser, its contexts and its pages.

| module | what it does |
|---|---|
| `browser.py` | a launched browser, its contexts and its pages; extracted from invisible_playwright's `server.py` without the Playwright channel |

⛔ THE JUGGLER CLIENT ITSELF IS NOT HERE. Until 0.3.0 this folder carried a
copy of it, taken from invisible_playwright on 2026-09-24, and the copies
drifted: five remedies made in the wrapper never reached this one, and the one
guard this copy had never reached the wrapper. Since 0.3.0 the pipe, the
protocol mirror, the frame lifecycle, the injected script, the input actions,
the keyboard and the human rhythm (`connection`, `protocol`, `lifecycle`,
`injected`, `actions`, `keyboard`, `keylayout`, `_profile`, `_behaviour`,
`_motion`, `_pacing`) live once in `invisible_core.juggler` (decision D85).

What this client needs differently is an OPTION of the core's client, not a
fork of it: `engine_approach=True`, because pyppeteer has no cursor of its own and
the engine side draws every pointer approach; `dialog_opened`, so a click that
opens `alert()` does not wait for an answer the suspended page cannot give;
`glide_to` for the callers that drive the pointer directly; and the main-world
helpers of `injected.py`. A copy of any core module in this package is the
defect that move removed, and `tests/test_juggler_lives_in_the_core.py`
refuses it.
"""
