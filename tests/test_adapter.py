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
    # 0 means no timeout: None, which the core's waits read as no limit
    # (until 0.3.5 it was 24 hours).
    assert _bridge.ms({"timeout": 0}) is None


def test_engine_failures_become_pyppeteers_exceptions():
    from invisible_core.juggler.connection import ProtocolError, TargetClosedError
    from invisible_core.juggler.injected import EvaluationError
    assert isinstance(_bridge.translate(EvaluationError("x")), errors.ElementHandleError)
    assert isinstance(_bridge.translate(TargetClosedError("x")), errors.NetworkError)
    assert isinstance(_bridge.translate(ProtocolError("a: no response in 2s")),
                      errors.TimeoutError)
    # A vanished world comes typed from the core, and is pyppeteer's sentence.
    from invisible_core.juggler.injected import ContextDestroyed
    gone = _bridge.translate(ContextDestroyed("Execution context was destroyed"))
    assert isinstance(gone, errors.ElementHandleError)
    assert "most likely because of a navigation" in str(gone)
    # Another document committed in ours' place: a failed navigation, in the
    # core's words, which name both addresses (invisible-core 41.36.0).
    from invisible_core.juggler.lifecycle import NavigationInterrupted
    said = ('Navigation to "http://a/next" is interrupted by another '
            'navigation to "http://a/landed"')
    lost = _bridge.translate(NavigationInterrupted(said))
    assert isinstance(lost, errors.PageError) and str(lost) == said
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


def test_frames_are_the_lifecycles_and_their_events_its_announcements():
    """The case measured on firefox-34, and again on firefox-41 for a parent
    that moved to another process: the engine reports no detach for the old
    document's iframe. The rule that a new document drops the old one's frames
    is the core's now (`tests/test_a_frame_that_moves_keeps_its_state.py`
    there); this page lists what the lifecycle holds and reports what it
    announces, and the main frame re-announced by the move is not a new one."""
    from invisible_core.juggler.connection import EventListeners
    from invisible_core.juggler.lifecycle import Lifecycle

    conn = EventListeners()
    lifecycle = Lifecycle(conn, "S")
    page = Page.__new__(Page)
    page._engine = type("E", (), {"lifecycle": lifecycle, "main_frame_id": "main"})()
    page._frames = {}
    emitted = []
    page.emit_threadsafe = lambda name, frame: emitted.append((name, frame._id))
    # The page is built after its main frame was seen, as in the engine.
    conn.dispatch_event("Page.frameAttached", {"frameId": "main"}, "S")
    lifecycle.announce = page._on_lifecycle
    for method, params in [
        ("Page.frameAttached", {"frameId": "old-child", "parentFrameId": "main"}),
        ("Page.frameAttached", {"frameId": "main"}),
        ("Page.navigationCommitted", {"frameId": "main", "navigationId": "n2",
                                      "url": "http://b/"}),
        ("Page.frameAttached", {"frameId": "new-child", "parentFrameId": "main"}),
    ]:
        conn.dispatch_event(method, params, "S")
    assert [f._id for f in page.frames] == ["main", "new-child"]
    assert emitted == [("frameattached", "old-child"),
                       ("framedetached", "old-child"),
                       ("frameattached", "new-child")], emitted


@pytest.mark.parametrize("raised, said", [
    ("interrupted", 'Navigation to "http://a/next" is interrupted by another '
                    'navigation to "http://a/landed"'),
    ("aborted", "NS_ERROR_UNKNOWN_HOST at http://a/next"),
])
def test_goto_words_an_interruption_once_and_an_abort_with_its_url(raised, said):
    """An abort is the engine's error text, and goto adds the address as
    pyppeteer does ("net::ERR_... at <url>"). An interruption already names
    both addresses, and adding the url again would say it twice."""
    from invisible_core.juggler.lifecycle import NavigationError, NavigationInterrupted

    error = (NavigationInterrupted(said) if raised == "interrupted"
             else NavigationError("NS_ERROR_UNKNOWN_HOST"))

    def goto(url, **kwargs):
        raise error

    page = Page.__new__(Page)
    page._engine = type("E", (), {"lifecycle": type("L", (), {
        "goto": staticmethod(goto)})(), "main_frame_id": "main"})()
    page._navigation_timeout_s = 5
    with pytest.raises(errors.PageError) as e:
        asyncio.run(page.goto("http://a/next"))
    assert str(e.value) == said
