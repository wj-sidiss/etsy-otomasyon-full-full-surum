"""Several shops on one computer: each is a home of its own, and nothing crosses over.

Also the two pieces the desktop app's connect flow relies on: a working-directory
.env that can be ignored, and an Etsy sign-in that can be cancelled.
"""

from __future__ import annotations

import os
import socket
import threading
import time

import pytest
from typer.testing import CliRunner

from etsyotomasyon import auth, cli, shops
from etsyotomasyon.config import Config, base_home, home_dir, load_env
from etsyotomasyon.errors import AuthError, ConfigError

runner = CliRunner()


def test_the_first_shop_is_the_base_home_so_single_shop_installs_do_not_move():
    assert shops.current().id == ""
    assert home_dir() == base_home()
    assert [s.id for s in shops.all_shops()] == [""]


def test_each_further_shop_gets_its_own_home():
    second = shops.add()
    third = shops.add()
    assert (second.id, third.id) == ("shop-2", "shop-3")
    shops.select("shop-3")
    assert home_dir() == base_home() / "shops" / "shop-3"
    assert shops.current() == third
    shops.select("")
    assert home_dir() == base_home()


def test_shops_are_listed_in_natural_order():
    for name in ("shop-10", "shop-9", "shop-2"):
        (base_home() / "shops" / name).mkdir(parents=True)
    (base_home() / "shops" / "Not A Shop").mkdir()
    assert [s.id for s in shops.all_shops()] == ["", "shop-2", "shop-9", "shop-10"]


@pytest.mark.parametrize("bad", ["", "../escape", "Shop", "a/b", "x" * 41, "-lead"])
def test_a_shop_id_cannot_escape_or_be_ambiguous(bad):
    with pytest.raises(ConfigError):
        shops.validate_id(bad)


def test_selecting_a_shop_that_does_not_exist_fails_loudly():
    with pytest.raises(ConfigError, match="no shop"):
        shops.select("shop-7")


def test_a_shop_remembers_its_etsy_name_and_whether_it_is_connected():
    shop = shops.add()
    shops.select(shop.id)
    assert (shop.name, shop.connected) == ("", False)
    shops.remember("Demo Shop", 42)
    (shop.home / "token.json").write_text("{}", encoding="utf-8")
    assert (shop.name, shop.connected) == ("Demo Shop", True)
    assert shops.Shop("").name == ""  # the first shop is untouched


def test_removing_a_shop_deletes_only_its_home():
    keep, drop = shops.add(), shops.add()
    shops.remove(drop.id)
    assert keep.home.is_dir() and not drop.home.exists()
    assert base_home().is_dir()
    with pytest.raises(ConfigError):
        shops.remove(drop.id)


def test_keys_do_not_leak_between_shops(monkeypatch):
    (base_home() / ".env").write_text("ETSY_KEYSTRING=first\nETSY_SHARED_SECRET=s1\n", encoding="utf-8")
    second = shops.add()
    (second.home / ".env").write_text("ETSY_KEYSTRING=second\nETSY_SHARED_SECRET=s2\n", encoding="utf-8")

    shops.select(second.id)
    assert Config.load().keystring == "second"
    monkeypatch.delenv("ETSY_KEYSTRING")
    monkeypatch.delenv("ETSY_SHARED_SECRET")
    shops.select("")
    assert Config.load().keystring == "first"


def test_the_desktop_app_can_ignore_a_stray_env_in_the_working_directory(monkeypatch):
    with open(".env", "w", encoding="utf-8") as handle:
        handle.write("ETSY_KEYSTRING=from-the-cwd\n")
    monkeypatch.setenv("ETSYOTOMASYON_IGNORE_CWD_ENV", "1")
    load_env()
    assert "ETSY_KEYSTRING" not in os.environ
    monkeypatch.delenv("ETSYOTOMASYON_IGNORE_CWD_ENV")
    load_env()
    assert os.environ["ETSY_KEYSTRING"] == "from-the-cwd"


# --- the CLI --------------------------------------------------------------------


def test_shops_can_be_added_listed_used_and_removed_from_the_cli():
    added = runner.invoke(cli.app, ["shops", "add"])
    assert added.exit_code == 0, added.output
    assert "shop-2" in added.output

    listed = runner.invoke(cli.app, ["--shop", "shop-2", "shops", "list"])
    assert listed.exit_code == 0, listed.output
    assert "shop-2" in listed.output and "(default)" in listed.output

    removed = runner.invoke(cli.app, ["shops", "remove", "shop-2", "--yes"])
    assert removed.exit_code == 0, removed.output
    assert not (base_home() / "shops" / "shop-2").exists()


def test_an_unknown_shop_is_an_error_not_a_silent_default():
    result = runner.invoke(cli.app, ["--shop", "nope", "shops", "list"])
    assert result.exit_code != 0
    assert isinstance(result.exception, ConfigError)


