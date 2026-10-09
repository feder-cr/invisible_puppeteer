"""A launched browser, its contexts and its pages - with no wrapper's API on top.

invisible_puppeteer's WebDriver and WebElement are an adapter over these
objects, the way invisible_playwright's in-process server is an adapter over
the same code inside `invisible_playwright/_juggler/server.py`, from which this
module was extracted on 2026-09-24: the launch, the event buffer, the context
options and the page wiring are that server's, with the Playwright channel
(guids, `__create__` ordering, the tagged value union) taken out.

⛔ THE COMMENTS BELOW ARE THAT SERVER'S, and the measurements they cite were
taken there. They are kept because they explain why the code has the shape it
has, and that reason did not change when the code was copied.
"""
from __future__ import annotations

import itertools
import tempfile
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from invisible_core import SessionLocale, parse_proxy

from invisible_core.juggler import connection
from invisible_core import write_user_js
from invisible_core.juggler import PageActs, read_version, remove_profile
from invisible_core.juggler.actions import Actions
from invisible_core.juggler.injected import InjectedScript
from invisible_core.juggler.lifecycle import Lifecycle


class EngineError(Exception):
    """A failure the caller should see with its reason.

    ⛔ Prefer this over a bare `Exception` for anything the caller could have
    caused: each adapter hands the message to its user, and "the engine refused
    the proxy" is a different day than "TypeError".
    """


# ── the session's rhythm, carried in the launch prefs ───────────────────────
#: How the session's SEED reaches the engine, which otherwise knows no seed at
#: all. The launcher composes the pref dict in full and `launch` already
#: receives it, so this adds no plumbing; `launch` takes the key back out
#: before writing `user.js`, so it never reaches a profile.
#:
#: ⛔ THE SEED CROSSES, NOT A PERSONA, and the second consumer is what settled
#: it. This carried a ready-made `TypingPersona` while the keyboard was the
#: only thing that wanted a rhythm. Then the click wanted one too, from a
#: DIFFERENT persona drawn from the same seed, and a transport carrying one
#: built object would have had to grow a second field - then a third. What is
#: actually session-scoped is the seed; a persona is something you build where
#: you use it.
#:
#: ⛔ NOT under `stealthfox.*`. That prefix means "the patched binary reads
#: this", and a reader who found this key there would go looking in C++ for
#: something answered in Python. The `server` in the name is historical: the
#: key was born in invisible_playwright's server, and renaming it would change
#: nothing a page can see while breaking every launcher that writes it.
SESSION_SEED_PREF = "invisible.server.session_seed"

#: The motion-duration cap `humanize=<seconds>` implies, in MILLISECONDS.
#:
#: ⛔ IT TRAVELS RATHER THAN BEING RE-DERIVED HERE, and in milliseconds because
#: Gecko has no float pref type - `1.0` written as a number arrives with the
#: right value and the wrong type. The cap is decided once, by the launcher,
#: from the caller's `humanize=`; the engine applying a cap of its own would be
#: a second answer to a question the caller answered, and would show up as a
#: drag that ignores a budget every click obeys.
MOTION_BUDGET_PREF = "invisible.server.motion_budget_ms"


def take_session_motion(prefs: Dict) -> tuple:
    """Split the launch prefs into what the BROWSER gets and what the SESSION
    keeps: the seed of its rhythms, and the budget one movement may spend.

    ⛔ A FUNCTION RATHER THAN THREE LINES INSIDE `launch`, because the thing
    worth testing is that the keys come OUT: a version that read them and forgot
    to remove them would behave identically in every observable way except for
    writing a session identifier into the profile, which no test that drives a
    browser would notice.

    ⛔ A MALFORMED VALUE RAISES rather than falling back to no rhythm. Emitting
    input at pipe speed is precisely the failure these personae exist to
    remove, and a fallback would make it the quiet default whenever the
    launcher sent something unexpected.
    """
    rest = dict(prefs)
    seed = rest.pop(SESSION_SEED_PREF, None)
    budget_ms = rest.pop(MOTION_BUDGET_PREF, None)
    return (rest,
            (None if seed is None else int(seed)),
            (None if budget_ms is None else int(budget_ms) / 1000.0))


