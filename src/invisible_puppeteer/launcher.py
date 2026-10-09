"""`launch()`: the patched Firefox, launched and driven over Juggler.

⛔ THIS IS invisible_playwright's LAUNCHER, ADAPTED. The session's geography
and locale, the sealed executable, the hidden surface, the fingerprint prefs,
the proxy and the process token are resolved in the same order and by the same
`CommonLaunch`, and the result goes to the engine's `launch()` directly - no
Playwright client, no driver process. What the caller gets is pyppeteer's
Browser.

pyppeteer's own options are honoured where they mean something for this
browser (``headless``, ``executablePath``, ``args``, ``userDataDir``, ``env``,
``defaultViewport``, ``ignoreHTTPSErrors``, ``slowMo``) and accepted without
effect where they describe Chromium's process management (``devtools``,
``dumpio``, ``handleSIG*``, ``autoClose``, ``logLevel``, ``loop``,
``ignoreDefaultArgs``, ``appMode``). invisible_playwright's keyword arguments
(``seed``, ``pin``, ``proxy``, ``humanize``, ``locale``, ``timezone``,
``extra_prefs``, ``prep_recaptcha``, ``show_cursor``) are added.
"""
from __future__ import annotations

import asyncio
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from invisible_core import configure_proxy as _configure_proxy_shared
from invisible_core import SessionLocale, persona_cookies, prepare_session_geo
from invisible_core._fpforge import Profile, generate_profile

from . import _session, errors
from ._engine import assert_wire_version, resolve_executable
from ._humanize import ENGINE_BINARY, resolve_cursor_engine
from ._juggler.browser import launch as launch_engine
from ._reaper import SessionToken, guard_for
from .browser import Browser


def _motion_available() -> bool:
    try:
        from invisible_core.juggler import CursorMotion  # noqa: F401
    except Exception:
        return False
    return True