def test_removing_a_shop_asks_first():
    shops.add()
    result = runner.invoke(cli.app, ["shops", "remove", "shop-2"], input="n\n")
    assert result.exit_code == 1
    assert (base_home() / "shops" / "shop-2").is_dir()


# --- a sign-in that can be cancelled ------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_waiting_for_the_browser_can_be_cancelled():
    port = _free_port()
    config = Config(keystring="k", shared_secret="s", redirect_uri=f"http://localhost:{port}/oauth/redirect")
    request = auth.build_authorization_url(config)
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    started = time.time()
    with pytest.raises(AuthError, match="Cancelled"):
        auth._capture_via_listener(request, config, port, timeout=60, cancel=cancel)
    assert time.time() - started < 5
    assert auth.port_is_free(port)  # the listener was closed


def test_the_listener_opens_without_a_host_name_lookup(monkeypatch):
    # A reverse DNS lookup here stalled Cancel for ~30s on a Mac.
    monkeypatch.setattr(socket, "getfqdn", lambda *a: pytest.fail("host name looked up"))
    server = auth.LoopbackServer(("127.0.0.1", _free_port()), auth._CallbackHandler)
    server.server_close()


def test_login_passes_the_cancel_through(monkeypatch):
    seen = {}

    def fake_capture(request, config, port, timeout, cancel=None):
        seen["cancel"] = cancel
        raise AuthError("Cancelled before Etsy sent the browser back.")

    monkeypatch.setattr(auth, "_capture_via_listener", fake_capture)
    config = Config(keystring="k", shared_secret="s", redirect_uri="http://localhost:3003/oauth/redirect")
    event = threading.Event()
    with pytest.raises(AuthError):
        auth.login(config, open_browser=False, cancel=event)
    assert seen["cancel"] is event


# --- callback and token exchange details the connect flow depends on -----------


def test_only_a_plain_http_localhost_callback_is_caught_by_the_listener():
    assert auth.is_loopback("http://localhost:3003/oauth/redirect")
    # The listener has no TLS: a browser sent to https://localhost would never arrive.
    assert not auth.is_loopback("https://localhost:3003/oauth/redirect")


def test_an_https_localhost_callback_uses_the_paste_flow(monkeypatch):
    monkeypatch.setattr(auth, "_capture_via_listener", lambda *a, **k: pytest.fail("listener used"))
    monkeypatch.setattr(auth, "exchange_code", lambda config, code, verifier: code)
    config = Config(keystring="k", shared_secret="s", redirect_uri="https://localhost:3003/oauth/redirect")
    asked = []

    def prompt(question):
        asked.append(question)
        return "https://localhost:3003/oauth/redirect?code=the-code"

    assert auth.login(config, open_browser=False, prompt=prompt) == "the-code"
    assert asked


def test_a_form_token_request_refused_for_its_format_is_retried_as_json(monkeypatch):
    import httpx

    calls = []

    def post(url, *, data=None, json=None, timeout=None):
        calls.append("json" if json is not None else "form")
        if json is None:
            return httpx.Response(403, text="Invalid API key: should be in the format 'keystring:shared_secret'.")
        return httpx.Response(200, json={"access_token": "1.a", "refresh_token": "r", "expires_in": 3600})

    monkeypatch.setattr(auth.httpx, "post", post)
    assert auth._post_token({"grant_type": "refresh_token"})["access_token"] == "1.a"
    assert calls == ["form", "json"]


def test_other_token_rejections_are_not_retried(monkeypatch):
    import httpx

    calls = []

    def post(url, *, data=None, json=None, timeout=None):
        calls.append(url)
        return httpx.Response(400, text='{"error":"invalid_grant"}')

    monkeypatch.setattr(auth.httpx, "post", post)
    with pytest.raises(AuthError, match="invalid_grant"):
        auth._post_token({"grant_type": "authorization_code"})
    assert len(calls) == 1


