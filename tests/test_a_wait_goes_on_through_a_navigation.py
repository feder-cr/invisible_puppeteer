"""`waitForFunction` asks the new document when the old one went under a turn.

pyppeteer's WaitTask runs again in the document that replaced the one it was
polling. Since 0.3.7 the predicate is sent once per turn ([B285]): a world
that went under it is the core's `ContextDestroyed`, and the next turn asks
again, rather than the wait ending on a navigation or the turn being sent
twice.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from invisible_core.juggler.injected import ContextDestroyed
from invisible_puppeteer.page import Frame

pytestmark = pytest.mark.unit


def test_a_turn_whose_world_went_is_asked_again():
    answers = [ContextDestroyed("Execution context was destroyed"),
               {"value": False}, {"value": True}]
    sent = []

    def evaluate_sync(page_function, args, *, by_value, force_expr):
        sent.append(page_function)
        answer = answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    frame = Frame.__new__(Frame)
    frame._evaluate_sync = evaluate_sync
    # A frame still attached: the wait asks the lifecycle on every turn.
    frame._id = "F1"
    frame._page = SimpleNamespace(_engine=SimpleNamespace(
        lifecycle=SimpleNamespace(frame=lambda fid: object())))
    handle = asyncio.run(frame.waitForFunction(
        "() => window.ready", {"timeout": 5000, "polling": 10}))
    assert handle._remote == {"value": True}
    assert len(sent) == 3