def address(context_id: Optional[str], params: Dict) -> Dict:
    """The context a `Browser.*` command is about, in the form Juggler reads.

    ⛔ `None` IS THE DEFAULT CONTEXT, AND IT IS ADDRESSED BY ABSENCE. Juggler
    keeps every context in a Map keyed by its id and registers the default one
    under `undefined`, so an omitted field resolves to it and a `null` does not:
    `Map.get(null)` finds nothing, and the command fails naming no cause. Every
    command that names a context goes through here, so the rule lives once.
    """
    out = dict(params)
    if context_id is None:
        out.pop("browserContextId", None)
    else:
        out["browserContextId"] = context_id
    return out


# ── launch ──────────────────────────────────────────────────────────────────
def launch(executable: str, *, prefs: Optional[Dict] = None,
           profile_dir: Optional[str] = None,
           env: Optional[Dict[str, str]] = None,
           args: Optional[List[str]] = None, headless: bool = True,
           proxy: Optional[Dict[str, str]] = None,
           ready_timeout: float = 60.0) -> "Browser":
    """Start the patched Firefox and hand back a `Browser` ready to open pages.

    `env` is the WHOLE environment the browser gets, or None to inherit this
    process's. ⛔ A given environment is not a patch on top of `os.environ`:
    the session composes it in full and drops what its hidden surface names
    with `None` (the Wayland variables an Xvfb session must not carry), and a
    merge would bring those back - measured 2026-09-21 on 0.25.0, the hidden
    Firefox was born with `WAYLAND_DISPLAY` anyway.

    ⛔ WHO MAKES THE PROFILE TAKES IT AWAY - AND ONLY THAT ONE. A caller's
    `profile_dir` is theirs and survives the session by definition; a directory
    invented here is ours and is removed by `Browser.close()`. Measured on
    2026-08-28, after one day of development: 136 leftover
    `invisible_profile_*` directories, 5.0 GB. Nothing failed, nothing warned.
    """
    ours = profile_dir is None
    profile = profile_dir or tempfile.mkdtemp(prefix="invisible_profile_")
    # ⛔ THE TYPING SEED TRAVELS IN THE PREFS AND IS TAKEN OUT AGAIN HERE,
    # before a single byte reaches the profile. It is not a browser
    # preference: the engine never reads it, and leaving it in `user.js` would
    # write a session identifier onto disk for no reader at all.
    browser_prefs, session_seed, motion_budget_s = take_session_motion(
        prefs or {})
    write_user_js(profile, browser_prefs)
    # ⛔ PARSED BEFORE THE BROWSER STARTS, so a proxy we cannot express refuses
    # the launch instead of leaving a process running without one.
    proxy_command = None
    if proxy:
        try:
            proxy_command = parse_proxy(proxy).as_engine_command()
        except ValueError as exc:
            if ours:
                remove_profile(profile)
            raise EngineError("the proxy cannot be applied: %s" % exc)
    try:
        conn = connection.launch(executable, profile, headless=bool(headless),
                                 env=env, argv_extra=list(args or []),
                                 ready_timeout=ready_timeout)
    except BaseException:
        if ours:
            remove_profile(profile)
        raise
    # ⛔ AND SENT BEFORE ANY PAGE EXISTS. Until 2026-08-30 `proxy=` was
    # accepted and dropped for every scheme the engine prefs do not carry: a
    # page resolved its own DNS and went out on the host address while the
    # session's timezone, locale and WebRTC candidate had all been resolved
    # THROUGH the proxy. Announcing one country and connecting from another is
    # worse than having no proxy at all.
    if proxy_command is not None:
        try:
            conn.send("Browser.setBrowserProxy", proxy_command, timeout=10)
        except BaseException as exc:
            conn.close()
            if ours:
                remove_profile(profile)
            raise EngineError(
                "the engine refused the proxy, so the browser was closed "
                "rather than left running without one: %s" % exc)
    return Browser(conn, read_version(executable),
                   session_seed=session_seed, motion_budget_s=motion_budget_s,
                   profile_dir=profile if ours else None)


