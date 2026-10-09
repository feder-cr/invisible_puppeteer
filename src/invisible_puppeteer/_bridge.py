"""The seam between pyppeteer's API and the engine.

How a selector becomes a DOM query, how a page function and its arguments go
into the page, how an engine failure becomes the exception pyppeteer raises,
and how a blocking engine call is run from asyncio.

⛔ WHERE THINGS RUN. Queries and element reads run in the UTILITY world
(`__ctx_aux__`), which sees the DOM through an Xray: native methods, nothing a
site's wrapped accessor can count. Only `evaluate` and friends run in the
page's own world, because running code as the page is what they are for.
"""
from __future__ import annotations

import asyncio
import math
import time
from typing import Any, List, Optional

from . import errors
from invisible_core.juggler.connection import ProtocolError, TargetClosedError
from invisible_core.juggler.injected import EvaluationError

#: Runs in the utility world. `root` is an element or null for the document.
FIND_JS = """(root, by, value) => {
  if (root && root.isConnected === false) return 'stale';
  const scope = root || document;
  const doc = scope.ownerDocument || scope;
  if (by === 'css') return Array.from(scope.querySelectorAll(value));
  const r = doc.evaluate(value, scope, null, 7, null);
  const out = [];
  for (let i = 0; i < r.snapshotLength; i++) out.push(r.snapshotItem(i));
  return out;
}"""


def query(page, frame_id: str, root: Optional[str], by: str, value: str) -> List[str]:
    """Every match as utility-world objectIds, in document order: one round
    trip for the query and one for the list, whatever the number of matches."""
    inj = page.injected
    ctx = inj.context_id(frame_id)
    args = [{"objectId": root} if root else {"value": None},
            {"value": by}, {"value": value}]
    try:
        remote = inj.call_raw(ctx, FIND_JS, args)
    except EvaluationError as e:
        raise errors.ElementHandleError(
            "Evaluation failed: %s is not a valid %s: %s"
            % (value, "selector" if by == "css" else "XPath expression", e)) from None
    if "objectId" not in remote:
        raise errors.ElementHandleError("Node is detached from document")
    array = remote["objectId"]
    try:
        items = [p for p in inj.properties(ctx, array) if p["name"].isdigit()]
        items.sort(key=lambda p: int(p["name"]))
        return [p["value"]["objectId"] for p in items
                if (p.get("value") or {}).get("objectId")]
    finally:
        inj.release(ctx, array)


# ── page functions ──────────────────────────────────────────────────────────
def is_jsfunc(func: str) -> bool:
    """pyppeteer's own heuristic, kept byte for byte: a string that starts
    with `function` or `async `, or contains `=>`, is a function to CALL with
    the arguments; anything else is an expression to evaluate."""
    func = func.strip()
    if func.startswith("function") or func.startswith("async "):
        return True
    return "=>" in func


def plain_argument(value: Any) -> dict:
    """A Python value as a protocol argument; the four numbers JSON cannot
    carry travel as `unserializableValue`, as they do in CDP."""
    if isinstance(value, float):
        if math.isnan(value):
            return {"unserializableValue": "NaN"}
        if math.isinf(value):
            return {"unserializableValue": "Infinity" if value > 0 else "-Infinity"}
        if value == 0 and math.copysign(1, value) < 0:
            return {"unserializableValue": "-0"}
    return {"value": value}


def value_of(remote: dict) -> Any:
    """The Python value of a by-value RemoteObject."""
    if "unserializableValue" in remote:
        return {"NaN": float("nan"), "Infinity": float("inf"),
                "-Infinity": float("-inf"), "-0": -0.0}[remote["unserializableValue"]]
    return remote.get("value")


BUTTONS = {"left": 0, "middle": 1, "right": 2}


def button(name: Optional[str]) -> int:
    if name in (None, ""):
        return 0
    if name not in BUTTONS:
        raise errors.PyppeteerError("Unknown button: %s" % name)
    return BUTTONS[name]


# ── errors ──────────────────────────────────────────────────────────────────
_CONTEXT_GONE = ("Failed to find execution context", "Cannot find context",
                 "execution context was destroyed", "Execution context was destroyed")


def is_context_gone(error: BaseException) -> bool:
    return any(mark in str(error) for mark in _CONTEXT_GONE)


def translate(error: BaseException) -> BaseException:
    """The pyppeteer exception an engine failure stands for."""
    if isinstance(error, (errors.PyppeteerError, errors.TimeoutError)):
        return error
    text = str(error)
    if isinstance(error, TargetClosedError):
        return errors.NetworkError("Protocol error: Target closed. %s" % text)
    if isinstance(error, EvaluationError):
        return errors.ElementHandleError("Evaluation failed: %s" % text)
    if isinstance(error, TimeoutError):
        if "missing visible" in text or "no quad" in text:
            return errors.ElementHandleError(
                "Node is either not visible or not an HTMLElement: %s" % text)
        return errors.TimeoutError(text)
    if isinstance(error, ProtocolError):
        if "no response in" in text:
            return errors.TimeoutError(text)
        if "Cannot find object" in text or is_context_gone(error):
            return errors.ElementHandleError(
                "Node is detached from document (%s)" % text)
        return errors.NetworkError("Protocol error: %s" % text)
    return errors.PyppeteerError("%s: %s" % (type(error).__name__, text))


# ── running engine calls from asyncio ───────────────────────────────────────
async def run(fn, *args, **kwargs):
    """A blocking engine call, off the event loop, with pyppeteer's errors.

    ⛔ OFF THE LOOP, because the engine waits: on the pipe, on the pacing of a
    drawn pointer path, on a typed character's rhythm. Run on the loop, a
    humanised click would freeze every other task of the program for the
    second or two the hand takes to travel."""
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except (errors.PyppeteerError, errors.TimeoutError):
        raise
    except Exception as e:
        raise translate(e) from None


def across_navigation(fn, *, timeout: float = 30.0):
    """A page-level read, repeated while a navigation the page started on its
    own replaces the frame's worlds and the registry still names the old one."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            return fn()
        except Exception as e:
            if not is_context_gone(e) or time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def ms(options: dict, key: str = "timeout", default: float = 30000) -> float:
    """A pyppeteer timeout (milliseconds, 0 = none) as seconds for the engine."""
    value = options.get(key, default)
    if value in (None, 0):
        return 24 * 3600.0
    return float(value) / 1000.0


def merge(options: Optional[dict], kwargs: dict) -> dict:
    out = dict(options or {})
    out.update(kwargs)
    return out
