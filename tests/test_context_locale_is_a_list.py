"""The session language is the core's decision, and this package only reads it.

A context's locale reaches the engine as the language LIST it stands for.
`Browser.setLocaleOverride` becomes the BrowsingContext's LanguageOverride on
an engine that applies a context's locale: the field navigator.languages is
split from, the realm's Intl locale is taken from and the Accept-Language
header is prepared from. The list is the core's
`decide_session_locale(tag).accept_languages`, so a context asking for de-DE
reports the four entries a German Firefox reports, not `["de-DE"]`.

And since invisible-core 36.32.0 the core decides the session language once
(`prepare_session_geo(...).locale`, a SessionLocale): the session's default
context carries that decision's primary, and this package has no decision of
its own. The guards at the bottom fail when an "auto" branch or one of the
removed core names comes back into the source.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from invisible_core import SessionLocale, decide_session_locale, generate_profile
from invisible_puppeteer import _session
from invisible_puppeteer._juggler.browser import Browser
from invisible_puppeteer._juggler.connection import EventListeners

_invisible_puppeteer_DIR = pathlib.Path(__file__).resolve().parent.parent / "src" / "invisible_puppeteer"
_LAUNCH_FILE = _invisible_puppeteer_DIR / "launcher.py"


class _Connection(EventListeners):
    def __init__(self):
        super().__init__()
        self.sent = []

    def send(self, method, params=None, session=None, timeout=30):
        self.sent.append((method, params))
        return {}


def _locale_commands(options):
    conn = _Connection()
    Browser(conn, "151.0").apply_context_options("CTX", options)
    return [p for m, p in conn.sent if m == "Browser.setLocaleOverride"]


@pytest.mark.unit
@pytest.mark.parametrize("locale", ["de-DE", "it-IT", "en-US", "en-GB", "pt_BR"])
def test_the_locale_goes_out_as_the_core_list(locale):
    assert _locale_commands({"locale": locale}) == [
        {"browserContextId": "CTX",
         "locale": decide_session_locale(locale).accept_languages}]


@pytest.mark.unit
def test_de_de_is_the_list_a_german_firefox_reports():
    # Written out once, so a change in the core's table shows up here as a
    # decision rather than passing through silently.
    assert _locale_commands({"locale": "de-DE"})[0]["locale"] == (
        "de-DE, de, en-US, en")


@pytest.mark.unit
def test_the_default_context_gets_the_list_too():
    """The session's launch locale arrives here, addressed by absence."""
    conn = _Connection()
    Browser(conn, "151.0").apply_context_options(None, {"locale": "it-IT"})
    assert [p for m, p in conn.sent if m == "Browser.setLocaleOverride"] == [
        {"locale": "it-IT, it, en-US, en"}]


@pytest.mark.unit
def test_no_locale_sends_nothing():
    """The context then keeps the session's list, seeded by the engine."""
    assert _locale_commands({}) == []


@pytest.mark.unit
def test_a_context_locale_of_auto_is_refused_without_the_network(monkeypatch):
    """"auto" is decided once, at launch, from the egress. This side does not
    know the proxy, so deciding it here would discover the HOST's address and
    pair the home country's language with the proxy's timezone. Known-bad:
    the server calling the core's launch decision instead of SessionLocale.of."""
    from invisible_core import _geo

    def _no_network(*_a, **_k):
        raise AssertionError("a context locale must not discover anything")
    monkeypatch.setattr(_geo, "discover_egress_ip", _no_network)
    with pytest.raises(ValueError, match="auto"):
        _locale_commands({"locale": "auto"})


# -- the default context reads the session decision --------------------------

class _Decided(_session.CommonLaunch):
    """A session past the geo step of the launch, without the network."""

    def __init__(self, requested):
        self._profile = generate_profile(42)
        self._timezone = "Europe/London"
        self._locale = decide_session_locale(requested)


@pytest.mark.unit
@pytest.mark.parametrize("requested", ["en-AU", "fr-FR", "de-DE", "it-IT", "en-GB"])
def test_the_default_context_locale_is_the_decision_s_primary(requested):
    """The context option is ONE tag, and it is what navigator.language reports.

    An Australian session decides `en-US, en` (Firefox has no en-AU build); a
    default context carrying the requested "en-AU" would be the second answer
    the 36.32.0 core removed.
    """
    session = _Decided(requested)
    assert isinstance(session._locale, SessionLocale)
    assert session._default_context_options()["locale"] == session._locale.primary


@pytest.mark.unit
def test_an_australian_session_s_default_context_is_en_us():
    assert _Decided("en-AU")._default_context_options()["locale"] == "en-US"


@pytest.mark.unit
@pytest.mark.parametrize("requested", [
    "en-AU", "en-ZA", "en-CA", "fr-BE", "de-CH", "he-IL", "ja-JP", "zh-CN",
    "zh-TW", "nb-NO", "ro-RO", "pt-BR", "es-MX", "sl-SI"])
def test_the_default_context_gets_back_the_list_the_profile_declared(requested):
    """primary -> the engine browser -> the engine must land on the launch list."""
    session = decide_session_locale(requested)
    assert _locale_commands({"locale": session.primary}) == [
        {"browserContextId": "CTX", "locale": session.accept_languages}]


# -- no locale decision of its own ---------------------------------------------

_REMOVED_CORE_NAMES = {"resolve_session_locale", "consent_region_lang",
                       "accept_languages"}


def _sources():
    for path in sorted(_invisible_puppeteer_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path, ast.parse(path.read_text(encoding="utf-8"))


@pytest.mark.unit
def test_no_module_imports_a_removed_locale_name():
    offenders = []
    for path, tree in _sources():
        for node in ast.walk(tree):
            # `SessionLocale.accept_languages` is the property to read; what
            # must not come back is the core MODULE's function of that name.
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("invisible_core"):
                names = {a.name for a in node.names}
            elif (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                  and node.value.id == "invisible_core"):
                names = {node.attr}
            else:
                continue
            for name in names & _REMOVED_CORE_NAMES:
                offenders.append(f"{path.name}:{node.lineno} {name}")
    assert not offenders, offenders


def _mentions_locale(node):
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and "locale" in sub.id.lower():
            return True
        if isinstance(sub, ast.Attribute) and "locale" in sub.attr.lower():
            return True
    return False


@pytest.mark.unit
def test_no_module_compares_a_locale_with_auto():
    """The `if locale == "auto"` branch lives in the core only."""
    offenders = []
    for path, tree in _sources():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            consts = [n for n in [node.left, *node.comparators]
                      if isinstance(n, ast.Constant) and n.value == "auto"]
            if consts and _mentions_locale(node):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, offenders


@pytest.mark.unit
def test_the_launch_hands_the_requested_locale_to_the_one_geo_call():
    tree = ast.parse(_LAUNCH_FILE.read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "prepare_session_geo"]
    assert len(calls) == 1
    assert any(isinstance(a, ast.Attribute) and a.attr == "_locale_requested"
               for a in calls[0].args)