# ── browser ─────────────────────────────────────────────────────────────────
class Browser:
    """One running Firefox, reached over its Juggler pipe."""

    #: A page that never gets a `Page` object would otherwise buffer for the
    #: life of the browser. Enough to hold the burst that precedes a `newPage`
    #: reply, far too little to be a leak.
    BUFFER_CAP = 256

    #: Context options that are ENGINE state, with the Juggler command and the
    #: field name each one travels in. The option names are the ones the
    #: protocol and Playwright share (`timezoneId`, `viewport`, `screen`...);
    #: an adapter with its own vocabulary translates into these.
    #:
    #: ⛔ RECEIVING AN OPTION IS NOT APPLYING IT. These once arrived, were
    #: stored, and were handed back to the client - so everything looked wired,
    #: and nothing ever reached the browser. The measurable consequence is a
    #: session that declares `timezoneId="America/New_York"` and whose pages
    #: report the host's zone, which is the `timezone_mismatch` signal this
    #: project exists to avoid, produced by the automation rather than by the
    #: proxy.
    #:
    #: ⛔ AND THE TIMEZONE IS NOT A DUPLICATE OF THE PREF. It looks like one -
    #: the profile already carries a zone - but `juggler.timezone.override`
    #: goes through `JS::SetTimeZoneOverride`, which on Windows ICU silently
    #: falls back to the host zone for no-DST IANA names (America/Phoenix,
    #: Pacific/Honolulu). The per-realm path here works for every zone.
    ENGINE_OPTIONS = (
        ("locale", "Browser.setLocaleOverride", "locale"),
        # ⛔ KEPT, after being measured twice and nearly dropped once. Sending
        # it made every page creation time out, and the first reading was that
        # the command was the problem. IT WAS THE WRONG CULPRIT: driven by hand
        # the same command answers and the page announces its frame in 0.7 s.
        # What it actually did was shift the timing enough to lose a race the
        # server already had - see the event buffer below. Deleting it would
        # have "fixed" the symptom and left the race for the next unlucky
        # machine.
        ("timezoneId", "Browser.setTimezoneOverride", "timezoneId"),
        ("colorScheme", "Browser.setColorScheme", "colorScheme"),
        ("reducedMotion", "Browser.setReducedMotion", "reducedMotion"),
        ("forcedColors", "Browser.setForcedColors", "forcedColors"),
        ("userAgent", "Browser.setUserAgentOverride", "userAgent"),
    )

    def __init__(self, conn: Any, version: str, *, session_seed: Any = None,
                 motion_budget_s: Any = None,
                 profile_dir: Optional[str] = None) -> None:
        self.conn = conn
        self.version = version
        #: ⛔ THE SESSION'S SEED, and the reason it lives on the BROWSER rather
        #: than on a module: a process can hold two sessions with two seeds,
        #: and a module-level value would give the second one the first's hand.
        #:
        #: `None` means the caller turned humanising off, and every rhythm
        #: downstream reads that as "no rhythm" rather than as a default one.
        self.session_seed = session_seed
        self.motion_budget_s = motion_budget_s
        #: Every page of the session gets the next number, and its acts are
        #: numbered from it (`PageActs`): two tabs never draw the same click,
        #: field or typed string. The Playwright wrapper numbers its pages the
        #: same way; a counter per page restarted at 1 in every tab.
        self._page_numbers = itertools.count(1)
        #: The profile THIS object must remove on close: set only when the
        #: launch invented it. A caller's profile is never here.
        self._owned_profile = profile_dir
        self._closed = False
        self._sessions: Dict[str, str] = {}
        #: targetId -> the engine's `targetInfo` (its context, its opener).
        #: ⛔ KEPT FOR EVERY TARGET, not only for the pages this process asked
        #: for: a link with `target=_blank` or a `window.open()` makes a tab
        #: the driver never requested, and Puppeteer's contract is that it shows
        #: up in `browser.pages()` and as a `popup` event.
        self._targets: Dict[str, Dict] = {}
        self._sessions_ready = threading.Condition()
        # ⛔ THE EVENTS OF A SESSION START BEFORE ANYBODY IS LISTENING, and
        # that is not a corner case: measured on 2026-08-28 at the raw
        # protocol level, `Page.frameAttached` and the two
        # `Runtime.executionContextCreated` arrive at 0.65 s while the
        # `Browser.newPage` REPLY comes at 0.70 s. The reply is what says which
        # session a `Page` is built for, so every consumer that page wires -
        # the lifecycle, which learns the main frame only from `frameAttached`,
        # and the injected script, which learns the worlds only from
        # `executionContextCreated` - is registered after its own events have
        # already gone past.
        #
        # ⛔ IT USUALLY WORKED, WHICH IS WHY IT SHIPPED. On a quiet machine
        # the reply and the events interleave the other way often enough that
        # nothing is lost. What made it deterministic was an unrelated command
        # - `Browser.setTimezoneOverride` on the context - which shifts the
        # timing just enough to lose the race EVERY time. So the fix is not to
        # stop sending that command. It is to stop dropping events that
        # arrived before their reader existed.
        self._buffered: Dict[str, List] = {}
        self._buffer_lock = threading.Lock()
        #: Sessions that already have a reader. Buffering one of these would be
        #: a leak with no purpose, and replaying into it would deliver every
        #: event twice.
        self._live: set = set()
        self.contexts: List["Context"] = []
        # ⛔ Registered BEFORE `Browser.enable`: the enable is what makes the
        # engine start announcing targets, and the recorder must already be
        # listening when the first one arrives.
        conn.add_listener(self._route_browser_event)
        conn.send("Browser.enable", {"attachToDefaultContext": True},
                  timeout=30)

    # ── sessions ────────────────────────────────────────────────────────────
    def _route_browser_event(self, method: str, params: Dict, session) -> None:
        if method == "Browser.attachedToTarget":
            info = params.get("targetInfo") or {}
            with self._sessions_ready:
                self._sessions[info.get("targetId")] = params.get("sessionId")
                self._targets[info.get("targetId")] = dict(info)
                self._sessions_ready.notify_all()
        elif method == "Browser.detachedFromTarget":
            # ⛔ A tab the SITE closed (`window.close()`) ends here, and so does
            # one we closed. Either way it must leave `browser.pages()`.
            target_id = params.get("targetId")
            with self._sessions_ready:
                self._sessions.pop(target_id, None)
                self._targets.pop(target_id, None)
                self._sessions_ready.notify_all()
        if session:
            with self._buffer_lock:
                if session not in self._live:
                    held = self._buffered.setdefault(session, [])
                    if len(held) < self.BUFFER_CAP:
                        held.append((method, params))

    def replay(self, session: str, deliver) -> int:
        """Hand a new consumer the events of its session that it missed.

        ⛔ DELIVERED IN ORDER AND EXACTLY ONCE. The buffer is dropped as it is
        replayed, under the same lock that fills it, so an event arriving
        during the replay is either in the list or goes to the live path -
        never both, which would announce a frame twice, and never neither,
        which is the bug this exists for.
        """
        with self._buffer_lock:
            self._live.add(session)
            held = self._buffered.pop(session, None) or []
        for method, params in held:
            deliver(method, params, session)
        return len(held)

    def targets_in(self, context_id: Optional[str]) -> List[str]:
        """Every live page target of one context, in the order they appeared.

        ⛔ The DEFAULT context is the one whose `browserContextId` is absent:
        Juggler addresses it by absence, and a lookup for `None` must match
        both the missing field and an explicit null.
        """
        with self._sessions_ready:
            return [tid for tid, info in self._targets.items()
                    if (info.get("browserContextId") or None) == context_id
                    and info.get("type", "page") == "page"]

    def forget(self, session: str) -> None:
        """Stop holding events for a session nobody is going to read."""
        with self._buffer_lock:
            self._buffered.pop(session, None)
            self._live.discard(session)

    def session_for(self, target_id: str, timeout: float) -> str:
        """⛔ The session arrives as an EVENT, not in the reply to `newPage`.
        Polling the dict without waiting on the condition is a race that passes
        on a fast machine and fails on a loaded one."""
        deadline = time.monotonic() + timeout
        with self._sessions_ready:
            while target_id not in self._sessions:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise EngineError(
                        "no session was attached for target %s in %.0fs"
                        % (target_id, timeout))
                self._sessions_ready.wait(left)
            return self._sessions[target_id]

    # ── contexts ────────────────────────────────────────────────────────────
    def new_context(self, options: Optional[Dict] = None) -> "Context":
        """A fresh container: its own cookies and storage, gone with it."""
        options = dict(options or {})
        result = self.conn.send("Browser.createBrowserContext",
                                {"removeOnDetach": True}, timeout=30)
        context_id = result["browserContextId"]
        self.apply_context_options(context_id, options)
        context = Context(self, options, context_id)
        self.contexts.append(context)
        return context

    def default_context(self, options: Optional[Dict] = None) -> "Context":
        """The context whose state IS the profile's: userContextId 0.

        ⛔ UNTIL 0.12.0 THE PERSISTENT PATH HANDED BACK A NEW CONTEXT, AND A
        `profile_dir=` KEPT NOTHING. `Browser.createBrowserContext` makes a
        container, a fresh userContextId, and Juggler's `destroy()` calls
        `ContextualIdentityService.remove` on it, which deletes the container's
        cookies and storage together with the identity. Every cookie a session
        wrote lived in a container that died with the session. Measured
        2026-09-05 against the published 0.12.0.

        The persistent context is the DEFAULT one, addressed by omitting
        `browserContextId`. That is what this returns, with the same options
        applied the same way.
        """
        options = dict(options or {})
        self.apply_context_options(None, options)
        context = Context(self, options, None)
        self.contexts.append(context)
        return context

    def apply_context_options(self, context_id: Optional[str],
                              options: Dict) -> None:
        """Push the options the caller asked for into the engine.

        ⛔ BEFORE THE FIRST PAGE EXISTS, and that ordering is the whole point:
        these are defaults a new page INHERITS, so applying them after
        `newPage` would leave the first page - the only one most sessions ever
        open - without them.

        ⛔ AND `no-preference` IS SENT, NOT SKIPPED. It looks like an "unset"
        value and it is not: an adapter omits the field entirely when its
        caller said nothing, so the string only ever arrives because somebody
        asked for it by name.
        """
        for name, command, field in self.ENGINE_OPTIONS:
            value = options.get(name)
            if value in (None, ""):
                continue
            if name == "locale":
                # ⛔ THE ENGINE TAKES A LANGUAGE LIST, NOT A TAG. The context's
                # locale becomes the BrowsingContext's LanguageOverride, which
                # navigator.languages is split from, the realm's Intl locale is
                # taken from and the Accept-Language header is prepared from -
                # the same field the launch locale seeds with
                # `juggler.locale.override`, the full list invisible_core
                # writes to `intl.accept_languages`. Sent as the bare tag, a
                # context asking for "de-DE" would report navigator.languages
                # == ["de-DE"] where a German Firefox reports de-DE, de,
                # en-US, en; and the default context, which gets the launch
                # locale through these same options, would drop from the
                # profile's four entries to one. The list is the core's
                # decision for that tag, and nothing here derives it: the
                # default context carries the launch decision's primary
                # and gets back the list the profile declared.
                value = SessionLocale.of(value).accept_languages
            self.context_send(command,
                              {"browserContextId": context_id, field: value})
        # ⛔ The rest of the option set, each one a lever that was arriving
        # and going nowhere. They share one property: they are sent ONCE, as
        # context options, and never again - so a place that forgets one is a
        # feature that silently does not exist rather than one that fails.
        #
        # A context may carry its OWN proxy, overriding the browser-level one.
        # Same refusal as the launch path: a context whose proxy cannot be
        # expressed must not come back usable.
        if options.get("proxy"):
            try:
                proxy = parse_proxy(options["proxy"]).as_engine_command()
            except ValueError as exc:
                raise EngineError(
                    "the context proxy cannot be applied: %s" % exc)
            self.context_send("Browser.setContextProxy",
                              dict(proxy, browserContextId=context_id))
        headers = options.get("extraHTTPHeaders")
        if headers:
            self.context_send("Browser.setExtraHTTPHeaders",
                              {"browserContextId": context_id,
                               "headers": headers})
        if options.get("offline"):
            self.context_send("Browser.setOnlineOverride",
                              {"browserContextId": context_id,
                               "override": "offline"})
        geolocation = options.get("geolocation")
        if geolocation:
            self.context_send("Browser.setGeolocationOverride",
                              {"browserContextId": context_id,
                               "geolocation": geolocation})
        credentials = options.get("httpCredentials")
        if credentials:
            self.context_send("Browser.setHTTPCredentials",
                              {"browserContextId": context_id,
                               "credentials": credentials})
        if options.get("ignoreHTTPSErrors"):
            self.context_send("Browser.setIgnoreHTTPSErrors",
                              {"browserContextId": context_id,
                               "ignoreHTTPSErrors": True})
        if options.get("bypassCSP"):
            self.context_send("Browser.setBypassCSP",
                              {"browserContextId": context_id,
                               "bypassCSP": True})
        # ⛔ Inverted on purpose: the option says `javaScriptEnabled=False`,
        # Juggler says `javaScriptDisabled=True`. Passing one straight into the
        # other would turn scripting OFF for every default context, which is
        # the kind of inversion that looks like the site being broken.
        if options.get("javaScriptEnabled") is False:
            self.context_send("Browser.setJavaScriptDisabled",
                              {"browserContextId": context_id,
                               "javaScriptDisabled": True})
        if options.get("hasTouch"):
            self.context_send("Browser.setTouchOverride",
                              {"browserContextId": context_id,
                               "hasTouch": True})
        permissions = options.get("permissions")
        if permissions:
            # ⛔ `"*"` is the wildcard Juggler tests for by name
            # (`origin === '*' || page._url.startsWith(origin)` in
            # `TargetRegistry.js`). An empty string would ALSO match every url
            # through the `startsWith` half, which is exactly the kind of
            # accident that works until somebody tightens that condition.
            self.context_send("Browser.grantPermissions",
                              {"browserContextId": context_id, "origin": "*",
                               "permissions": permissions})
        viewport = options.get("viewport")
        if viewport:
            # ⛔ `screen` is a SEPARATE option from `viewport` and rides in
            # the same command. Without it the page reports a screen the size
            # of its own window, which no real desktop has ever done and which
            # this project spends a pref on getting right.
            wanted: Dict[str, Any] = {"viewportSize": {
                "width": viewport["width"], "height": viewport["height"]}}
            screen = options.get("screen")
            if screen:
                wanted["screenSize"] = {"width": screen["width"],
                                        "height": screen["height"]}
            if options.get("deviceScaleFactor"):
                wanted["deviceScaleFactor"] = options["deviceScaleFactor"]
            if options.get("isMobile"):
                wanted["isMobile"] = True
            self.context_send("Browser.setDefaultViewport",
                              {"browserContextId": context_id,
                               "viewport": wanted})

    def context_send(self, command: str, params: Dict,
                     timeout: float = 10) -> Any:
        """A `Browser.*` command about one context. The literal dicts above
        carry `browserContextId` with the id, or with None for the default
        context; `address` turns None into the absence Juggler reads."""
        return self.conn.send(command,
                              address(params.get("browserContextId"), params),
                              timeout=timeout)

    # ── the end ─────────────────────────────────────────────────────────────
    def close(self) -> None:
        """Close the browser, THEN take away a profile this session invented.

        ⛔ THE ORDER IS THE POINT. The browser holds a lock on its profile
        until it is gone, and removing the directory first fails on Windows -
        silently, because `remove_profile` must never raise. Until 2026-09-24
        the two were separate shutdown hooks in invisible_playwright's server,
        run in REVERSE registration order, and the removal was registered
        second: it ran first. One method doing both, in order, is the fix.

        Idempotent: an adapter may reach this from more than one way out.
        """
        if self._closed:
            return
        self._closed = True
        try:
            self.conn.close()
        except Exception:
            pass
        if self._owned_profile:
            remove_profile(self._owned_profile)


