import os
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def firefox_binary():
    """The patched Firefox for the e2e tests, or a clean skip.

    Same lookup as invisible_playwright's conftest: ``INVPW_BINARY_PATH`` if it
    is set, otherwise the cached binary of the sealed engine; never a download
    inside a test run.
    """
    env_path = os.environ.get("INVPW_BINARY_PATH")
    if env_path:
        if Path(env_path).exists():
            return env_path
        pytest.skip(f"INVPW_BINARY_PATH={env_path!r} does not exist")
    from invisible_core.constants import BINARY_ENTRY_REL
    from invisible_core.download import cache_dir_for_version
    if sys.platform not in BINARY_ENTRY_REL:
        pytest.skip(f"unsupported platform: {sys.platform}")
    entry = cache_dir_for_version() / BINARY_ENTRY_REL[sys.platform]
    if not entry.exists():
        pytest.skip("patched Firefox binary not cached and INVPW_BINARY_PATH "
                    "unset; set INVPW_BINARY_PATH=<firefox binary> or run "
                    "`invisible-puppeteer fetch`")
    return str(entry)
