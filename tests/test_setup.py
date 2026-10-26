"""The setup checklist.

The point of these is the distinction between "verified absent" and "cannot verify".
A checklist that quietly assumes is worse than one that asks, so the three states are
pinned here.
"""

import httpx
import pytest
from typer.testing import CliRunner

from etsyotomasyon import setup as setup_mod
from etsyotomasyon.cli import app

runner = CliRunner()


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """No credentials, no token, no workspace — a stranger's first run."""
    monkeypatch.setenv("ETSYOTOMASYON_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    for name in ("ETSY_KEYSTRING", "ETSY_SHARED_SECRET", "ETSY_REDIRECT_URI", "ETSY_SHOP_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        setup_mod.workspace_mod, "default_root", lambda: tmp_path / "Etsy Studio"
    )
    return tmp_path


@pytest.fixture
def configured(clean_env, monkeypatch):
    monkeypatch.setenv("ETSY_KEYSTRING", "KEY123")
    monkeypatch.setenv("ETSY_SHARED_SECRET", "SECRET")
    monkeypatch.setenv("ETSY_REDIRECT_URI", "http://localhost:3003/oauth/redirect")
    return clean_env


# --- the three states -----------------------------------------------------------


def test_python_and_install_always_pass_when_running():
    assert setup_mod.check_python().state == setup_mod.OK
    assert setup_mod.check_installed().state == setup_mod.OK


def test_a_missing_credential_is_verified_absent_not_unknown(clean_env):
    result = setup_mod.check_credentials()
    assert result.state == setup_mod.MISSING
    assert any("etsyotomasyon init" in line for line in result.fix)


def test_configured_credentials_are_reported_without_the_secret(configured):
    result = setup_mod.check_credentials()
    assert result.state == setup_mod.OK
    assert "SECRET" not in result.detail, "the shared secret must never be printed"
    assert "6 chars" in result.detail


def test_a_shop_cannot_be_verified_without_a_token(clean_env):
    # No code can see whether someone has an Etsy shop. Saying so beats assuming.
    assert setup_mod.check_shop().state == setup_mod.UNKNOWN


def test_a_registered_callback_cannot_be_verified_either(clean_env):
    assert setup_mod.check_callback_registered().state == setup_mod.UNKNOWN


def test_an_ip_callback_is_reported_missing(configured, monkeypatch):
    monkeypatch.setenv("ETSY_REDIRECT_URI", "http://127.0.0.1:3003/cb")
    result = setup_mod.check_redirect()
    assert result.state == setup_mod.MISSING
    assert "IP addresses" in result.detail


def test_a_localhost_callback_is_accepted(configured):
    assert setup_mod.check_redirect().state == setup_mod.OK


def test_no_token_means_the_shop_is_not_connected(configured):
    result = setup_mod.check_connected()
    assert result.state == setup_mod.MISSING
    assert any("auth login" in line for line in result.fix)


# --- the workspace step ---------------------------------------------------------


def test_a_missing_workspace_points_at_drop_init(clean_env):
    result = setup_mod.check_workspace()
    assert result.state == setup_mod.MISSING
    assert any("drop init" in line for line in result.fix)


def test_a_workspace_without_a_template_says_to_build_a_listing(clean_env):
    from etsyotomasyon.drop.workspace import Workspace

    Workspace(clean_env / "Etsy Studio").create()
    result = setup_mod.check_workspace()
    assert result.state == setup_mod.MISSING
    assert any("by hand" in line for line in result.fix)


def test_the_workspace_step_never_blocks(clean_env):
    workspace_steps = [s for s in setup_mod.build_steps() if "workspace" in s.title.lower()]
    assert workspace_steps and not workspace_steps[0].required


# --- the next-command hint ------------------------------------------------------


def test_next_command_prefers_a_required_step():
    steps = setup_mod.build_steps()
    results = [
        (steps[4], setup_mod.StepResult(setup_mod.MISSING, "", ["Run: etsyotomasyon init"])),
        (steps[9], setup_mod.StepResult(setup_mod.MISSING, "", ["Run: etsyotomasyon drop init"])),
    ]
    assert setup_mod.next_command(results) == "etsyotomasyon init"


def test_next_command_falls_through_to_an_optional_step():
    steps = setup_mod.build_steps()
    results = [
        (steps[0], setup_mod.StepResult(setup_mod.OK)),
        (steps[9], setup_mod.StepResult(setup_mod.MISSING, "", ["Run: etsyotomasyon drop init"])),
    ]
    assert setup_mod.next_command(results) == "etsyotomasyon drop init"


def test_next_command_is_empty_when_nothing_is_missing():
    steps = setup_mod.build_steps()
    assert setup_mod.next_command([(steps[0], setup_mod.StepResult(setup_mod.OK))]) == ""


# --- the command itself ---------------------------------------------------------


def test_doctor_never_asks_and_exits_nonzero_when_blocked(clean_env):
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "Keystring AND shared secret" in result.output
    assert "Next:" in result.output


def test_doctor_reports_unverifiable_steps_without_assuming(clean_env):
    result = runner.invoke(app, ["doctor"])
    assert "only you can confirm this" in result.output


def test_setup_asks_and_a_no_produces_instructions(clean_env):
    result = runner.invoke(app, ["setup"], input="n\n")
    assert "https://www.etsy.com/sell" in result.output


def test_saying_no_to_a_shop_stops_the_rest(clean_env):
    # Telling someone to create an API app is noise when they have no shop yet.
    result = runner.invoke(app, ["setup"], input="n\n")
    assert "An Etsy API app" in result.output  # listed
    assert "your-apps" not in result.output  # but not yet instructed


def test_no_runnable_command_beats_the_wrong_one(clean_env):
    # "Open an Etsy shop" has no command. Falling through to the optional workspace
    # step would print "Next: etsyotomasyon drop init", which is actively misleading.
    result = runner.invoke(app, ["setup"], input="n\n")
    assert "Next: etsyotomasyon drop init" not in result.output


def test_setup_keeps_checking_after_an_unverifiable_answer(clean_env):
    # A question we cannot verify must not hide the checks that follow it.
    result = runner.invoke(app, ["setup"], input="y\ny\ny\n")
    assert "Keystring AND shared secret" in result.output


def test_a_fully_configured_setup_reports_ready(configured, monkeypatch):
    from etsyotomasyon import auth as auth_mod
    from etsyotomasyon.auth import Token

    token = Token(access_token="1.abc", refresh_token="r", expires_at=9e12,
                  scopes=("shops_r", "listings_r", "listings_w",
                          "transactions_r", "transactions_w"))
    monkeypatch.setattr(auth_mod, "load_token", lambda: token)
    monkeypatch.setattr(setup_mod.auth, "load_token", lambda: token)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"application": "ok"})

    real_init = setup_mod.EtsyClient.__init__

    def patched(self, config, **kw):
        real_init(self, config, **kw)
        self._http = httpx.Client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(setup_mod.EtsyClient, "__init__", patched)
    from etsyotomasyon.drop.workspace import Workspace

    ws = Workspace(configured / "Etsy Studio").create()
    ws.write_template({"source_listing_id": 1, "fields": {}})
    from PIL import Image

    Image.new("RGB", (100, 100)).save(ws.mockups / "m.jpg", "JPEG")

    result = runner.invoke(app, ["setup"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "You are ready" in result.output