class _Session(_session.CommonLaunch):
    """One launched browser's lifetime: everything `InvisiblePlaywright`'s
    `__init__`, `__enter__` and `_teardown` do, without Playwright."""

    def __init__(self, *, seed, pin, headless, proxy, extra_args, humanize,
                 locale, timezone, extra_prefs, binary_path, profile_dir,
                 prep_recaptcha, show_cursor, env_overrides, context_overrides):
        # `zoom.stealth.fpp.hw_seed` is int32_t, so the seed stays in int31.
        self.seed: int = int(seed) if seed is not None else secrets.randbits(31)
        self._pin = pin
        self._headless = headless
        self._proxy = proxy
        self._extra_args = list(extra_args or [])
        self._humanize = humanize
        self._cursor_engine = resolve_cursor_engine(humanize, _motion_available)
        self._show_cursor = None if show_cursor is None else bool(show_cursor)
        # What the caller ASKED for: "auto" or a tag. The decision is the
        # core's, made at launch from the egress (prepare_session_geo), and
        # there is none before it.
        self._locale_requested = locale
        self._locale: Optional[SessionLocale] = None
        self._timezone = timezone
        self._extra_prefs = extra_prefs or None
        self._binary_path = binary_path
        self._profile_dir: Optional[Path] = Path(profile_dir) if profile_dir else None
        self._prep_recaptcha = bool(prep_recaptcha) and self._profile_dir is None
        self._profile: Profile = generate_profile(self.seed, pin=self._pin)
        self._virtual_display: Any = None
        self._session_token = SessionToken()
        self._lifetime_guard = guard_for()
        self._webrtc_egress_ip: Optional[str] = None
        self._srflx_declared: Optional[str] = None
        self._ultimo_controllo_uscita: float = 0.0
        self._uscite_non_misurabili: int = 0
        self._env_overrides = dict(env_overrides or {})
        self._context_overrides = dict(context_overrides or {})
        self.engine_browser = None
        self.engine_context = None
        self.context_options: Dict[str, Any] = {}

    def start(self) -> None:
        """⛔ THE ORDER IS THE ORIGINAL'S: geography first (the timezone and the
        WebRTC declaration feed the prefs), the hidden surface before the prefs
        (B172's sandbox workarounds depend on whether it exists), the token
        before the environment (the environment carries it)."""
        try:
            # The language is decided in the same call, from the same egress:
            # the core resolves "auto" and applies Firefox's language table, and
            # the session keeps the DECISION (a SessionLocale), never a tag.
            geo = prepare_session_geo(self._timezone, self._proxy,
                                      self._locale_requested)
            self._timezone = geo.timezone
            self._locale = geo.locale
            self._webrtc_egress_ip = geo.egress_ip
            self._srflx_declared = geo.srflx_to_declare()
            executable = resolve_executable(self._binary_path)
            true_headless = self._resolve_headless()
            prefs = self._build_prefs()
            engine_proxy = _configure_proxy_shared(self._proxy, prefs)
            self._session_token = SessionToken.mint()
            env = self._build_env(prefs)
            env.update({k: str(v) for k, v in self._env_overrides.items()})
            if self._profile_dir is not None:
                self._profile_dir.mkdir(parents=True, exist_ok=True)
            browser = launch_engine(
                str(executable), prefs=prefs,
                profile_dir=str(self._profile_dir) if self._profile_dir else None,
                env=env, args=self._extra_args, headless=true_headless,
                proxy=engine_proxy)
            self.engine_browser = browser
            assert_wire_version(browser)
            self._bind_process_tree()
            options = self._default_context_options()
            options.update(self._context_overrides)
            self.context_options = options
            if self._profile_dir is not None:
                context = browser.default_context(options)
            else:
                context = browser.new_context(options)
            if self._prep_recaptcha:
                # The cookie list is the core's (persona_cookies, pure data);
                # handing it to the engine context is the only part kept here.
                # A failure to seed never fails the launch, as before.
                try:
                    context.set_cookies(persona_cookies(self._profile, self._locale))
                except Exception:
                    pass
            self.engine_context = context
            # pyppeteer's launch leaves one page open; a persistent profile
            # already has one of its own, and it is reused.
            if not context.live_targets():
                self._before_new_page()
                context.new_page()
        except BaseException:
            self._teardown()
            raise

    def _prepare_page(self, engine_page) -> None:
        """⛔ With `INVPW_CURSOR_ENGINE=binary` the BROWSER draws the path, so
        the engine must not draw one too."""
        if self._cursor_engine == ENGINE_BINARY:
            engine_page.actions.motion = None

    def _before_new_page(self) -> None:
        self._assert_uscita_invariata()

    # The egress guard, from invisible_playwright's launcher.
    _INTERVALLO_CONTROLLO_USCITA_S = 120.0
    _MAX_USCITE_NON_MISURABILI = 3

    def _assert_uscita_invariata(self) -> None:
        """Refuses if the proxy's egress IP changed since launch: the WebRTC
        srflx declared at launch would then disagree with the page's address."""
        if not self._proxy or not self._webrtc_egress_ip:
            return
        now = time.monotonic()
        if now - self._ultimo_controllo_uscita < self._INTERVALLO_CONTROLLO_USCITA_S:
            return
        self._ultimo_controllo_uscita = now
        outcome, current = _session.egress_ancora_valido(
            self._proxy, self._webrtc_egress_ip)
        if outcome == _session.USCITA_DERIVATA:
            raise _session.ProxyEgressDrifted(
                "the proxy's egress IP changed during the session: it was %s "
                "at launch, now it is %s. Use a proxy that holds the session "
                "sticky, or shorten the session." % (self._webrtc_egress_ip, current))
        if outcome == _session.USCITA_NON_MISURABILE:
            self._uscite_non_misurabili += 1
            if self._uscite_non_misurabili >= self._MAX_USCITE_NON_MISURABILI:
                raise _session.ProxyEgressNonVerificabile(
                    "the egress IP was not verifiable for %d checks in a row"
                    % self._uscite_non_misurabili)
            return
        self._uscite_non_misurabili = 0

    def _teardown(self) -> None:
        """Browser, hidden surface, then anything left of the process tree -
        each step on its own, and the reap last and unconditional."""
        if self.engine_browser is not None:
            try:
                self.engine_browser.close()
            except Exception:
                pass
            self.engine_browser = None
        if self._virtual_display is not None:
            try:
                self._virtual_display.stop()
            except Exception:
                pass
            self._virtual_display = None
        if self._session_token:
            try:
                self._lifetime_guard.reap(self._session_token)
            except Exception:
                pass
            self._session_token = SessionToken()


