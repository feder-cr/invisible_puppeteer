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

from invisible_core.juggler.connection import (
    EventListeners, ProtocolError, ReplyTimeout)
from invisible_puppeteer._juggler import browser as module
from invisible_puppeteer._juggler.browser import EngineError, launch

pytestmark = pytest.mark.unit


class FakeConnection(EventListeners):
    """What `launch` and `close` touch, and whether the profile was still there
    when the browser was closed."""

    def __init__(self, profile, silent_enable=False):
        super().__init__()
        self._profile = pathlib.Path(profile)
        self._silent_enable = silent_enable
        self.profile_at_close = None

    def startup_send(self, method, params=None):
        """The real one bounds the command by what is left of the launch;
        `silent_enable` is a `Browser.enable` past that deadline."""
        if method == "Browser.enable" and self._silent_enable:
            raise ReplyTimeout("Timeout 60000ms exceeded while the browser "
                               "was starting: Browser.enable: no response")
        return self.send(method, params, timeout=None)

    def send(self, method, params=None, session=None, timeout=30):
        return {}

    def close(self):
        self.profile_at_close = self._profile.exists()


@pytest.fixture
def private_temp(tmp_path, monkeypatch):
    """Every session directory of these tests lands here, so "nothing left"
    is a listing of an empty directory and not a race with another process."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return tmp_path


def _launching(seen, refuse_proxy=False, fail=False, silent_enable=False,
               silent_proxy=False, conns=None):
    """The core's `connection.launch`: it sets the proxy itself, and a proxy
    refused or unanswered closes the browser there before it raises."""
    def fake_launch(executable, profile_dir, **kwargs):
        seen.append((profile_dir, kwargs["env"]))
        if fail:
            raise RuntimeError("the browser exited during startup")
        conn = FakeConnection(profile_dir, silent_enable)
        if kwargs.get("proxy") is not None and refuse_proxy:
            conn.close()
            raise ProtocolError("the engine refused the proxy, so the browser "
                                "was closed rather than left running without one")
        if kwargs.get("proxy") is not None and silent_proxy:
            conn.close()
            raise ReplyTimeout("Timeout 60000ms exceeded while the browser was "
                               "starting: Browser.setBrowserProxy: no response")
        if conns is not None:
            conns.append(conn)
        return conn
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


def test_a_proxy_left_unanswered_is_a_timeout_not_a_refusal(private_temp, monkeypatch):
    """⛔ KNOWN-BAD (firefox-41 gate, Linux under load): the proxy command had
    10 s of its own, and its silence came out as "the engine refused the
    proxy" while the launch still had time. It is the core's launch timeout
    now, a TimeoutError, and the session's files still go."""
    seen = []
    monkeypatch.setattr(module.connection, "launch",
                        _launching(seen, silent_proxy=True))
    with pytest.raises(TimeoutError, match="exceeded") as caught:
        launch("x", env={}, proxy={"server": "socks5://127.0.0.1:1"})
    assert not isinstance(caught.value, EngineError)
    assert os.listdir(private_temp) == []


def test_the_caller_s_timeout_and_proxy_reach_the_core(private_temp, monkeypatch):
    calls = []

    def fake_launch(executable, profile_dir, **kwargs):
        calls.append(kwargs)
        return FakeConnection(profile_dir)

    monkeypatch.setattr(module.connection, "launch", fake_launch)
    launch("x", env={}, proxy={"server": "socks5://127.0.0.1:1080"},
           ready_timeout=12.5).close()
    assert calls[0]["ready_timeout"] == 12.5
    assert calls[0]["proxy"]["host"] == "127.0.0.1"
    assert calls[0]["proxy"]["port"] == 1080


def test_a_browser_that_fails_to_enable_is_closed_and_leaves_nothing(private_temp, monkeypatch):
    """`Browser.enable` is the last step of the startup: a launch that fails
    there closes the browser it started and takes the session's files."""
    seen, conns = [], []
    monkeypatch.setattr(module.connection, "launch",
                        _launching(seen, silent_enable=True, conns=conns))
    with pytest.raises(TimeoutError):
        launch("x", env={})
    conn, = conns
    assert conn.profile_at_close is True, "the browser was not closed, or closed after its profile went"
    assert os.listdir(private_temp) == []


def test_a_proxy_that_cannot_be_expressed_leaves_nothing_behind(private_temp, monkeypatch):
    seen = []
    monkeypatch.setattr(module.connection, "launch", _launching(seen))
    with pytest.raises(EngineError):
        launch("x", env={}, proxy={"server": "ftp://127.0.0.1:21"})
    assert seen == [], "the browser was started for a proxy that cannot be applied"
    assert os.listdir(private_temp) == []
