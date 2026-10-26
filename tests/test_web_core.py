"""The web app's core API: status, shops, preferences, jobs, notifications, files, events.

Everything runs against a real server on a free port (the `web` fixture) with a
fake Etsy behind the shared EtsyClient. Nothing touches the network or the real
~/.etsyotomasyon or Desktop.
"""

from __future__ import annotations

import io
import json
import threading
import time

import httpx
import pytest
from PIL import Image
from web_helpers import (
    ETSY_SHOP_ID,
    SHOP_NAME,
    FakeEtsy,
    read_events,
    use_fake_etsy,
    wait_for_job,
)

from etsyotomasyon import auth, shops
from etsyotomasyon.client import EtsyClient
from etsyotomasyon.config import home_dir
from etsyotomasyon.errors import (
    AuthError,
    AuthUnreachable,
    ConfigError,
    EtsyApiError,
    ValidationError,
)
from etsyotomasyon.web import context as context_mod
from etsyotomasyon.web import events as events_mod
from etsyotomasyon.web import files as files_mod
from etsyotomasyon.web.context import ShopLock
from etsyotomasyon.web.errors import describe, to_api_error
from etsyotomasyon.web.router import ApiError, Request, Response, Router


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    """Retries without the real pauses between them."""
    monkeypatch.setattr(EtsyClient, "_backoff", staticmethod(lambda attempt: 0.0))


def status(web) -> dict:
    return web.ctx.refresh_status(force=True)


# --- status ------------------------------------------------------------------------


