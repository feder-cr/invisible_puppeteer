"""Every route from a chosen path to a spawned Firefox goes through here.

``binary_path=`` (and ``INVPW_BINARY_PATH``, which the test suite and the e2e
scripts turn into ``binary_path=``) never reaches ``ensure_binary()``, so none
of the download-side checks run on that route. The guard therefore sits on the
resolved executable, not inside the fetcher.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

from invisible_core import ensure_binary
from invisible_core.seal import EngineMismatch, active_seal, verify_engine


def resolve_executable(binary_path: Optional[Union[str, Path]]) -> Path:
    """``binary_path=`` skips the download path entirely, so the guard sits on the
    resolved executable rather than inside the fetcher.

    ⛔ This used to say "binary_path= and INVPW_BINARY_PATH", and the second half
    was false: nothing here reads that variable. It is translated into
    ``binary_path=`` by the test scripts and by ``run_e2e.py``, so it is a
    convention of this repo's harness and not a feature of the library. Corrected
    2026-08-14 - a docstring that promises an env var the code never reads sends
    the reader looking for a bug in the wrong function.

    Driving an engine the packaged seal does not describe is done with
    ``binary_path=`` plus ``INVISIBLE_SEAL_FILE`` pointing at a seal generated for
    it; without that, ``verify_engine`` refuses, and refusing is correct - the
    prefs and the spoofed User-Agent describe the sealed build.
    """
    seal = active_seal()
    if binary_path:
        return verify_engine(Path(binary_path), seal, source=f"binary_path={binary_path}")
    return ensure_binary(seal=seal)


def assert_wire_version(browser) -> None:
    """Compare what the engine reports over the protocol with the seal.

    browser.version is a cached property from the connection initializer
    (Juggler Browser.getInfo -> MOZ_APP_VERSION_DISPLAY), so this costs zero
    round trips and no pref can spoof it. It is the only check that also
    catches a hand-edited application.ini. Not available on the persistent
    context path, where Playwright exposes no Browser.
    """
    if browser is None:
        return
    seal = active_seal()
    raw = getattr(browser, "version", "")
    # Playwright types Browser.version as str. Anything else (a stub, a mock,
    # a driver that did not populate the initializer) carries no evidence
    # either way, and inventing a mismatch out of it would be a false alarm.
    if not isinstance(raw, str):
        return
    reported = raw.split("/")[-1].strip()
    if reported and reported != seal.upstream_version:
        raise EngineMismatch(
            "engine/seal mismatch after launch - the running browser is not the sealed build\n"
            f"  protocol says: Firefox {reported}\n"
            f"  seal says    : Firefox {seal.upstream_version} (tag {seal.tag}, "
            f"build {seal.build_id})\n"
            "  why          : application.ini can be edited; this value comes from the "
            "running engine itself.\n"
            "  fix          : python -m invisible_puppeteer fetch")
