"""Writing the profile a Firefox launch is given, and reading back what it is.

⛔ Leaves, like the ones in `_marshal`: none of them mentions a dispatcher.
What they have in common is a different subject - a directory on disk and the
`user.js` inside it - which is why they are not in the same module. One
`_helpers.py` holding both would be the bucket every project grows and nobody
can name.

Re-exported from `server.py`: `tests/gates/prefs_byte_parity.py` and the
transport tests import `_write_user_js`, `_host_of` and `_domain_matches` from
there.
"""
from __future__ import annotations

import json
import pathlib
import shutil
from typing import Any, Dict, Optional


def _remove_profile(directory: str) -> None:
    """Take away a profile WE created. Never one the caller named.

    ⛔ IT MUST NOT RAISE. This runs while the session is already going away,
    and on Windows a file can still be held for a moment after the process that
    owned it exits. A profile left behind is a few dozen megabytes; an
    exception here would be a shutdown that fails for a reason nobody cares
    about, on a path the caller has already stopped watching.
    """
    shutil.rmtree(directory, ignore_errors=True)


def _write_user_js(profile_dir: str, prefs: Dict) -> int:
    """The prefs, into the profile, BEFORE the browser starts.

    ⛔ WITHOUT THIS THE PYTHON PATH LAUNCHES A BROWSER THAT IS NOT THE PRODUCT,
    and it does so silently. Two hundred prefs arrive in `firefoxUserPrefs` on
    every `launch`, and the first draft of this server threw them away and
    started Firefox on an empty temporary profile. Everything worked - pages
    loaded, clicks landed, the tests were green - and every stealth declaration
    the package exists to make was simply absent.

    It was caught by a closed shadow root, of all things: the engine patch that
    lets a locator reach inside one is gated on `StealthEngineActive()`, which
    is a pref. The same probe found the root through `_juggler` directly and
    lost it through the public API, and the only difference between those two
    arms was who wrote the profile. A gate for a completely different feature
    is what made the absence visible; nothing was checking for it directly.

    ⛔ AND THEY ARE WRITTEN, NOT SENT. Our fork already removed prefs from the
    protocol - `Browser.enable` does not accept them any more - because a
    browser configured on the second launch is a browser that was wrong on the
    first. This mirrors what the driver's `defaultArgs` does, in Python.

    ⛔ Gecko HAS NO FLOAT PREF TYPE: a fraction must be written as a STRING, or
    the parser fails on that line and IGNORES EVERY LINE AFTER IT. That is not
    a hypothetical - `ui.textScaleFactor` written as a number once killed the
    browser on the second context, and the failure looked nothing like a prefs
    problem.
    """
    import os
    lines = []
    # ⛔ INSERTION ORDER, NOT SORTED. The driver uses `Object.keys()`, and the
    # two writers have to agree BYTE FOR BYTE - sorting produces a file that is
    # equally correct and not identical. Caught by
    # `tests/gates/prefs_byte_parity.py` on its very first real run, which is
    # the whole argument for keeping a judge around.
    for name in prefs:
        value = prefs[name]
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, int):
            rendered = str(value)
        elif isinstance(value, float):
            # ⛔ A string, deliberately. See the docstring.
            rendered = json.dumps(str(value))
        else:
            rendered = json.dumps(str(value))
        lines.append('user_pref(%s, %s);' % (json.dumps(name), rendered))
    os.makedirs(profile_dir, exist_ok=True)
    # ⛔ NO HEADER COMMENT, and no sorting either. The driver writes neither,
    # and this file has to match the driver's BYTE FOR BYTE - that is the
    # criterion item 1 was written with. A header is one line of difference in
    # a comparison whose whole value is that it finds differences.
    body = "\n".join(lines) + "\n"
    # ⛔ `write_bytes`: on Windows a text-mode write translates every newline,
    # and a prefs file is read by the browser, not by git.
    with open(os.path.join(profile_dir, "user.js"), "wb") as handle:
        handle.write(body.encode("utf-8"))
    return len(lines)


def _read_version(executable: str) -> str:
    """The base version, from `application.ini` next to the binary.

    ⛔ Read, never assumed: this project has three separate incidents where a
    folder name or a guess about the version sent a whole evening of
    measurements against the wrong build.
    """
    ini = pathlib.Path(executable).parent / "application.ini"
    try:
        for line in ini.read_text(encoding="utf-8",
                                  errors="replace").splitlines():
            if line.startswith("Version="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return "0.0"


def _only_set(params: Dict) -> Dict:
    """The parameters that were actually given, with the absent ones REMOVED.

    ⛔ IN A CLOSED-WORLD SCHEMA, `null` AND ABSENT ARE DIFFERENT ANSWERS.
    Juggler validates every field it is handed: an Optional that arrives as
    `null` is not "not provided", it is a value of the wrong type, and the
    command is REJECTED at runtime with `Object "<root>.clip" is undefined,
    but has some scheme`. Measured on 2026-08-28 on `Page.screenshot`, whose
    clip and quality are both optional and were both being sent as null.

    ⛔ AND THIS IS NOT DONE INSIDE `Connection.send`, tempting as that is:
    some commands mean something BY sending null. `Browser.setGeolocationOverride`
    with `geolocation: null` CLEARS the override, and stripping it there would
    turn "stop pretending to be somewhere" into "do nothing".
    """
    return {k: v for k, v in params.items() if v is not None}


def _host_of(url: str) -> str:
    """The host of a url, without importing a parser for three characters."""
    without_scheme = url.split("://", 1)[-1]
    return without_scheme.split("/", 1)[0].split(":", 1)[0].lower()


def _domain_matches(cookie_domain: str, host: str) -> bool:
    """⛔ A LEADING DOT MEANS "AND EVERY SUBDOMAIN", and dropping it turns a
    site-wide cookie into one that matches nothing. Comparing the two strings
    directly is the version that looks right and returns an empty list."""
    domain = (cookie_domain or "").lstrip(".").lower()
    if not domain or not host:
        return False
    return host == domain or host.endswith("." + domain)
