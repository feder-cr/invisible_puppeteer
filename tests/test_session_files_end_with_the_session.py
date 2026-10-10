"""The session's directories end with the session, through the core's `SessionFiles` (B223, B267).

`launch` makes them and `Browser.close()` takes them away after the browser; what
`SessionFiles.remove` does about the children that outlive the browser is the
core's to test. Here: the wiring, and every way out of `launch`.
"""
from __future__ import annotations

import os
import pathlib
import tempfile

import pytest

from invisible_core.juggler.connection import EventListeners
from invisible_puppeteer._juggler import browser as module
from invisible_puppeteer._juggler.browser import EngineError, launch

pytestmark = pytest.mark.unit


class FakeConnection(EventListeners):
    """What `launch` and `close` touch, and whether the profile was still there
    when the browser was closed."""

    def __init__(self, profile, refuse_proxy=False):
        super().__init__()
        self._profile = pathlib.Path(profile)
        self._refuse_proxy = refuse_proxy
        self.profile_at_close = None

    def send(self, method, params=None, session=None, timeout=30):
        if method == "Browser.setBrowserProxy" and self._refuse_proxy:
            raise RuntimeError("refused")
        return {}

    def close(self):
        self.profile_at_close = self._profile.exists()


@pytest.fixture
def private_temp(tmp_path, monkeypatch):
    """Every session directory of these tests lands here, so "nothing left"
    is a listing of an empty directory and not a race with another process."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return tmp_path


def _launching(seen, refuse_proxy=False, fail=False):
    def fake_launch(executable, profile_dir, **kwargs):
        seen.append((profile_dir, kwargs["env"]))
        if fail:
            raise RuntimeError("the browser exited during startup")
        return FakeConnection(profile_dir, refuse_proxy)
    return fake_launch


def test_the_browser_runs_on_the_session_s_files_and_close_takes_them_away(private_temp, monkeypatch):
    """⛔ KNOWN-BAD (B223): the profile was removed right after the browser,
    while a child of it still held a file inside, and the browser's temporary
    files went to the system directory (B267). Both are `SessionFiles` now,
    and `close()` hands them to it after the browser."""
    seen = []
    monkeypatch.setattr(module.connection, "launch", _launching(seen))
    b = launch("x", prefs={"a": True}, env={"PATH": "p"})
    (profile, env), = seen
    profile, tmp = pathlib.Path(profile), pathlib.Path(env["TMP"])
    assert (profile / "user.js").exists()
    assert env["TEMP"] == env["TMPDIR"] == str(tmp) and env["PATH"] == "p"
    assert tmp.is_dir() and tmp.parent == private_temp
    b.close()
    assert b.conn.profile_at_close is True, (
        "the profile was already gone when the browser was closed")
    assert os.listdir(private_temp) == [], "the session left its directories behind"
    b.close()


def test_a_profile_the_caller_named_survives(private_temp, monkeypatch, tmp_path_factory):
    theirs = tmp_path_factory.mktemp("caller_owns")
    seen = []
    monkeypatch.setattr(module.connection, "launch", _launching(seen))
    b = launch("x", profile_dir=str(theirs), env={})
    assert seen[0][0] == str(theirs)
    b.close()
    assert (theirs / "user.js").exists(), "the CALLER's profile was deleted"
    assert os.listdir(private_temp) == []


def test_a_launch_that_fails_leaves_nothing_behind(private_temp, monkeypatch):
    seen = []
    monkeypatch.setattr(module.connection, "launch", _launching(seen, fail=True))
    with pytest.raises(RuntimeError):
        launch("x", env={})
    assert seen, "the engine was never asked"
    assert os.listdir(private_temp) == []


def test_a_proxy_the_engine_refuses_leaves_nothing_behind(private_temp, monkeypatch):
    seen = []
    monkeypatch.setattr(module.connection, "launch",
                        _launching(seen, refuse_proxy=True))
    with pytest.raises(EngineError):
        launch("x", env={}, proxy={"server": "socks5://127.0.0.1:1"})
    assert seen, "the engine was never asked"
    assert os.listdir(private_temp) == []


def test_a_proxy_that_cannot_be_expressed_leaves_nothing_behind(private_temp, monkeypatch):
    seen = []
    monkeypatch.setattr(module.connection, "launch", _launching(seen))
    with pytest.raises(EngineError):
        launch("x", env={}, proxy={"server": "ftp://127.0.0.1:21"})
    assert seen == [], "the browser was started for a proxy that cannot be applied"
    assert os.listdir(private_temp) == []
