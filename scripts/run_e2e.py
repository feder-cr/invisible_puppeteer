#!/usr/bin/env python3
"""Run the FULL e2e suite (every test that opens the browser) against a binary.

Every ``@pytest.mark.e2e`` test is excluded from the default `pytest` run
(`addopts = -m 'not slow and not e2e'`) because they need a real Firefox binary
and a display, and they skip themselves when no binary is available. That makes
them easy to forget - and "we can't afford for something to not work". This is
the gate that runs them all, deliberately, against a chosen binary.

It is the MANDATORY pre-release e2e gate: run it green against the freshly-built
release binary BEFORE un-drafting a firefox-N (alongside the fppro + WebRTC
realness gates). It is NOT in the public CI drive-gate - the hosted runners are
content-process unstable under a heavy headless interaction sequence (see
70-known-bugs / 60-ci-release-pipeline); this runs locally on reliable hardware.

Flake-resilience: under full-suite load a couple of interaction tests (dblclick,
hover/mouseenter) can flake even though they pass 3/3 in isolation, so failures
are reran up to twice on the known transient signatures. A genuinely broken
binary fails all attempts. The webrtc e2e fake a TCP-only SOCKS locally (no
proxy/secrets), so the whole suite is offline.

The run opens FOUR browsers at once by default. `--dist loadfile` is not
negotiable and the reason is on the line that sets it: two properties of this
suite depend on a file staying whole on one worker, and both were measured
failing when it did not.

Usage:
    python scripts/run_e2e.py <firefox-binary>
    python scripts/run_e2e.py            # uses $INVPW_BINARY_PATH
    python scripts/run_e2e.py <binary> -n 8    # eight browsers at once
    python scripts/run_e2e.py <binary> -n 1    # the old serial path, unchanged
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_RERUN_SIGNATURES = "Timeout|context was destroyed|was detached|not visible|because of a navigation|TargetClosed"

#: Browsers opened at once. Four was asked for and four is what the default is;
#: it is not a measured optimum and there is no reason to believe one number
#: fits every machine, so `-n` moves it and `-n 1` puts the run back exactly on
#: the serial path this script had before parallelism existed.
_DEFAULT_PROCS = "4"


def _take_procs(argv: list[str]) -> tuple[str, list[str]]:
    """Pull `-n/--procs` out of the arguments, leaving the rest for pytest.

    Hand-parsed rather than argparse because everything this script does not
    recognise is forwarded to pytest verbatim, and argparse would have to be
    taught every pytest flag in order to not eat one.

    The value is kept as a STRING and handed to xdist unread, so `auto` and any
    future spelling xdist accepts keep working without this function learning
    about them. Only `0` and `1` are interpreted here, and they mean the same
    thing: do not load xdist at all. That is not the same as `-n 1`, which
    still runs the tests inside a worker subprocess - a difference that matters
    the day this script is used to compare a parallel run against a serial one,
    because then the serial arm must be the ORIGINAL path and not a
    one-worker variant of the new one.
    """
    procs, rest, i = _DEFAULT_PROCS, [], 0
    while i < len(argv):
        if argv[i] in ("-n", "--procs"):
            if i + 1 >= len(argv):
                print("run_e2e.py: -n needs a value (1 = serial)", file=sys.stderr)
                raise SystemExit(2)
            procs, i = argv[i + 1], i + 2
            continue
        rest.append(argv[i])
        i += 1
    return procs, rest


def main() -> int:
    procs, extra = _take_procs(sys.argv[1:])
    binary = extra[0] if extra else os.environ.get("INVPW_BINARY_PATH")
    extra = extra[1:] if extra else []
    if not binary:
        print("usage: run_e2e.py <firefox-binary>  (or set INVPW_BINARY_PATH)", file=sys.stderr)
        return 2
    if not Path(binary).exists():
        print(f"ERROR: binary not found: {binary}", file=sys.stderr)
        return 2

    # An escape hatch in the environment is not neutral here: one e2e ASSERTS
    # the behaviour it disables.
    #
    # `INVISIBLE_CORE_PIN=allow` tells the core to run a mismatched pair instead
    # of repairing it. It is the normal way to drive a locally built engine, so
    # it is often already exported in the shell that starts this run - and it is
    # INHERITED by the venv subprocess that
    # `test_the_mismatch_repairs_itself_at_import_without_a_restart` spawns.
    # That test deliberately breaks an environment and requires importing the
    # wrapper to fix it. With the variable set, the repair correctly declines,
    # the version does not move, and the test fails for a reason that has
    # nothing to do with the binary under test.
    #
    # Measured 2026-08-16: 1 failed / 143 passed in 14 minutes, and the same
    # file re-run with the variable unset gave 4 passed in 2 minutes. Refusing
    # up front costs a second; discovering it costs the whole run plus the time
    # spent believing the product was broken.
    #
    # It is stripped, not merely warned about: a warning at the top of a
    # 14-minute run scrolls away long before the failure appears.
    env = {k: v for k, v in os.environ.items() if k != "INVISIBLE_CORE_PIN"}
    if "INVISIBLE_CORE_PIN" in os.environ:
        print("[run_e2e] INVISIBLE_CORE_PIN is set in this shell and has been "
              "STRIPPED for the run: one e2e asserts the pin repair that the "
              "variable disables, and it would fail for that reason alone.",
              file=sys.stderr)

    # One setting drives the whole suite: conftest's firefox_binary fixture and
    # the webrtc e2e both resolve from these.
    env["INVPW_BINARY_PATH"] = binary
    env["STEALTHFOX_E2E_BINARY"] = binary

    repo = Path(__file__).resolve().parent.parent

    # ⛔ `--dist loadfile` IS NOT A TUNING CHOICE, IT IS A CORRECTNESS ONE, and
    # xdist's default (`--dist load`, which hands out one test at a time) breaks
    # this suite in two measured ways.
    #
    # 1. MODULE-SCOPED BROWSERS. `test_fingerprint_consistency.py` is 65 of the
    #    193 e2e tests and opens ONE browser for all of them, from a
    #    module-scoped fixture. Scattered test-by-test across four workers, each
    #    worker that receives any of those tests builds its own copy of that
    #    fixture: four browsers where the file wants one, and the identity those
    #    65 tests exist to prove is no longer being read off a single session.
    #
    # 2. TESTS THAT DEPEND ON AN EARLIER TEST IN THEIR OWN FILE.
    #    `test_binary_executes_after_fetch` fails 2 out of 2 in isolation and
    #    passes 9 out of 9 when its module runs whole, because a prior test in
    #    that file populates the venv it uses. Measured 2026-08-29, and it very
    #    nearly got recorded as a deterministic failure of the product.
    #
    # `loadfile` keeps every file whole on one worker, so both properties
    # survive. The price is the ceiling: the longest single FILE is now the
    # floor of the whole run, and with 65 of 193 tests in one file this run
    # cannot go faster than that file no matter how many workers are asked for.
    # That is why raising `-n` past a certain point buys nothing here, and the
    # fix for it would be splitting that file, not adding workers.
    parallel = [] if procs in ("0", "1") else ["-n", procs, "--dist", "loadfile"]

    cmd = [
        sys.executable, "-m", "pytest",
        "-m", "e2e",
        "-o", "addopts=",            # override the default 'not e2e' deselection
        "--reruns", "2", "--reruns-delay", "1",
        "--only-rerun", _RERUN_SIGNATURES,
        # A DEADLINE is not a flake, and retrying one multiplies it by three.
        # `Timeout` in the signatures above was written for Playwright's
        # TimeoutError, but it also matches `subprocess.TimeoutExpired` -
        # measured, 2 tests produced 4 reruns - and the release/upgrade e2e
        # spend their time in `pip install` calls whose timeouts run to 300s
        # each. Retried twice that is 900s for one test, and the two files sum
        # to 10050s of timeout budget against a 2400s job. That is how the job
        # was killed at 40 minutes twice with nothing to show for it.
        "--rerun-except", "TimeoutExpired",   # a subprocess deadline
        "--rerun-except", "Timeout >",        # pytest-timeout's own deadline
        # And a per-test deadline, so a hang FAILS WITH A NAME instead of eating
        # the job. 420s is 4.5x the slowest legitimate test measured on CI
        # (test_webgl_readpixels_no_masking_signature, 94.4s; the second slowest
        # is 11.4s and the whole suite is 318s), and 1/6 of the job budget.
        # METHOD=thread, not the signal default, and that is the whole point.
        # Measured 2026-08-13 on the run of this very change: the suite wedged in
        # `test_hover_triggers_mouseenter` and the 420s signal deadline came and
        # went with NOTHING at 19.3 minutes, 21 minutes, 40. SIGALRM is delivered
        # to the main thread and Python runs the handler at the next bytecode
        # boundary; Playwright's sync API is blocked in a greenlet waiting on the
        # driver socket, so that boundary never arrives. A watchdog THREAD does
        # not need the hung thread to cooperate: it dumps every thread's stack
        # and ends the process. The suite does not carry on, which is the price,
        # and in exchange a hang stops being anonymous.
        "--timeout", "420",
        "--timeout-method", "thread",
        "-p", "no:cacheprovider",
        # -v, not -q, and the reason is a hang we could not name twice.
        # Under -q pytest emits one character per test and the line only
        # reaches the log when it is full, so a run that dies mid-line says
        # nothing about where it died. Measured 2026-08-12 on two runs of the
        # SAME commit: both printed the identical `.s....[ 50%]` line at 78
        # seconds, one then finished in 4:47 and the other was killed by the
        # 40-minute timeout with no second line - so the hang was somewhere in
        # tests 73-141 and that is as close as the log could get. Same symptom
        # on 2026-08-04. PYTHONUNBUFFERED was already set and is not the
        # missing piece: the output does stream, the GRANULARITY was wrong.
        # One line per test costs 141 lines and names the last one that ran.
        "-v", "--tb=short",
    ] + parallel + extra
    print(f"[run_e2e] binary={binary}")
    print(f"[run_e2e] browsers at once: {procs if parallel else '1 (serial)'}")
    print(f"[run_e2e] {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=repo, env=env).returncode


if __name__ == "__main__":
    sys.exit(main())
