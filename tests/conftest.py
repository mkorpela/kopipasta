"""Keep the test suite out of the developer's real home directory.

Found by inspecting the actual `~/.cache/kopipasta` after a run: the suite had
written a dozen stray project directories there, and `test_main.py` had
overwritten the real saved task with its own fixture string ("Refactor
logic"). Before the cache was keyed per project this was worse — the suite
clobbered the single global file that the developer's own sessions relied on.

`Path.home()` reads the environment on every call, so setting these here
isolates both in-process code and any subprocess the tests spawn.
"""

import pytest


@pytest.fixture(autouse=True)
def isolate_home(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("home")
    for var in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(var, str(home))
    # HOMEDRIVE/HOMEPATH are the Windows fallback when USERPROFILE is absent.
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)
    return home
