"""Every protocol name this package writes is one the engine declares.

The mirror itself, and the tests that talk to a browser over the pipe, are the
core's and the Playwright wrapper's since 0.3.0: this package used to carry a
copy of both, a command behind. What stays is the one question about THIS
package's own code.
"""
from __future__ import annotations

import pytest

from invisible_core.juggler.protocol import COMMANDS, EVENTS

pytestmark = pytest.mark.unit


def test_every_protocol_name_this_package_writes_is_declared():
    """⛔ A COMMAND THE ENGINE NO LONGER HAS IS FOUND HERE, NOT IN A BROWSER.

    The mirror is regenerated from the shipped engine; the code that calls it
    is not. When `Page.dispatchTrustedInputEvents` left the engine, the mirror
    lost it and `actions.py` still sent it: nothing in the default selection
    noticed, and the first `select_option` against the new engine failed. So
    every `"Domain.name"` literal in the package must be a command or an event
    of the mirror. The known-bad input: put that old name back in a `send`.
    """
    import pathlib
    import re

    import invisible_puppeteer

    pattern = re.compile(
        r"""["']((?:Browser|Page|Network|Runtime|Heap)\.[a-z]\w*)["']""")
    root = pathlib.Path(invisible_puppeteer.__file__).parent
    unknown = {}
    for source in sorted(root.rglob("*.py")):
        if source.name == "protocol.py":
            continue
        for name in pattern.findall(source.read_text(encoding="utf-8")):
            if name not in COMMANDS and name not in EVENTS:
                unknown.setdefault(name, []).append(source.name)
    assert not unknown, "not in the protocol mirror: %r" % unknown
