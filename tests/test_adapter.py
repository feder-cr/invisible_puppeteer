"""The pyppeteer adapter's own logic, without a browser."""
from __future__ import annotations

import asyncio
import math
import threading

import pytest

from invisible_puppeteer import _bridge, errors
from invisible_puppeteer._events import EventEmitter
from invisible_puppeteer.page import Page, _state

pytestmark = pytest.mark.unit


def test_function_or_expression_is_decided_like_pyppeteer():
    assert _bridge.is_jsfunc("() => 1")
    assert _bridge.is_jsfunc("  function () { return 1 }")
    assert _bridge.is_jsfunc("async () => 1")
    assert _bridge.is_jsfunc("x => x * 2")
    assert not _bridge.is_jsfunc("1 + 2")
    assert not _bridge.is_jsfunc("document.title")


def test_wait_until_maps_to_the_latest_engine_state():
    assert _state(None) == "load"
    assert _state("domcontentloaded") == "domcontentloaded"
    assert _state("networkidle0") == "networkidle"
    assert _state(["domcontentloaded", "load"]) == "load"
    with pytest.raises(ValueError):
        _state("whenever")


def test_numbers_json_cannot_carry_travel_as_unserializable_values():
    assert _bridge.plain_argument(float("nan")) == {"unserializableValue": "NaN"}
    assert _bridge.plain_argument(float("-inf")) == {"unserializableValue": "-Infinity"}
    assert _bridge.plain_argument(-0.0) == {"unserializableValue": "-0"}
    assert _bridge.plain_argument(1.5) == {"value": 1.5}
    assert math.isnan(_bridge.value_of({"unserializableValue": "NaN"}))
    assert _bridge.value_of({"value": [1, 2]}) == [1, 2]


def test_buttons_and_timeouts():
    assert [_bridge.button(b) for b in (None, "left", "middle", "right")] == [0, 0, 1, 2]
    with pytest.raises(errors.PyppeteerError):
        _bridge.button("fourth")
    assert _bridge.ms({"timeout": 1500}) == 1.5
    assert _bridge.ms({"timeout": 0}) > 3600  # 0 means no timeout


def test_engine_failures_become_pyppeteers_exceptions():
    from invisible_puppeteer._juggler.connection import ProtocolError, TargetClosedError
    from invisible_puppeteer._juggler.injected import EvaluationError
    assert isinstance(_bridge.translate(EvaluationError("x")), errors.ElementHandleError)
    assert isinstance(_bridge.translate(TargetClosedError("x")), errors.NetworkError)
    assert isinstance(_bridge.translate(ProtocolError("a: no response in 2s")),
                      errors.TimeoutError)
    gone = _bridge.translate(ProtocolError("Failed to find execution context with id = 3"))
    assert isinstance(gone, errors.ElementHandleError) and "id = 3" in str(gone)
    assert issubclass(errors.TimeoutError, asyncio.TimeoutError)


def test_the_emitter_calls_plain_handlers_and_schedules_coroutines():
    async def main():
        emitter = EventEmitter(asyncio.get_running_loop())
        seen = []
        emitter.on("x", lambda v: seen.append(("plain", v)))

        async def coro(v):
            seen.append(("coro", v))
        emitter.on("x", coro)
        emitter.once("x", lambda v: seen.append(("once", v)))
        emitter.emit("x", 1)
        emitter.emit("x", 2)
        await asyncio.sleep(0)
        # From another thread, the way the pipe's reader delivers.
        t = threading.Thread(target=emitter.emit_threadsafe, args=("x", 3))
        t.start()
        t.join()
        await asyncio.sleep(0.05)
        return seen
    seen = asyncio.run(main())
    assert ("once", 1) in seen and ("once", 2) not in seen
    assert ("plain", 3) in seen and ("coro", 3) in seen


class _Frame:
    def __init__(self, parent):
        self.parent = parent


class _Lifecycle:
    def __init__(self):
        self.frames = {"main": _Frame(None), "old-child": _Frame("main"),
                       "old-grandchild": _Frame("old-child")}


class _Engine:
    def __init__(self):
        self.lifecycle = _Lifecycle()
        self.main_frame_id = "main"


def test_a_committed_document_drops_the_frames_of_the_old_one():
    """The case measured on firefox-34: a back-forward-cache navigation left the
    old iframe in the lifecycle with a dead world, and it was listed."""
    page = Page.__new__(Page)
    page._engine = _Engine()
    page._frames = {}
    page._gone = set()
    page._forget_children("main")
    # The new document's frame attaches after the commit.
    page._engine.lifecycle.frames["new-child"] = _Frame("main")
    listed = [f._id for f in page.frames]
    assert listed == ["main", "new-child"]
