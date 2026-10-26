"""Test isolation.

`Config.load()` reads a `.env` from the working directory. Without this, a developer
who has actually configured etsyotomasyon gets different results from CI — their real
credentials leak into tests that were written assuming none exist. That is a test
suite that passes for the wrong reason, and it hides exactly the bugs these tests are
meant to catch.

So every test starts in an empty directory with no Etsy environment. Tests that need
repository files address them by absolute path.
"""

import pytest
from web_helpers import web  # noqa: F401 — the `web` fixture for tests/test_web_*.py

ETSY_VARS = (
    "ETSY_KEYSTRING",
    "ETSY_SHARED_SECRET",
    "ETSY_REDIRECT_URI",
    "ETSY_SHOP_ID",
    "ETSY_SCOPES",
    "ETSYOTOMASYON_RATE_PER_SEC",
    "ETSYOTOMASYON_HOME",
    "ETSYOTOMASYON_SHOP",
    "ETSYOTOMASYON_IGNORE_CWD_ENV",
    "PINTEREST_APP_ID",
    "PINTEREST_APP_SECRET",
    "PINTEREST_REDIRECT_URI",
    "PINTEREST_SANDBOX",
    "PINTEREST_ACCESS_TOKEN",
)


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch, tmp_path):
    """No inherited credentials, no inherited .env, no writing to a real token store.

    Each variable is set and then deleted, rather than only deleted, so monkeypatch
    records it and undoes whatever the test itself writes: `load_env()`, shop
    selection and the desktop settings all write os.environ directly, and a value
    left behind would leak into every later test.
    """
    for name in ETSY_VARS:
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    # No test asks GitHub for a newer release: a launch started by a test (or a
    # subprocess of one) would otherwise look after 10 s. Tests of the checker
    # delete this and give it a fake transport (tests/test_web_update.py).
    monkeypatch.setenv("ETSYOTOMASYON_NO_UPDATE_CHECK", "1")

    home = tmp_path / "etsyotomasyon-home"
    home.mkdir()
    monkeypatch.setenv("ETSYOTOMASYON_HOME", str(home))

    work = tmp_path / "cwd"
    work.mkdir()
    monkeypatch.chdir(work)
