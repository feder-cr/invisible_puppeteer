"""The places where this client drives the shared engine differently from invisible_playwright.

Each test here fails on the ORIGINAL behaviour - it was run against the
unadapted code first - and passes on the adaptation:

  1. every pointer approach is drawn by the engine (there, a client-side
     wrapper draws it and the engine sends one jump);
  3. `fill("")` deletes the selected text, and leaves `change` to the blur
     (there, the field keeps its value).

Two more lived here until firefox-39 and left for the engine: a dialog opened
by the action ended the client's wait for `Page.pointerLanded`, and
`Connection.send` could be interrupted by the caller for that wait. The landing
now comes back with the dispatch, and the engine ends that wait itself when a
dialog opens; the Playwright wrapper's e2e checks it against a real dialog
(`test_a_click_that_opens_a_dialog_still_answers`). [B230]
"""
from __future__ import annotations


import pytest

from invisible_core.juggler.actions import Actions

pytestmark = pytest.mark.unit

POINT = (400.0, 300.0)


class _Lifecycle:
    main_frame = "main"


class _Keyboard:
    def __init__(self):
        self.pressed = []

    def modifier_mask(self):
        return 0

    def press(self, key, **kw):
        self.pressed.append(key)


class _Connection:
    def __init__(self):
        self.sent = []

    def send(self, method, params=None, session=None, timeout=None):
        self.sent.append((method, dict(params or {})))
        if method == "Page.dispatchMouseEvent":
            if "landsOn" in (params or {}):
                return {"landing": {"type": params["type"], "landed": True,
                                    "seen": 1, "on": ""}}
            return {}
        if method == "Page.getContentQuads":
            x, y = POINT
            return {"quads": [{"p1": {"x": x - 10, "y": y - 5},
                               "p2": {"x": x + 10, "y": y - 5},
                               "p3": {"x": x + 10, "y": y + 5},
                               "p4": {"x": x - 10, "y": y + 5}}]}
        return {}

    def moves(self):
        return [p for m, p in self.sent
                if m == "Page.dispatchMouseEvent" and p.get("type") == "mousemove"]


class _Injected:
    def __init__(self, fill_result="needsinput"):
        self.fill_result = fill_result

    def query_selector(self, frame, selector, strict=False):
        return "element"

    def element_states(self, frame, element, states):
        return {"ok": True}

    def check_hit_target(self, frame, element, point):
        return "done"

    def scroll_into_view(self, frame, element):
        return False

    def dispose(self, frame, element):
        pass

    def evaluate(self, frame, expression, **kw):
        return {"w": 1280, "h": 800}

    def call(self, frame, declaration, *args, **kw):
        if "injected.fill(" in declaration:
            return self.fill_result
        return "done"


def _actions(conn=None, injected=None, motion=True) -> Actions:
    a = Actions.__new__(Actions)
    a.lifecycle = _Lifecycle()
    a.inj = injected or _Injected()
    a.c = conn or _Connection()
    a.session = "s"
    a.keyboard = _Keyboard()
    a.position = (0.0, 0.0)
    a.pointer_persona = None
    # The core's Actions numbers every act of the page and is told who draws
    # the approach; this client has no cursor of its own.
    from invisible_core.juggler._behaviour import PageActs
    a.acts = PageActs(1)
    a.engine_approach = True
    if motion:
        from invisible_core.seedmix import sub_seed
        from invisible_core.juggler._motion import CursorMotion
        a.motion = CursorMotion(sub_seed(42, "server:drag"))
    return a


def test_a_click_travels_to_its_target_along_a_path():
    a = _actions()
    a.click("#b", timeout=2.0)
    moves = a.c.moves()
    assert len(moves) > 3, "the pointer jumped: %d move(s)" % len(moves)
    assert (moves[-1]["x"], moves[-1]["y"]) == POINT


def test_without_a_generator_the_approach_is_one_event():
    a = _actions(motion=False)
    a.click("#b", timeout=2.0)
    assert len(a.c.moves()) == 1


def test_a_hover_leaves_its_last_move_to_the_commit():
    """The approach stops one waypoint short, so the point is reached ONCE."""
    a = _actions()
    a.hover("#b", timeout=2.0)
    moves = a.c.moves()
    at_point = [m for m in moves if (m["x"], m["y"]) == POINT]
    assert len(moves) > 3 and len(at_point) == 1


def test_clearing_a_field_deletes_the_selected_text():
    """The Delete is the whole of it. `change` used to be requested from the
    engine as well, while the field still had focus: Firefox fired a second
    one at the blur, and a contenteditable got one it can never fire. The
    engine has no such command any more, and `change` comes from the blur."""
    a = _actions()
    a.fill("#b", "", timeout=2.0)
    assert a.keyboard.pressed == ["Delete"]
    sent = [m for m, _ in a.c.sent]
    assert not {"Page.dispatchTrustedInputEvents", "Page.setUserInput",
                "Page.selectOptions"} & set(sent), sent