def _merge(options: Optional[dict], kwargs: dict) -> dict:
    out = dict(options or {})
    out.update(kwargs)
    return out


async def launch(options: dict = None, **kwargs: Any) -> Browser:
    """Start the patched Firefox and return a :class:`Browser`.

    Same call as pyppeteer's ``launch``. ``headless`` defaults to ``True`` as
    it does there - and here that means a hidden desktop with the real
    rendering pipeline, not Firefox's headless mode, so the fingerprint is the
    same either way.

    ⛔ ``defaultViewport`` DEFAULTS TO THE PROFILE, NOT TO 800x600. pyppeteer's
    800x600 would contradict the screen the fingerprint declares; passing a
    viewport explicitly is honoured, and ``None`` keeps the profile's.
    """
    options = _merge(options, kwargs)
    context_overrides: Dict[str, Any] = {}
    viewport = options.get("defaultViewport")
    if isinstance(viewport, dict):
        context_overrides["viewport"] = {"width": int(viewport["width"]),
                                         "height": int(viewport["height"])}
        if viewport.get("deviceScaleFactor"):
            context_overrides["deviceScaleFactor"] = viewport["deviceScaleFactor"]
        if viewport.get("isMobile"):
            context_overrides["isMobile"] = True
        if viewport.get("hasTouch"):
            context_overrides["hasTouch"] = True
    if options.get("ignoreHTTPSErrors"):
        context_overrides["ignoreHTTPSErrors"] = True
    session = _Session(
        seed=options.get("seed"), pin=options.get("pin"),
        headless=bool(options.get("headless", True)),
        proxy=options.get("proxy"), extra_args=options.get("args"),
        humanize=options.get("humanize", True),
        locale=options.get("locale", "auto"),
        timezone=options.get("timezone", ""),
        extra_prefs=options.get("extra_prefs"),
        binary_path=options.get("executablePath"),
        profile_dir=options.get("userDataDir"),
        prep_recaptcha=options.get("prep_recaptcha", False),
        show_cursor=options.get("show_cursor"),
        env_overrides=options.get("env"),
        context_overrides=context_overrides)
    await asyncio.to_thread(session.start)
    loop = asyncio.get_running_loop()
    browser = Browser(session, session.engine_browser, session.engine_context,
                      loop, slow_mo=options.get("slowMo", 0),
                      context_options=session.context_options)
    browser.seed = session.seed
    # The page pyppeteer's launch leaves open, wrapped now so its events are
    # already being listened to.
    await browser._default_context.pages()
    return browser


async def connect(options: dict = None, **kwargs: Any) -> Browser:
    raise errors.BrowserError(
        "connect() is not supported: there is no remote endpoint, the browser "
        "is launched by launch() and driven over its own pipe")


def executablePath() -> str:
    """The sealed engine's executable, downloaded if missing."""
    return str(resolve_executable(None))


def defaultArgs(options: dict = None, **kwargs: Any) -> List[str]:
    """Extra command-line arguments: none are added by default."""
    return list(_merge(options, kwargs).get("args") or [])


__all__ = ["launch", "connect", "executablePath", "defaultArgs"]