# ── context ─────────────────────────────────────────────────────────────────
class Context:
    """One browser context: the default one, or a Juggler container."""

    def __init__(self, browser: Browser, options: Dict,
                 context_id: Optional[str]) -> None:
        self.browser = browser
        self.options = options
        #: `None` is the DEFAULT context - userContextId 0, the one whose
        #: cookies and storage are the profile's.
        self.context_id = context_id
        self.pages: List["Page"] = []
        #: The caller's context-level init scripts, in order. The engine's
        #: command replaces the list, so the accumulation lives here.
        self._init_scripts: List[str] = []
        self.closed = False

    @property
    def conn(self) -> Any:
        return self.browser.conn

    def send(self, command: str, params: Dict, timeout: float = 30) -> Any:
        """A `Browser.*` command addressed to THIS context."""
        return self.conn.send(command, address(self.context_id, params),
                              timeout=timeout)

    def new_page(self) -> "Page":
        result = self.send("Browser.newPage", {})
        return self.adopt(result["targetId"])

    def adopt(self, target_id: str) -> "Page":
        """The `Page` for a target of this context, built once.

        Used for the pages this process opens and for the ones a site opens on
        its own, which exist in the engine before anybody here asks for them.
        """
        for page in self.pages:
            if page.target_id == target_id:
                return page
        session = self.browser.session_for(target_id, timeout=20.0)
        page = Page(self, session, target_id)
        self.pages.append(page)
        return page

    def live_targets(self) -> List[str]:
        """Every page target of this context the engine still has open."""
        return self.browser.targets_in(self.context_id)

    # ── init scripts ────────────────────────────────────────────────────────
    def add_init_script(self, source: str) -> None:
        """A script for every page of this context - present and future.

        ⛔ BOTH SIDES, and that is not belt and braces. `Browser.setInitScripts`
        covers pages this context opens LATER; the pages already open have
        their own list in the engine and do not re-read the context's, so they
        are told directly. A version that did only the first works in every
        test that adds the script before opening a page, which is most of them.
        """
        self._init_scripts.append(source)
        self._push_init_scripts()
        for page in list(self.pages):
            try:
                page.injected.add_init_script(source)
            except Exception:
                pass

    def remove_init_script(self, source: str) -> None:
        """Undo one context-level init script, on both sides it was added to."""
        if source in self._init_scripts:
            self._init_scripts.remove(source)
        self._push_init_scripts()
        for page in list(self.pages):
            try:
                page.injected.remove_init_script(source)
            except Exception:
                pass

    def _push_init_scripts(self) -> None:
        self.send("Browser.setInitScripts",
                  {"scripts": [{"script": s} for s in self._init_scripts]})

    # ── cookies ─────────────────────────────────────────────────────────────
    def cookies(self) -> List[Dict]:
        result = self.send("Browser.getCookies", {}) or {}
        return list(result.get("cookies") or [])

    def set_cookies(self, cookies: List[Dict]) -> None:
        """⛔ `expires` IS SECONDS AND -1 MEANS SESSION, not zero and not
        milliseconds. A translation added here "helpfully" would silently
        expire every session cookie in 1970."""
        if cookies:
            self.send("Browser.setCookies", {"cookies": list(cookies)})

    def clear_cookies(self) -> None:
        """⛔ Juggler clears the WHOLE context: it takes no filter. An adapter
        whose API can ask for a subset must refuse that form, or delete the
        rest back itself - never clear more than it was asked to."""
        self.send("Browser.clearCookies", {})

    # ── the end ─────────────────────────────────────────────────────────────
    def close(self) -> None:
        """Remove this context. ⛔ THE DEFAULT CONTEXT IS NEVER REMOVED: it
        closes the browser instead, which is what closing a persistent context
        means. `Browser.removeBrowserContext` on it would make Juggler's
        `destroy()` unregister the default from its maps and leave a browser
        that no command can address."""
        if self.closed:
            return
        self.closed = True
        for page in list(self.pages):
            page.detach()
        if self.context_id is None:
            self.browser.close()
            return
        try:
            self.conn.send("Browser.removeBrowserContext",
                           {"browserContextId": self.context_id}, timeout=10)
        except Exception:
            pass