def test_the_page_after_etsy_says_where_to_go_and_escapes_etsy_text():
    import http.server

    import httpx

    port = _free_port()
    auth._CallbackHandler.result = {}
    auth._CallbackHandler.expected_path = "/oauth/redirect"
    server = http.server.HTTPServer(("127.0.0.1", port), auth._CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        ok = httpx.get(f"http://127.0.0.1:{port}/oauth/redirect?code=c&state=s")
        bad = httpx.get(f"http://127.0.0.1:{port}/oauth/redirect",
                        params={"error": "access_denied", "error_description": "<script>x</script>"})
    finally:
        server.shutdown()
        server.server_close()
    assert ok.status_code == 200
    # Receipt, not success: the state check and the token exchange come after it.
    assert "etsyotomasyon" in ok.text and "yanıt verdi" in ok.text and "terminal" not in ok.text
    assert "Connected" not in ok.text
    assert bad.status_code == 400
    assert "<script>" not in bad.text and "&lt;script&gt;" in bad.text


def test_a_refused_tracking_upload_explains_the_country_restriction():
    from etsyotomasyon.errors import EtsyApiError

    refused = EtsyApiError(403, "Unauthorized", method="POST", path="/shops/1/receipts/2/tracking")
    assert "Türkiye" in refused.hint() and "scope" not in refused.hint()
    other = EtsyApiError(403, "Forbidden", method="GET", path="/shops/1/receipts")
    assert "scope" in other.hint()


def test_a_tracking_upload_refused_for_a_missing_scope_says_so():
    from etsyotomasyon.errors import EtsyApiError

    scope = EtsyApiError(403, "Missing required scope(s): transactions_w", method="POST",
                         path="/shops/1/receipts/2/tracking")
    assert "Türkiye" not in scope.hint()
    assert "scope" in scope.hint()



# --- review findings: the working-directory .env, init, ETSYOTOMASYON_SHOP ------------


def test_a_working_directory_env_never_overrides_another_shops_keys():
    with open(".env", "w", encoding="utf-8") as handle:
        handle.write("ETSY_KEYSTRING=first-shop-key\nETSY_SHARED_SECRET=s1\nETSY_SHOP_ID=111\n")
    second = shops.add()
    (second.home / ".env").write_text("ETSY_KEYSTRING=second-key\nETSY_SHARED_SECRET=s2\n",
                                      encoding="utf-8")
    shops.select(second.id)
    config = Config.load()
    assert config.keystring == "second-key"
    assert config.shop_id is None  # shop 1's ETSY_SHOP_ID did not leak either


def test_init_writes_the_selected_shops_keys_into_that_shops_home():
    shops.add()
    result = runner.invoke(
        cli.app,
        ["--shop", "shop-2", "init", "--keystring", "SHOP2KEY",
         "--redirect-uri", "http://localhost:3003/oauth/redirect", "--no-check"],
        input="SHOP2SECRET\n",
    )
    assert result.exit_code == 0, result.output
    written = base_home() / "shops" / "shop-2" / ".env"
    assert "ETSY_KEYSTRING=SHOP2KEY" in written.read_text(encoding="utf-8")
    assert not os.path.exists(".env")


def test_init_without_a_shop_writes_where_the_desktop_app_reads():
    result = runner.invoke(
        cli.app,
        ["init", "--keystring", "KEY1", "--redirect-uri", "http://localhost:3003/oauth/redirect",
         "--no-check"],
        input="SECRET1\n",
    )
    assert result.exit_code == 0, result.output
    assert "ETSY_KEYSTRING=KEY1" in (base_home() / ".env").read_text(encoding="utf-8")


def test_the_environment_variable_resolves_like_the_option(monkeypatch):
    monkeypatch.setenv("ETSYOTOMASYON_SHOP", "default")
    assert home_dir() == base_home()
    listed = runner.invoke(cli.app, ["shops", "list"])
    assert listed.exit_code == 0, listed.output

    monkeypatch.setenv("ETSYOTOMASYON_SHOP", "shop-typo")
    result = runner.invoke(cli.app, ["shops", "list"])
    assert result.exit_code != 0
    assert isinstance(result.exception, ConfigError)
    assert not (base_home() / "shops" / "shop-typo").exists()



def _idle_client(port: int) -> socket.socket:
    """What a browser's speculative preconnect looks like: connected, silent."""
    for _ in range(50):
        try:
            return socket.create_connection(("127.0.0.1", port), timeout=2)
        except OSError:
            time.sleep(0.05)
    raise AssertionError("listener never came up")


def test_an_idle_browser_connection_does_not_block_cancelling_the_etsy_sign_in():
    port = _free_port()
    config = Config(keystring="k", shared_secret="s", redirect_uri=f"http://localhost:{port}/oauth/redirect")
    request = auth.build_authorization_url(config)
    cancel = threading.Event()
    idle = []
    threading.Timer(0.3, lambda: idle.append(_idle_client(port))).start()
    threading.Timer(0.8, cancel.set).start()
    started = time.time()
    try:
        with pytest.raises(AuthError, match="Cancelled"):
            auth._capture_via_listener(request, config, port, timeout=60, cancel=cancel)
        assert time.time() - started < 6
    finally:
        for sock in idle:
            sock.close()


def test_connecting_pinterest_can_be_cancelled_too():
    from etsyotomasyon import pinterest

    port = _free_port()
    config = pinterest.PinterestConfig(app_id="a", app_secret="s",
                                       redirect_uri=f"http://localhost:{port}/")
    cancel = threading.Event()
    idle = []
    threading.Timer(0.3, lambda: idle.append(_idle_client(port))).start()
    threading.Timer(0.8, cancel.set).start()
    started = time.time()
    try:
        with pytest.raises(AuthError, match="Cancelled"):
            pinterest._listen_for_code(config, "state", timeout=60, cancel=cancel)
        assert time.time() - started < 6
    finally:
        for sock in idle:
            sock.close()