def test_status_without_keys_says_keys_and_needs_no_network(web):
    resp = web.client.get("/api/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "keys"
    assert data["setup"]["keys"] is False and data["setup"]["connected"] is False
    assert data["shop"]["id"] == ""
    assert data["setup"]["workspace"].startswith(str(web.desktop))
    assert set(data) >= {"state", "detail", "checked_at", "shop", "setup", "scopes",
                         "quota_remaining"}


def test_status_with_keys_but_no_sign_in_is_disconnected(web):
    fake = use_fake_etsy(web, connected=False)
    data = status(web)
    assert data["state"] == "disconnected"
    assert data["setup"]["keys"] is True and data["setup"]["connected"] is False
    assert fake.calls == [("GET", "/openapi-ping")]
    assert fake.requests[0].headers["x-api-key"] == "KEY123:SECRET456"


def test_status_connected_names_the_shop_and_remembers_it(web):
    fake = use_fake_etsy(web)
    data = status(web)
    assert data["state"] == "connected"
    assert data["shop"] == {"id": "", "name": SHOP_NAME, "etsy_shop_id": ETSY_SHOP_ID,
                            "currency": "USD"}
    assert data["setup"]["connected"] is True
    assert "transactions_r" in data["scopes"]
    assert shops.current().name == SHOP_NAME
    assert ("GET", f"/shops/{ETSY_SHOP_ID}") in fake.calls


def test_status_offline_when_etsy_cannot_be_reached(web):
    fake = use_fake_etsy(web)
    fake.offline = True
    started = time.monotonic()
    assert status(web)["state"] == "offline"
    assert time.monotonic() - started < 5
    assert len(fake.calls) == 2  # a status check tries twice, not five times


def test_status_bad_keys_when_etsy_refuses_them(web):
    fake = use_fake_etsy(web, connected=False)
    fake.error("GET", "/openapi-ping", 403,
               "Invalid API key: should be in the format 'keystring:shared_secret'.")
    assert status(web)["state"] == "bad_keys"


def test_status_reconnect_when_the_sign_in_is_revoked(web, monkeypatch):
    fake = use_fake_etsy(web)
    fake.error("GET", "/users/me", 401, "invalid_token")

    def refused(token, config):
        raise AuthError("Token endpoint rejected the request (400)")

    monkeypatch.setattr(auth, "refresh", refused)
    assert status(web)["state"] == "reconnect"


def test_status_error_for_anything_else(web):
    fake = use_fake_etsy(web)
    fake.error("GET", "/users/me", 500, "boom")
    assert status(web)["state"] == "error"


def test_status_refresh_is_debounced_unless_forced(web):
    fake = use_fake_etsy(web, connected=False)
    first = web.client.post("/api/status/refresh", json={"force": True}).json()
    assert first["state"] == "disconnected"
    again = web.client.post("/api/status/refresh", json={}).json()
    assert again["checked_at"] == first["checked_at"]
    assert len(fake.calls) == 1
    forced = web.client.post("/api/status/refresh", json={"force": True}).json()
    assert forced["checked_at"] >= first["checked_at"] and len(fake.calls) == 2


def _wait(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def _revoked(fake, monkeypatch):
    """Etsy refuses the access token and the refresh token: the sign-in was revoked."""
    fake.error("GET", "/users/me", 401, "invalid_token")
    fake.error("GET", f"/shops/{ETSY_SHOP_ID}", 401, "invalid_token")

    def refused(token, config):
        raise AuthError("Token endpoint rejected the request (400): invalid_grant")

    monkeypatch.setattr(auth, "refresh", refused)


def test_a_request_that_finds_the_sign_in_revoked_rechecks_the_status(web, monkeypatch):
    monkeypatch.setattr(context_mod, "SUSPECT_GAP", 0.0)
    fake = use_fake_etsy(web)
    assert status(web)["state"] == "connected"
    _revoked(fake, monkeypatch)
    web.server.router.get("/api/test/shop", lambda req: req.ctx.client().shop())

    def ask():
        resp = web.client.get("/api/test/shop")
        assert resp.json()["error"]["code"] == "reconnect"

    def pushed(events):
        return any(t == "status" and d["state"] == "reconnect" for t, d in events)

    # Nobody pressed a refresh button: the failed request alone flips the status, and
    # every tab hears it (shop card, banner, Mağaza Bağlantısı's reconnect button).
    assert pushed(read_events(web, pushed, after_connect=ask))
    assert web.client.get("/api/status").json()["state"] == "reconnect"


def test_a_request_that_finds_etsy_offline_rechecks_and_then_keeps_trying(web, monkeypatch):
    monkeypatch.setattr(context_mod, "SUSPECT_GAP", 0.0)
    monkeypatch.setattr(context_mod, "RETRY_DELAYS", (0.3,))
    fake = use_fake_etsy(web)
    assert status(web)["state"] == "connected"
    web.server.router.get("/api/test/shop", lambda req: req.ctx.client().shop())
    fake.offline = True
    assert web.client.get("/api/test/shop").json()["error"]["code"] == "offline"
    assert _wait(lambda: web.ctx.status["state"] == "offline")
    # Etsy is back: the status clears by itself, as the banner promises.
    fake.offline = False
    assert _wait(lambda: web.ctx.status["state"] == "connected")


def test_a_problem_the_status_already_shows_is_not_checked_again(web, monkeypatch):
    fake = use_fake_etsy(web)
    fake.offline = True
    assert status(web)["state"] == "offline"
    asked = []
    monkeypatch.setattr(web.ctx, "set_status_soon", lambda delay=0.3: asked.append(delay))

    def fail(code):
        def handler(req):
            raise ApiError(503 if code == "offline" else 401, code, "from a test")
        return handler

    web.server.router.get("/api/test/offline", fail("offline"))
    web.server.router.get("/api/test/reconnect", fail("reconnect"))
    web.server.router.get("/api/test/other", fail("not_found"))
    assert web.client.get("/api/test/offline").status_code == 503
    assert web.client.get("/api/test/other").status_code == 401
    assert asked == []
    assert web.client.get("/api/test/reconnect").status_code == 401
    assert len(asked) == 1 and asked[0] <= context_mod.SUSPECT_GAP


def test_an_error_a_page_shows_inside_its_answer_counts_too(web, monkeypatch):
    use_fake_etsy(web)
    assert status(web)["state"] == "connected"
    asked = []
    monkeypatch.setattr(web.ctx, "set_status_soon", lambda delay=0.3: asked.append(delay))
    # Like the Panel's tiles: each one's error is described, the answer is a 200.
    web.server.router.get("/api/test/tiles",
                          lambda req: {"orders": {"error": describe(AuthError("revoked"))}})
    assert web.client.get("/api/test/tiles").status_code == 200
    assert len(asked) == 1
    # Outside a request (a job, a script) nothing is scheduled from here.
    to_api_error(AuthError("revoked"))
    assert len(asked) == 1


def test_a_sooner_status_check_replaces_a_later_one(web):
    use_fake_etsy(web, connected=False)
    web.ctx.set_status_soon(60.0)
    web.ctx.set_status_soon(0.05)
    assert _wait(lambda: web.ctx.status["checked_at"] is not None, 3)
    first = web.ctx.status["checked_at"]
    web.ctx.set_status_soon(0.05)
    web.ctx.set_status_soon(60.0)  # a later one does not push the sooner one back
    assert _wait(lambda: web.ctx.status["checked_at"] != first, 3)


def test_hidden_names_replace_the_shop_name_everywhere(web):
    use_fake_etsy(web)
    status(web)
    assert web.client.post("/api/prefs", json={"language": "en", "anonymise": True}).status_code == 200
    body = json.dumps([
        web.client.get("/api/status").json(),
        web.client.get("/api/session").json(),
        web.client.get("/api/shops").json(),
    ])
    assert SHOP_NAME not in body
    assert web.client.get("/api/status").json()["shop"]["name"] == "Shop 1"
    web.client.post("/api/prefs", json={"language": "tr"})
    assert web.client.get("/api/status").json()["shop"]["name"] == "Mağaza 1"
    assert web.ctx.anonymise(SHOP_NAME) == "Mağaza 1"


def test_setup_facts_count_the_workspace(web):
    ws = web.ctx.workspace()
    for name in ("shirt.png", "mug.png"):
        Image.new("RGB", (40, 30)).save(ws.mockups / name)
    (ws.mockups / "positions.json").write_text(
        json.dumps({"shirt.png": {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5}}), encoding="utf-8")
    ws.template_path.write_text(json.dumps({"source_listing_id": 1000001,
                                            "source_title": "Example template"}), encoding="utf-8")
    Image.new("RGB", (40, 30)).save(ws.products / "design-one.png")
    Image.new("RGB", (40, 30)).save(ws.products / "design-two.png")
    (ws.root / "upload-history.json").write_text(
        json.dumps({"12345678": {"design-one.png": {"status": "ok"}}}), encoding="utf-8")
    setup = status(web)["setup"]
    assert setup["mockups"] == 2 and setup["mockups_calibrated"] == 1
    assert setup["template"] is True and setup["template_title"] == "Example template"
    assert setup["designs_pending"] == 1


def test_the_template_title_of_an_old_product_json_is_plain_text(web):
    # Saved before Etsy's HTML entities were decoded: no "plain_text" mark yet.
    ws = web.ctx.workspace()
    ws.template_path.write_text(json.dumps({
        "source_listing_id": 1000001,
        "source_title": "Mom&#39;s &quot;Best&quot; Coffee Mug &amp; Gift",
    }), encoding="utf-8")
    assert status(web)["setup"]["template_title"] == 'Mom\'s "Best" Coffee Mug & Gift'
    ws.template_path.write_text("[1, 2]", encoding="utf-8")  # not a template at all
    setup = status(web)["setup"]
    assert setup["template"] is True and setup["template_title"] is None


def test_pending_designs_read_the_history_through_its_shared_reader(web, monkeypatch):
    # automation.read_history holds the lock the writer holds, so a status check never
    # has the file open while a Tasarım Yükle run replaces it.
    from etsyotomasyon.drop import automation

    ws = web.ctx.workspace()
    Image.new("RGB", (40, 30)).save(ws.products / "design-one.png")
    Image.new("RGB", (40, 30)).save(ws.products / "design-two.png")
    automation.save_history(automation.history_path(ws.root),
                            {"12345678": {"design-one.png": {"status": "ok"}}})
    calls = []
    real = automation.read_history

    def spy(root):
        calls.append(root)
        return real(root)

    monkeypatch.setattr(automation, "read_history", spy)
    assert status(web)["setup"]["designs_pending"] == 1
    assert calls and set(calls) == {ws.root}
    (ws.root / "upload-history.json").write_text("{not json", encoding="utf-8")
    assert status(web)["setup"]["designs_pending"] == 2


# --- the shared client -------------------------------------------------------------------


def test_the_client_is_shared_and_says_what_setup_is_missing(web):
    with pytest.raises(ApiError) as error:
        web.ctx.client()
    assert (error.value.status, error.value.code, error.value.params) == (
        409, "setup_needed", {"step": "keys"})
    use_fake_etsy(web, connected=False)
    with pytest.raises(ApiError) as error:
        web.ctx.client()
    assert error.value.params == {"step": "connect"}
    keys_only = web.ctx.client(require_auth=False)
    assert keys_only is web.ctx.client(require_auth=False)

    use_fake_etsy(web)  # connecting makes a client that holds the token
    client = web.ctx.client()
    assert client is not keys_only and client.token is not None
    assert web.ctx.client() is client
    web.ctx.reset_client()
    assert web.ctx.client() is not client


def test_an_api_error_from_a_handler_reaches_the_browser_as_json(web):
    web.server.router.get("/api/test/needs-shop", lambda req: req.ctx.client())
    resp = web.client.get("/api/test/needs-shop")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "setup_needed"
    assert resp.json()["error"]["params"] == {"step": "keys"}


def test_an_unexpected_exception_is_internal_without_a_traceback(web):
    def boom(req):
        raise RuntimeError("secret detail C:/somewhere")

    web.server.router.get("/api/test/boom", boom)
    resp = web.client.get("/api/test/boom")
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "internal"
    assert "secret detail" not in resp.text and "Traceback" not in resp.text


def test_library_errors_map_to_codes_through_the_api(web):
    def invalid(req):
        raise ValidationError("price must be a number")

    web.server.router.get("/api/test/invalid", invalid)
    resp = web.client.get("/api/test/invalid")
    assert resp.status_code == 422
    assert resp.json()["error"] == {"code": "invalid", "message": "price must be a number",
                                    "params": {}}


# --- shops -----------------------------------------------------------------------------------


def test_shops_are_added_switched_and_removed(web):
    use_fake_etsy(web)
    data = web.client.get("/api/shops").json()
    assert data["current"] == "" and [s["id"] for s in data["shops"]] == [""]

    added = web.client.post("/api/shops/add", json={})
    assert added.status_code == 200
    assert added.json()["current"] == "shop-2"
    assert [s["id"] for s in added.json()["shops"]] == ["", "shop-2"]
    assert web.client.get("/api/session").json()["shop_id"] == "shop-2"
    # A new shop starts empty: none of the first shop's keys leak into it.
    assert status(web)["state"] == "keys"

    switched = web.client.post("/api/shops/switch", json={"id": ""})
    assert switched.json()["current"] == ""
    assert status(web)["state"] == "connected"
    assert web.ctx.app_prefs()["shop"] == ""

    assert web.client.post("/api/shops/switch", json={"id": "nope"}).status_code == 404
    assert web.client.post("/api/shops/switch", json={"id": 5}).status_code == 422

    base = web.client.post("/api/shops/remove", json={"id": "", "confirm": True})
    assert base.status_code == 400 and base.json()["error"]["code"] == "cannot_remove_base"
    unconfirmed = web.client.post("/api/shops/remove", json={"id": "shop-2"})
    assert unconfirmed.status_code == 400
    assert unconfirmed.json()["error"]["code"] == "confirm_required"
    removed = web.client.post("/api/shops/remove", json={"id": "shop-2", "confirm": True})
    assert removed.status_code == 200 and [s["id"] for s in removed.json()["shops"]] == [""]


def test_removing_the_open_shop_opens_the_first_one(web):
    web.client.post("/api/shops/add", json={})
    resp = web.client.post("/api/shops/remove", json={"id": "shop-2", "confirm": True})
    assert resp.json()["current"] == ""
    assert shops.current().id == ""


def test_the_shop_cannot_change_while_a_job_runs(web):
    release = threading.Event()
    job = web.ctx.jobs.start("test", "common:test", lambda j: release.wait(10))
    try:
        for path, body in (("/api/shops/add", {}), ("/api/shops/switch", {"id": ""}),
                           ("/api/shops/remove", {"id": "shop-2", "confirm": True})):
            resp = web.client.post(path, json=body)
            assert resp.status_code == 409, path
            assert resp.json()["error"]["code"] == "busy"
    finally:
        release.set()
    assert job.wait(5)
    assert web.client.post("/api/shops/add", json={}).status_code == 200


def test_the_last_open_shop_is_reopened_next_time(web):
    from etsyotomasyon.desktop import settings
    from etsyotomasyon.web.context import AppContext

    web.client.post("/api/shops/add", json={})
    settings.use_shop("")  # as if the process had restarted
    again = AppContext(token="x", check_status=False)
    try:
        assert again.shop_id == "shop-2"
    finally:
        again.close()


# --- preferences ---------------------------------------------------------------------------


def test_prefs_round_trip(web):
    prefs = web.client.get("/api/prefs").json()
    assert prefs["language"] in ("tr", "en") and prefs["anonymise"] is False
    assert prefs["workspace"].startswith(str(web.desktop))
    web.ctx.update_shop_prefs(template_listing="1000001")
    saved = web.client.post("/api/prefs", json={"language": "en", "anonymise": True}).json()
    assert saved["language"] == "en" and saved["anonymise"] is True
    assert saved["template_listing"] == "1000001"
    assert web.client.get("/api/session").json()["language"] == "en"
    assert web.ctx.app_prefs()["language"] == "en"


@pytest.mark.parametrize("body", [{"language": "de"}, {"language": 1}, {"anonymise": "yes"}])
def test_prefs_refuse_bad_values(web, body):
    resp = web.client.post("/api/prefs", json=body)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "invalid"


def test_prefs_refuse_a_body_that_is_not_json(web):
    resp = web.client.post("/api/prefs", content=b"{not json",
                           headers={"Content-Type": "application/json"})
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "invalid_json"


def test_session_describes_the_app(web):
    data = web.client.get("/api/session").json()
    assert data["port"] == web.port and data["shop_id"] == ""
    assert data["shops"][0]["id"] == "" and data["shops"][0]["label"]
    assert set(data) >= {"version", "language", "anonymise", "port", "shop_id", "shops"}


# --- jobs ------------------------------------------------------------------------------------


def test_a_job_runs_and_reports_its_result_state_and_log(web):
    def work(job):
        job.progress(1, 2, label="first")
        job.set_state(seen=["a"])
        job.log("half way")
        job.progress(2, 2, label="second")
        return {"made": 2}

    job = web.ctx.jobs.start("test", "common:test", work, params={"n": 2})
    final = wait_for_job(web, job.id)
    assert final["status"] == "done" and final["result"] == {"made": 2}
    assert final["progress"] == {"done": 2, "total": 2, "label": "second"}
    assert final["state"] == {"seen": ["a"]}
    assert final["log"][0]["text"] == "half way"
    assert final["params"] == {"n": 2} and final["error"] is None
    assert final["started_at"] >= final["created_at"] and final["finished_at"]
    listed = web.client.get("/api/jobs", params={"kind": "test"}).json()
    assert [j["id"] for j in listed] == [job.id]
    assert web.client.get("/api/jobs", params={"kind": "other"}).json() == []
    assert web.client.get("/api/jobs", params={"active": "1"}).json() == []


def test_a_failing_job_carries_a_translatable_error(web):
    def work(job):
        raise ValidationError("row 2: price is missing")

    job = web.ctx.jobs.start("test", "common:test", work)
    final = wait_for_job(web, job.id)
    assert final["status"] == "error"
    assert final["error"] == {"code": "invalid", "message": "row 2: price is missing", "params": {}}


def test_a_result_that_is_not_json_is_an_error_not_a_broken_worker(web):
    job = web.ctx.jobs.start("test", "common:test", lambda j: object())
    final = wait_for_job(web, job.id)
    assert final["status"] == "error" and final["error"]["code"] == "internal"
    assert final["result"] is None
    later = web.ctx.jobs.start("test", "common:test", lambda j: "fine")
    assert wait_for_job(web, later.id)["result"] == "fine"


def test_jobs_run_one_at_a_time_and_a_queued_one_can_be_cancelled(web):
    release = threading.Event()
    order: list[str] = []

    def first(job):
        order.append("first")
        release.wait(10)

    first_job = web.ctx.jobs.start("test", "common:test", first)
    second_job = web.ctx.jobs.start("test", "common:test", lambda j: order.append("second"))
    third_job = web.ctx.jobs.start("test", "common:test", lambda j: order.append("third"))
    deadline = time.monotonic() + 5
    while first_job.status != "running" and time.monotonic() < deadline:
        time.sleep(0.01)
    active = web.client.get("/api/jobs", params={"active": "1"}).json()
    assert {j["id"]: j["status"] for j in active} == {
        first_job.id: "running", second_job.id: "queued", third_job.id: "queued"}

    cancelled = web.client.post(f"/api/jobs/{second_job.id}/cancel", json={})
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    release.set()
    assert wait_for_job(web, third_job.id)["status"] == "done"
    assert order == ["first", "third"]


def test_a_running_job_stops_when_it_next_checks(web):
    started = threading.Event()

    def work(job):
        started.set()
        for _ in range(500):
            job.check_cancel()
            time.sleep(0.01)
        return "finished"

    job = web.ctx.jobs.start("test", "common:test", work)
    assert started.wait(5)
    assert web.client.post(f"/api/jobs/{job.id}/cancel", json={}).status_code == 200
    final = wait_for_job(web, job.id)
    assert final["status"] == "cancelled" and final["result"] is None


def test_a_job_that_cannot_be_stopped_says_so(web):
    release = threading.Event()
    job = web.ctx.jobs.start("test", "common:test", lambda j: release.wait(10), cancellable=False)
    deadline = time.monotonic() + 5
    while job.status != "running" and time.monotonic() < deadline:
        time.sleep(0.01)
    resp = web.client.post(f"/api/jobs/{job.id}/cancel", json={})
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "not_cancellable"
    release.set()
    assert wait_for_job(web, job.id)["status"] == "done"


def test_an_unknown_job_is_not_found(web):
    assert web.client.get("/api/jobs/nope").status_code == 404
    assert web.client.post("/api/jobs/nope/cancel", json={}).status_code == 404


def test_only_the_last_50_jobs_are_kept(web):
    jobs = [web.ctx.jobs.start("test", "common:test", lambda j: None) for _ in range(60)]
    assert jobs[-1].wait(10)
    assert len(web.client.get("/api/jobs").json()) == 50


def test_progress_events_are_throttled_but_the_last_one_arrives(web):
    sub = web.ctx.events.subscribe()

    def work(job):
        for n in range(1, 201):
            job.progress(n, 1000)
        time.sleep(0.4)  # let the trailing update fire

    job = web.ctx.jobs.start("test", "common:test", work)
    assert job.wait(10)
    frames = []
    while True:
        frame = sub.get(timeout=0.2)
        if frame is None:
            break
        frames.append(frame)
    sub.close()
    progress = [json.loads(f.split("data: ", 1)[1]) for f in frames if f.startswith("event: job\n")]
    assert len(progress) < 20
    assert any(p["progress"]["done"] == 200 for p in progress)


def test_a_job_holds_the_shop_read_lock(web):
    seen = {}

    def work(job):
        lock = web.ctx.shop_lock
        seen["held"] = threading.get_ident() in lock._readers

    job = web.ctx.jobs.start("test", "common:test", work)
    assert job.wait(5) and seen["held"] is True


# --- notifications ----------------------------------------------------------------------------


def test_notifications_are_stored_per_shop_and_marked_read(web):
    item = web.ctx.notify("orders", "shipped", {"n": 2}, tone="success", link="/siparisler")
    web.ctx.notify("seo", "done", tone="loud")
    data = web.client.get("/api/notifications").json()
    assert data["unread"] == 2
    assert data["items"][0]["tone"] == "info"  # an unknown tone falls back
    newest, oldest = data["items"]
    assert oldest == item and oldest["params"] == {"n": 2} and oldest["read"] is False
    assert (home_dir() / "notifications.json").is_file()

    after = web.client.post("/api/notifications/read", json={"ids": [newest["id"]]}).json()
    assert after["unread"] == 1
    assert web.client.post("/api/notifications/read", json={}).json()["unread"] == 0
    assert web.client.post("/api/notifications/read", json={"ids": "all"}).status_code == 422

    web.client.post("/api/shops/add", json={})
    assert web.client.get("/api/notifications").json() == {"items": [], "unread": 0}


def test_a_shop_name_in_a_notification_follows_the_hide_names_preference(web):
    use_fake_etsy(web)
    status(web)
    web.ctx.notify("connect", "notify.connected", {"shop": SHOP_NAME}, tone="success")
    assert web.client.get("/api/notifications").json()["items"][0]["params"] == {"shop": SHOP_NAME}

    # Turned on later (to take a screenshot, say): older notifications are hidden too.
    web.client.post("/api/prefs", json={"anonymise": True, "language": "tr"})
    assert web.client.get("/api/notifications").json()["items"][0]["params"] == {"shop": "Mağaza 1"}
    for answer in (web.client.get("/api/notifications"), web.client.get("/api/dashboard"),
                   web.client.post("/api/notifications/read", json={})):
        assert SHOP_NAME not in answer.text

    def pushed(events):
        return any(t == "notification" for t, _ in events)

    events = read_events(web, pushed, after_connect=lambda: web.ctx.notify(
        "connect", "notify.connected", {"shop": SHOP_NAME}))
    assert next(d for t, d in events if t == "notification")["params"] == {"shop": "Mağaza 1"}

    # Stored as it was: turned off again, the name comes back.
    web.client.post("/api/prefs", json={"anonymise": False})
    assert web.client.get("/api/notifications").json()["items"][0]["params"] == {"shop": SHOP_NAME}


def test_only_the_last_50_notifications_are_kept(web):
    for n in range(55):
        web.ctx.notify("common", "n", {"n": n})
    items = web.client.get("/api/notifications").json()["items"]
    assert len(items) == 50 and items[0]["params"] == {"n": 54}


# --- files -----------------------------------------------------------------------------------


def _png(path, size=(400, 300), mode="RGB"):
    Image.new(mode, size, (10, 200, 30) if mode == "RGB" else (10, 200, 30, 0)).save(path)


def test_a_workspace_image_is_served(web):
    ws = web.ctx.workspace()
    _png(ws.products / "design one.png")
    resp = web.client.get("/api/files/workspace", params={"path": "2-PRODUCTS/design one.png"})
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/png"
    assert resp.headers["cache-control"] == "no-store"
    versioned = web.client.get("/api/files/workspace",
                               params={"path": "2-PRODUCTS/design one.png", "v": "1"})
    assert versioned.headers["cache-control"].startswith("private")


@pytest.mark.parametrize("path", [
    "../secret.png",
    "..\\secret.png",
    "2-PRODUCTS/../../secret.png",
    "README.txt",
    "",
    "2-PRODUCTS/missing.png",
])
def test_nothing_outside_the_workspace_or_not_an_image_is_served(web, path):
    ws = web.ctx.workspace()
    _png(ws.root.parent / "secret.png")
    resp = web.client.get("/api/files/workspace", params={"path": path})
    assert resp.status_code == 404
    thumb = web.client.get("/api/files/thumb", params={"path": path, "w": 100})
    assert thumb.status_code == 404


def test_an_absolute_path_is_refused(web):
    ws = web.ctx.workspace()
    target = ws.root.parent / "secret.png"
    _png(target)
    for path in (str(target), target.as_posix()):
        assert web.client.get("/api/files/workspace", params={"path": path}).status_code == 404


def test_a_thumbnail_is_a_small_jpeg_flattened_on_white_and_cached(web):
    ws = web.ctx.workspace()
    _png(ws.mockups / "clear.png", mode="RGBA")
    resp = web.client.get("/api/files/thumb", params={"path": "1-MOCKUPS/clear.png", "w": 100})
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/jpeg"
    with Image.open(io.BytesIO(resp.content)) as image:
        assert image.size == (100, 75)
        assert image.getpixel((50, 37))[0] > 240  # transparent -> white, not black
    cached = list((home_dir() / "cache" / "thumbs").glob("*.jpg"))
    assert len(cached) == 1
    small = web.client.get("/api/files/thumb", params={"path": "1-MOCKUPS/clear.png", "w": 5})
    with Image.open(io.BytesIO(small.content)) as image:
        assert image.width == 64
    bad = web.client.get("/api/files/thumb", params={"path": "1-MOCKUPS/clear.png", "w": "big"})
    assert bad.status_code == 422


def test_a_file_that_is_not_really_an_image_has_no_thumbnail(web):
    ws = web.ctx.workspace()
    (ws.products / "broken.png").write_bytes(b"not a png")
    resp = web.client.get("/api/files/thumb", params={"path": "2-PRODUCTS/broken.png"})
    assert resp.status_code == 404


def test_open_folder_opens_the_folder_asked_for(web, monkeypatch):
    opened = []
    monkeypatch.setattr(files_mod, "open_path", opened.append)
    resp = web.client.post("/api/open-folder", json={"which": "mockups"})
    assert resp.status_code == 200
    assert opened[0].name == "1-MOCKUPS" and opened[0].is_dir()
    assert web.client.post("/api/open-folder", json={"which": "C:/"}).status_code == 422


# --- quit ----------------------------------------------------------------------------------------


def test_quit_stops_the_app_but_not_during_a_job(web):
    stopped = threading.Event()
    web.ctx.on_quit = stopped.set
    release = threading.Event()
    job = web.ctx.jobs.start("test", "common:test", lambda j: release.wait(10))
    busy = web.client.post("/api/quit", json={})
    assert busy.status_code == 409 and busy.json()["error"]["code"] == "busy"
    assert not stopped.is_set()
    forced = web.client.post("/api/quit", json={"force": True})
    assert forced.status_code == 200 and forced.json() == {"ok": True}
    assert stopped.wait(3)
    release.set()
    job.wait(5)


# --- events -----------------------------------------------------------------------------------


def test_the_event_stream_says_hello_then_sends_the_status(web):
    events = read_events(web, lambda e: len(e) >= 2)
    assert events[0] == ("hello", {"instance": web.ctx.instance, "version": web.ctx.version})
    assert events[1][0] == "status" and "state" in events[1][1]


def test_published_events_carry_growing_ids(web):
    # The tabs of one browser share one stream (a leader relays it); while the leader
    # changes, two streams overlap and the id drops the doubles.
    sub = web.ctx.events.subscribe()
    try:
        web.ctx.events.publish("job", {"n": 1})
        web.ctx.events.publish("notification", {"n": 2})
        frames = [sub.get(timeout=1), sub.get(timeout=1)]
    finally:
        sub.close()
    ids = []
    for frame, (topic, data) in zip(frames, [("job", {"n": 1}), ("notification", {"n": 2})]):
        lines = frame.rstrip("\n").split("\n")
        assert lines[0] == f"event: {topic}" and lines[1].startswith("id: ")
        assert json.loads(lines[2][len("data: "):]) == data
        ids.append(int(lines[1][len("id: "):]))
    assert ids[1] == ids[0] + 1
    # The frames a new stream starts with are its own: no id.
    assert "id:" not in events_mod.encode("hello", {})


def test_status_changes_are_pushed(web):
    use_fake_etsy(web, connected=False)

    def pushed(events):
        return any(t == "status" and d["state"] == "disconnected" for t, d in events)

    events = read_events(web, pushed, after_connect=lambda: web.ctx.refresh_status(force=True))
    assert pushed(events)


def test_jobs_notifications_and_shop_changes_are_pushed(web):
    def trigger():
        web.ctx.notify("orders", "shipped")
        job = web.ctx.jobs.start("test", "common:test", lambda j: j.emit("step", n=1))
        assert job.wait(5)  # a shop change is refused while a job is queued or running
        assert web.client.post("/api/shops/add", json={}).status_code == 200

    def complete(events):
        topics = [t for t, _ in events]
        return {"notification", "job-event", "shop"} <= set(topics) and any(
            t == "job" and d["status"] == "done" for t, d in events)

    events = read_events(web, complete, after_connect=trigger)
    job_event = next(d for t, d in events if t == "job-event")
    assert job_event["type"] == "step" and job_event["data"] == {"n": 1}
    assert next(d for t, d in events if t == "shop") == {"id": "shop-2"}


# --- units: router, errors, lock ------------------------------------------------------------------


def test_router_patterns_convert_and_decode():
    router = Router()
    router.get("/api/listings/{id:int}", lambda req: req.params)
    router.get("/api/mockups/{name}", lambda req: req.params)

    @router.post("/api/files/{rest:path}")
    def rest(req):
        return req.params

    route, params = router.match("GET", "/api/listings/1000001")
    assert params == {"id": 1000001}
    assert router.match("GET", "/api/mockups/shirt%20white%2F.png")[1] == {"name": "shirt white/.png"}
    assert router.match("POST", "/api/files/a/b.png")[1] == {"rest": "a/b.png"}
    with pytest.raises(ApiError) as error:
        router.match("GET", "/api/listings/abc")
    assert error.value.status == 404
    with pytest.raises(ApiError) as error:
        router.match("DELETE", "/api/listings/1")
    assert error.value.status == 405
    with pytest.raises(ValueError):
        router.get("/api/listings/{id:int}", lambda req: None)


def test_request_helpers():
    req = Request("GET", "/x", {"page": "2", "on": "true"}, {}, raw_query="tag=a&tag=b&page=2&on=true")
    assert req.query_list("tag") == ["a", "b"]
    assert req.int_query("page", 1, min=1) == 2
    assert req.int_query("missing", 7) == 7
    assert req.bool_query("on") is True and req.bool_query("off", True) is True
    with pytest.raises(ApiError):
        req.int_query("page", 1, max=1)
    assert Request("POST", "/x", {}, {}, body=b"").json() == {}
    with pytest.raises(ApiError) as error:
        Request("POST", "/x", {}, {}, body=b"[1]").json_object()
    assert error.value.code == "invalid_json"
    assert Response.json({"a": 1}).body == b'{"a":1}'


@pytest.mark.parametrize("exc, status, code", [
    (ConfigError("no keys"), 409, "setup_needed"),
    (AuthUnreachable("down"), 503, "offline"),
    (AuthError("revoked"), 401, "reconnect"),
    (ValidationError("bad"), 422, "invalid"),
    (EtsyApiError(0, "network error"), 503, "offline"),
    (EtsyApiError(401, "invalid_token"), 401, "reconnect"),
    (EtsyApiError(403, "Invalid API key: should be in the format"), 403, "bad_keys"),
    (EtsyApiError(403, "Unauthorized", path="/shops/1/receipts/2/tracking"), 403,
     "tracking_restricted"),
    (EtsyApiError(403, "insufficient scope", path="/shops/1/listings"), 502, "etsy_error"),
    (EtsyApiError(404, "not found"), 404, "not_found"),
    (EtsyApiError(429, "slow down"), 429, "rate_limited"),
    (EtsyApiError(500, "oops"), 502, "etsy_error"),
    (FileNotFoundError("x"), 404, "not_found"),
    (RuntimeError("secret"), 500, "internal"),
    (ApiError(418, "teapot", "short and stout", size="small"), 418, "teapot"),
])
def test_errors_map_to_status_and_code(exc, status, code):
    error = to_api_error(exc)
    assert (error.status, error.code) == (status, code)
    if code == "etsy_error":
        assert error.params == {"status": exc.status}
    if code == "internal":
        assert "secret" not in error.message


def test_the_tracking_refusal_explains_itself():
    error = to_api_error(EtsyApiError(403, "Unauthorized", path="/shops/1/receipts/2/tracking"))
    assert "Shop Manager" in error.message


def test_the_shop_lock_lets_readers_share_and_writers_wait():
    lock = ShopLock()
    with lock.read(), lock.read():
        pass
    reading = threading.Event()
    done = threading.Event()

    def reader():
        with lock.read():
            reading.set()
            done.wait(5)

    thread = threading.Thread(target=reader)
    thread.start()
    assert reading.wait(5)
    with pytest.raises(ApiError) as error, lock.write(timeout=0.1):
        pass
    assert error.value.code == "busy"
    done.set()
    thread.join(5)
    with lock.write(timeout=1), lock.read(), lock.write(timeout=0.1):
        pass  # the writing thread may read, and write again


def test_the_fake_etsy_answers_unknown_paths_with_404():
    fake = FakeEtsy()
    resp = fake(httpx.Request("GET", "https://openapi.etsy.com/v3/application/nothing"))
    assert resp.status_code == 404