# ── page ────────────────────────────────────────────────────────────────────
class Page:
    """One tab: its Juggler session, its frames, its injected script, its hands.

    ⛔ THE LIFECYCLE AND THE INJECTED SCRIPT ARE BUILT FIRST AND FED THE EVENTS
    THIS PAGE ALREADY MISSED. `Page.frameAttached` and the
    `Runtime.executionContextCreated` pair are sent by the browser BEFORE the
    `Browser.newPage` reply that told us which session this is, so without
    the replay the lifecycle waits twenty seconds for a main frame that was
    announced before it was born.
    """

    def __init__(self, context: Context, session: str, target_id: str) -> None:
        self.context = context
        self.session = session
        self.target_id = target_id
        conn = context.conn
        self.lifecycle = Lifecycle(conn, session)
        self.injected = InjectedScript(conn, session)
        self.injected.install()
        browser = context.browser
        # ⛔ engine_approach: this client has no cursor of its own, so the
        # engine side draws every pointer approach - without it every click
        # would arrive as one jump from wherever the pointer was.
        self.actions = Actions(conn, session, self.lifecycle, self.injected,
                               acts=PageActs(next(browser._page_numbers)),
                               session_seed=browser.session_seed,
                               motion_budget_s=browser.motion_budget_s,
                               engine_approach=True)
        self._listeners: List[Callable] = []
        self._detached = False
        self.replayed_events = browser.replay(session, conn.dispatch_event)
        self.main_frame_id = self.lifecycle.wait_for_main_frame(timeout=20.0)

    @property
    def browser(self) -> Browser:
        return self.context.browser

    @property
    def conn(self) -> Any:
        return self.context.conn

    # ⛔ The actions engine owns the keyboard: it holds the modifier state, and
    # a second keyboard would lose "Shift is down" between a `down` and the
    # next key. A page pressing a key does not need to know that.
    @property
    def keyboard(self):
        return self.actions.keyboard

    def send(self, method: str, params: Optional[Dict] = None,
             timeout: float = 30) -> Any:
        """A command on THIS page's session."""
        return self.conn.send(method, params or {}, session=self.session,
                              timeout=timeout)

    def on_event(self, fn: Callable[[str, Dict], None]) -> None:
        """Call `fn(method, params)` for every event of THIS page's session.

        ⛔ Registered on the connection's list and removed by `detach()`: a
        subscriber added without a matching removal is how a browser once
        reached 979 subscribers at page 325 and then stopped delivering events
        altogether.
        """
        def route(method: str, params: Dict, session) -> None:
            if session == self.session:
                fn(method, params)
        self._listeners.append(route)
        self.conn.add_listener(route)

    def detach(self) -> None:
        """Unsubscribe this page and everything it owns, ONCE."""
        if self._detached:
            return
        self._detached = True
        conn = self.conn
        for route in self._listeners:
            conn.remove_listener(route)
        self._listeners.clear()
        self.lifecycle.detach()
        self.injected.detach()
        if self in self.context.pages:
            self.context.pages.remove(self)

    def close(self) -> None:
        """Close THIS tab, and only this one.

        ⛔ `Page.close`, NOT `Browser.removeBrowserContext`. A caller who opens
        three pages in one context and closes one must not lose all three,
        plus its cookies and its storage.
        """
        try:
            self.send("Page.close", {"runBeforeUnload": False}, timeout=10)
        except Exception:
            pass
        # ⛔ The session leaves the browser's registry here, or a long-lived
        # browser accumulates one entry per page it ever opened.
        try:
            self.browser.forget(self.session)
        except Exception:
            pass
        self.detach()
