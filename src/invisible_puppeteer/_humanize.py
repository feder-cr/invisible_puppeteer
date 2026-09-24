"""Who draws the pointer's path, and how long one movement may take.

⛔ A DECISION BOTH WRAPPERS MAKE THE SAME WAY, so it is made here. `humanize=`
keeps the meaning it has always had on invisible_playwright's constructor -
falsy disables humanising, `True` enables it with the default cap, a number
enables it with that cap in seconds - and invisible_puppeteer's constructor
takes the same argument with the same meaning. The prefs handed to the browser
depend on the answer, so two wrappers answering differently would launch two
different browsers for the same seed.

What is NOT here is how a wrapper checks that its own path generator can run:
invisible_playwright's has to patch the vendored client's bindings, which is
Playwright's business, and hands that check in as `python_available`.

Moved out of `invisible_playwright/_cursor.py` on 2026-09-24.
"""
from __future__ import annotations

import os
from typing import Any, Callable

# The escape hatch. ``INVPW_CURSOR_ENGINE`` picks who generates the motion:
#   "python"  (default) - the wrapper, seeded from the session seed
#   "binary"            - the browser's own expansion, i.e. the pre-existing
#                         behaviour, for anyone who was depending on it
#   "off"                - no humanisation at all, same as humanize=False
ENGINE_ENV = "INVPW_CURSOR_ENGINE"

ENGINE_PYTHON = "python"
ENGINE_BINARY = "binary"
ENGINE_OFF = "off"

#: Hard ceiling on how far a single approach may be stretched, so a
#: pathological generator cannot hang a click. Mirrors the constructor's own
#: default cap.
DEFAULT_MAX_SECONDS = 1.5


def resolve_cursor_engine(humanize: Any,
                          python_available: Callable[[], bool]) -> str:
    """Decide who generates the motion for a session.

    This only decides WHERE the motion comes from, never WHETHER the user
    asked for it. `python_available` answers whether the wrapper's own
    generator can run in this process; when it cannot, the browser draws the
    path rather than nobody, because a session that cannot move at all is
    worse than one that moves with the browser's hand.
    """
    if not humanize:
        return ENGINE_OFF
    choice = (os.environ.get(ENGINE_ENV) or "").strip().lower()
    if choice in (ENGINE_BINARY, "juggler"):
        return ENGINE_BINARY
    if choice in (ENGINE_OFF, "none", "false", "0"):
        return ENGINE_OFF
    # "", "python", "wrapper" and anything unrecognised: prefer the Python
    # generator, but never at the price of a session that cannot move at all.
    return ENGINE_PYTHON if python_available() else ENGINE_BINARY


def max_seconds_for(humanize: Any) -> float:
    """The motion-duration cap implied by ``humanize=``."""
    if humanize is True:
        return DEFAULT_MAX_SECONDS
    try:
        value = float(humanize)
    except (TypeError, ValueError):
        return DEFAULT_MAX_SECONDS
    return value if value > 0 else DEFAULT_MAX_SECONDS
