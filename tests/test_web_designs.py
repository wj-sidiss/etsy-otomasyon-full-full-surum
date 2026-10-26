"""Tasarım Yükle endpoints: uploads, the pending list, the draft run and its recovery."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time

import httpx
import pytest
from PIL import Image
from web_helpers import ETSY_SHOP_ID, read_events, use_fake_etsy, wait_for_job

from etsyotomasyon.drop import mockup, stream
from etsyotomasyon.drop.template import Template
from etsyotomasyon.web.api import designs

TEMPLATE_ID = 1000001
SHOP = str(ETSY_SHOP_ID)


def _png(colour=(200, 60, 40), transparent=True) -> bytes:
    image = Image.new("RGBA", (40, 40), (0, 0, 0, 0) if transparent else (*colour, 255))
    for x in range(10, 30):
        for y in range(10, 30):
            image.putpixel((x, y), (*colour, 255))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _jpg(colour=(240, 240, 240)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (40, 40), colour).save(buffer, "JPEG")
    return buffer.getvalue()


def _photo_jpg() -> bytes:
    """A finished product photo: no border is one colour (never a "flat" ground, which
    the start card's "place" choice would put onto the mockups)."""
    buffer = io.BytesIO()
    Image.effect_noise((40, 40), 80).convert("RGB").save(buffer, "JPEG", quality=95)
    return buffer.getvalue()


def _put(web, path, data, **query):
    return web.client.put("/api/designs/files", params={"path": path, **query}, content=data)


@pytest.fixture
def fast_images(monkeypatch):
    """Compositing at the real 2000 px output is slow and beside the point here."""
    def small_compose(design, template_image, out, *, area=None, **_kw):
        out.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (30, 30), (200, 200, 200)).save(out, "JPEG")
        return out

    def small_flat(design, out, **_kw):
        out.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (30, 30), (255, 255, 255)).save(out, "JPEG")
        return out

    monkeypatch.setattr(mockup, "compose", small_compose)
    monkeypatch.setattr(mockup, "flatten_design", small_flat)
    # These tiny pictures would each get a "may look soft" warning (tests/test_vp_flow.py).
    monkeypatch.setattr(stream, "SMALL_IMAGE_EDGE", 0)
    monkeypatch.setattr(mockup, "MAX_UPSCALE", float("inf"))


def _setup_shop(web, *, connected=True, template=True, mockups=2):
    fake = use_fake_etsy(web, connected=connected)
    ws = web.ctx.workspace()
    for n, name in enumerate(["tshirt-white.jpg", "mug-white.jpg", "tote-natural.jpg"][:mockups]):
        (ws.mockups / name).write_bytes(_jpg((230 + n, 230, 230)))
    if template:
        ws.write_template(Template(TEMPLATE_ID, source_title="Retro Mountain Sunset Shirt", fields={
            "taxonomy_id": 482, "price": 21.0, "quantity": 10, "who_made": "i_did",
            "when_made": "made_to_order", "type": "physical", "shipping_profile_id": 5551,
        }, description="Soft ringspun cotton tee.", tags=["gift for him"]).to_dict())
    listing_ids = iter(range(2000001, 2000100))
    created: list[dict] = []

    def create(request: httpx.Request):
        form = dict(httpx.QueryParams(request.content.decode()))
        created.append(form)
        return {"listing_id": next(listing_ids), "state": "draft"}

    fake.add("POST", f"/shops/{ETSY_SHOP_ID}/listings", create)
    for listing_id in range(2000001, 2000020):
        fake.add("POST", f"/shops/{ETSY_SHOP_ID}/listings/{listing_id}/images",
                 {"listing_image_id": listing_id * 10})
    fake.add("GET", f"/listings/{TEMPLATE_ID}/inventory", {"products": [{
        "product_id": 1, "sku": "", "is_deleted": False, "property_values": [],
        "offerings": [{"offering_id": 2, "is_deleted": False, "is_enabled": True, "quantity": 10,
                       "price": {"amount": 2100, "divisor": 100, "currency_code": "USD"}}]}]})

    def search(request: httpx.Request):
        keywords = request.url.params.get("keywords", "design")
        first = keywords.split()[0]
        return {"count": 30, "results": [
            {"listing_id": 3000000 + n, "title": f"{keywords} shirt vintage gift {n}",
             "tags": [f"{first} tee", "vintage gift", f"extra tag {n % 12}"],
             "price": {"amount": 2000, "divisor": 100, "currency_code": "USD"},
             "num_favorers": n} for n in range(30)]}

    fake.add("GET", "/listings/active", search)
    web.ctx.refresh_status(force=True)
    fake.created = created
    return fake, ws


# --- setup and blockers -------------------------------------------------------------------


def test_without_keys_the_pending_view_says_what_is_missing(web):
    resp = web.client.get("/api/designs/pending")
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == [] and data["ready"] is False
    assert data["blockers"] == ["keys", "template", "empty"]
    assert data["mockups"] == {"enabled": 0, "used": 0, "switched_on": 0, "over_limit": 0,
                               "max": 19, "main": None, "main_type": None, "total": 0,
                               "types": {}, "primary": None, "names": [], "used_items": [],
                               "left_out": []}
    assert data["shop"]["connected"] is False

    start = web.client.post("/api/designs/start", json={})
    assert start.status_code == 409
    error = start.json()["error"]
    assert error["code"] == "setup_incomplete"
    assert error["params"]["blockers"] == ["keys", "template", "empty"]


def test_a_connected_shop_with_a_template_and_designs_is_ready(web, fast_images):
    _fake, ws = _setup_shop(web)
    assert _put(web, "retro-mountain-sunset.png", _png()).status_code == 200
    assert _put(web, "IMG_2043.png", _png((10, 200, 30))).status_code == 200
    data = web.client.get("/api/designs/pending").json()
    assert data["ready"] is True and data["blockers"] == []
    by_name = {item["name"]: item for item in data["items"]}
    assert by_name["retro-mountain-sunset.png"]["concept"] == "retro mountain sunset"
    assert by_name["retro-mountain-sunset.png"]["thumb_path"] == "2-PRODUCTS/retro-mountain-sunset.png"
    assert by_name["IMG_2043.png"]["junk_reason"]
    assert "junk" in data["warnings"]
    assert data["mockups"]["enabled"] == 2 and data["mockups"]["types"] == {"tshirt": 1, "mug": 1}
    assert data["template"]["title"] == "Retro Mountain Sunset Shirt"
    assert data["template"]["price"] == 21.0 and data["template"]["shipping_profile"] is True
    assert data["shop"] == {"connected": True, "name": "ExampleShop", "state": "connected"}
    assert data["estimate_requests"] > 0


def test_a_template_that_cannot_make_a_draft_blocks_the_start(web):
    _fake, ws = _setup_shop(web)
    data = json.loads(ws.template_path.read_text(encoding="utf-8"))
    data["fields"]["price"] = -3
    ws.write_template(data)
    _put(web, "retro-mountain-sunset.png", _png())
    pending = web.client.get("/api/designs/pending").json()
    assert pending["blockers"] == ["template_invalid"]
    assert "price" in pending["template"]["invalid"]


def test_offline_is_a_blocker_for_a_real_run_but_not_for_a_check(web, fast_images):
    fake, _ws = _setup_shop(web)
    _put(web, "retro-mountain-sunset.png", _png())
    fake.offline = True
    web.ctx.refresh_status(force=True)
    assert web.client.get("/api/designs/pending").json()["blockers"] == ["offline"]
    assert web.client.post("/api/designs/start", json={}).json()["error"]["params"] == {
        "blockers": ["offline"]}
    job = web.client.post("/api/designs/start", json={"dry_run": True})
    assert job.status_code == 200
    final = wait_for_job(web, job.json()["id"])
    assert final["status"] == "done"
    assert final["state"]["items"][0]["status"] == "checked"


# --- uploads ---------------------------------------------------------------------------------


def test_uploads_land_in_the_products_folder_without_overwriting(web):
    ws = web.ctx.workspace()
    first = _put(web, "mountain sunset.png", _png())
    assert first.status_code == 200
    assert first.json() == {"name": "mountain sunset.png", "file": "mountain sunset.png",
                            "folder": None, "path": "2-PRODUCTS/mountain sunset.png",
                            "duplicate": False, "known": False, "ignored": None,
                            "size": len(_png())}
    same = _put(web, "mountain sunset.png", _png()).json()
    assert same["duplicate"] is True and same["name"] == "mountain sunset.png"
    other = _put(web, "Mountain Sunset.PNG", _png((1, 2, 3))).json()
    assert other["name"] == "Mountain Sunset-2.png" and other["duplicate"] is False
    assert sorted(p.name for p in ws.products.iterdir()) == [
        "Mountain Sunset-2.png", "mountain sunset.png"]


def test_a_dropped_folder_becomes_one_product_folder_per_batch(web):
    ws = web.ctx.workspace()
    for name in ("01-front.jpg", "02-back.jpg"):
        resp = _put(web, f"desert cactus print/{name}", _jpg(), batch="b1")
        assert resp.json()["name"] == "desert cactus print"
    again = _put(web, "desert cactus print/01-front.jpg", _jpg((9, 9, 9)), batch="b2").json()
    assert again["name"] == "desert cactus print-2", "a later drop never merges into an old product"
    assert sorted(p.name for p in (ws.products / "desert cactus print").iterdir()) == [
        "01-front.jpg", "02-back.jpg"]
    pending = web.client.get("/api/designs/pending").json()
    folder = next(i for i in pending["items"] if i["name"] == "desert cactus print")
    assert folder["kind"] == "folder" and folder["files"] == 2
    assert folder["thumb_path"] == "2-PRODUCTS/desert cactus print/01-front.jpg"


@pytest.mark.parametrize("path, code", [
    ("../escape.png", "invalid"),
    ("a/b/c.png", "invalid"),
    (".hidden.png", "invalid"),
    ("notes.txt", "not_image"),
])
def test_bad_upload_paths_are_refused(web, path, code):
    resp = _put(web, path, _png())
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == code


def test_a_file_that_is_not_an_image_is_refused(web):
    resp = _put(web, "sunset.png", b"this is not a png")
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "not_image"
    assert not any(web.ctx.workspace().products.iterdir())


def test_an_oversized_design_is_refused(web, monkeypatch):
    monkeypatch.setattr(designs, "MAX_UPLOAD", 100)
    resp = _put(web, "sunset.png", _png())
    assert resp.status_code == 413 and resp.json()["error"]["code"] == "too_large"


def test_a_preview_named_file_is_saved_but_flagged(web):
    data = _put(web, "sunset-preview.png", _png()).json()
    assert data["ignored"] == "preview_name"


def test_removing_a_design_moves_it_to_the_archive(web):
    ws = web.ctx.workspace()
    _put(web, "sunset.png", _png())
    (ws.archive / "sunset.png").write_bytes(b"older")
    resp = web.client.request("DELETE", "/api/designs/files", params={"path": "sunset.png"})
    assert resp.status_code == 200
    assert resp.json()["archived"] == "archive/sunset-2.png"
    assert not (ws.products / "sunset.png").exists()
    assert (ws.archive / "sunset-2.png").read_bytes() == _png()
    missing = web.client.request("DELETE", "/api/designs/files", params={"path": "nope.png"})
    assert missing.status_code == 404


# --- the run ------------------------------------------------------------------------------------


def test_a_run_creates_drafts_and_reports_every_step(web, fast_images):
    fake, ws = _setup_shop(web)
    _put(web, "retro-mountain-sunset.png", _png())
    _put(web, "but-first-coffee.png", _png((90, 60, 30)))
    _put(web, "IMG_2043.png", _png((10, 200, 30)))

    def trigger():
        resp = web.client.post("/api/designs/start", json={})
        assert resp.status_code == 200, resp.text

    events = read_events(
        web,
        lambda evs: any(t == "job" and d["kind"] == "designs" and d["status"] == "done"
                        for t, d in evs),
        after_connect=trigger,
        timeout=20,
    )
    job_id = next(d["id"] for t, d in events if t == "job" and d["kind"] == "designs")
    final = wait_for_job(web, job_id)
    assert final["status"] == "done"
    result = final["result"]
    assert result["created"] == 2 and result["errors"] == 1 and result["total"] == 3
    state = final["state"]
    by_name = {item["name"]: item for item in state["items"]}
    good = by_name["retro-mountain-sunset.png"]
    assert good["status"] == "ok" and good["listing_id"] >= 2000001
    assert good["steps"] == {s: "done" for s in ("mockup", "research", "title", "tags",
                                                  "check", "draft")}
    assert good["title"].startswith("Retro Mountain Sunset") and len(good["tags"]) == 13
    assert len(good["images"]) == 3 and good["images"][0].startswith("3-DRAFTS/")
    junk = by_name["IMG_2043.png"]
    assert junk["status"] == "error" and junk["error"]["code"] == "junk_name"

    item_events = [d for t, d in events if t == "job-event" and d["type"] == "item"]
    assert item_events and all(d["kind"] == "designs" for d in item_events)
    assert any(d["type"] == "batch" for t, d in events if t == "job-event")

    # Never a state on a create: drafts only.
    assert len(fake.created) == 2
    assert all("state" not in form and "listing_id" not in form for form in fake.created)
    uploads = [call for call in fake.calls if call[0] == "POST" and call[1].endswith("/images")]
    assert len(uploads) == 6

    history = json.loads((ws.root / "upload-history.json").read_text(encoding="utf-8"))
    assert {k: v["status"] for k, v in history[SHOP].items()} == {
        "but-first-coffee.png": "ok", "retro-mountain-sunset.png": "ok"}
    last = web.client.get("/api/designs/last").json()["run"]
    assert last["summary"]["created"] == 2 and len(last["items"]) == 3
    notes = web.client.get("/api/notifications").json()["items"]
    assert notes[0]["ns"] == "designs" and notes[0]["key"] == "notify.done_errors"

    pending = web.client.get("/api/designs/pending").json()
    assert [item["name"] for item in pending["items"]] == ["IMG_2043.png"]


def test_a_second_start_while_one_runs_is_refused(web, fast_images):
    _setup_shop(web)
    _put(web, "retro-mountain-sunset.png", _png())
    release = threading.Event()
    blocker = web.ctx.jobs.start("designs", "designs:job.title", lambda job: release.wait(5))
    try:
        resp = web.client.post("/api/designs/start", json={})
        assert resp.status_code == 409 and resp.json()["error"]["code"] == "busy"
        assert "running" in web.client.get("/api/designs/pending").json()["blockers"]
        removal = web.client.request("DELETE", "/api/designs/files",
                                     params={"path": "retro-mountain-sunset.png"})
        assert removal.status_code == 409
    finally:
        release.set()
        blocker.wait(5)


def test_stopping_finishes_the_draft_in_flight_and_keeps_the_results(web, fast_images):
    fake, ws = _setup_shop(web)
    for name in ("a-retro-sunset.png", "b-ocean-waves.png", "c-cat-mom-club.png"):
        _put(web, name, _png())
    creating = threading.Event()
    go_on = threading.Event()
    ids = iter(range(2000001, 2000100))

    def slow_create(request):
        creating.set()
        go_on.wait(5)
        return {"listing_id": next(ids)}

    fake.add("POST", f"/shops/{ETSY_SHOP_ID}/listings", slow_create)
    job = web.client.post("/api/designs/start", json={}).json()
    assert creating.wait(10)
    cancel = web.client.post(f"/api/jobs/{job['id']}/cancel")
    assert cancel.status_code == 200
    go_on.set()
    final = wait_for_job(web, job["id"], timeout=15)
    assert final["status"] == "cancelled"
    statuses = [item["status"] for item in final["state"]["items"]]
    assert statuses == ["ok", "cancelled", "cancelled"]
    assert final["state"]["result"]["cancelled"] is True
    assert json.loads((ws.drafts / "last-run.json").read_text(encoding="utf-8"))["summary"][
        "created"] == 1


def test_a_dry_run_needs_no_connection_and_sends_nothing(web, fast_images):
    fake, ws = _setup_shop(web, connected=False)
    _put(web, "retro-mountain-sunset.png", _png())
    assert web.client.get("/api/designs/pending").json()["blockers"] == ["connect"]
    job = web.client.post("/api/designs/start", json={"dry_run": True}).json()
    final = wait_for_job(web, job["id"], timeout=15)
    assert final["status"] == "done"
    item = final["state"]["items"][0]
    assert item["status"] == "checked" and item["steps"]["draft"] == "todo"
    assert not any(method == "POST" for method, _path in fake.calls)
    assert not (ws.root / "upload-history.json").exists()
    assert final["result"]["dry_run"] is True and final["result"]["csv"].endswith("review.csv")


def test_a_stale_lock_is_a_blocker_the_seller_can_remove(web):
    _fake, ws = _setup_shop(web)
    _put(web, "ocean-waves.png", _png())
    (ws.root / ".auto-upload.lock").write_text(str(_ended_pid()))
    pending = web.client.get("/api/designs/pending").json()
    assert pending["blockers"] == ["locked"] and pending["locked_at"]
    resp = web.client.post("/api/designs/unlock", json={})
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "confirm_required"
    assert web.client.post("/api/designs/unlock", json={"confirm": True}).status_code == 200
    assert not (ws.root / ".auto-upload.lock").exists()
    assert web.client.get("/api/designs/pending").json()["blockers"] == []


# --- recovery ---------------------------------------------------------------------------------------


def test_an_uncertain_attempt_can_be_retried_after_the_seller_confirms(web):
    _fake, ws = _setup_shop(web)
    _put(web, "retro-mountain-sunset.png", _png())
    _put(web, "ocean-waves.png", _png())
    (ws.root / "upload-history.json").write_text(json.dumps({SHOP: {
        "retro-mountain-sunset.png": {"status": "pending", "listing_id": None},
        "ocean-waves.png": {"status": "ok", "listing_id": 2000009},
    }}), encoding="utf-8")
    pending = web.client.get("/api/designs/pending").json()
    assert pending["items"] == [] and pending["already_done"] == 1
    assert pending["review"] == [{"name": "retro-mountain-sunset.png", "status": "pending",
                                  "listing_id": None, "problem": "uncertain", "message": ""}]

    unconfirmed = web.client.post("/api/designs/review/forget",
                                  json={"name": "retro-mountain-sunset.png"})
    assert unconfirmed.status_code == 400
    drafted = web.client.post("/api/designs/review/forget",
                              json={"name": "ocean-waves.png", "confirm": True})
    assert drafted.status_code == 409 and drafted.json()["error"]["code"] == "already_drafted"
    ok = web.client.post("/api/designs/review/forget",
                         json={"name": "retro-mountain-sunset.png", "confirm": True})
    assert ok.status_code == 200
    names = [item["name"] for item in web.client.get("/api/designs/pending").json()["items"]]
    assert names == ["retro-mountain-sunset.png"]


def test_the_last_run_is_empty_before_the_first(web):
    assert web.client.get("/api/designs/last").json() == {"run": None}


def test_status_counts_pending_designs_after_an_upload(web):
    _setup_shop(web)
    _put(web, "retro-mountain-sunset.png", _png())
    deadline = time.monotonic() + 5
    status = web.ctx.refresh_status(force=True)
    while status["setup"]["designs_pending"] != 1 and time.monotonic() < deadline:
        status = web.ctx.refresh_status(force=True)
    assert status["setup"]["designs_pending"] == 1


def test_a_template_listing_gone_from_etsy_fails_the_run_cleanly(web, fast_images):
    fake, ws = _setup_shop(web)
    _put(web, "retro-mountain-sunset.png", _png())
    fake.error("GET", f"/listings/{TEMPLATE_ID}/inventory", 404, "Listing not found")
    job = web.client.post("/api/designs/start", json={}).json()
    final = wait_for_job(web, job["id"], timeout=15)
    assert final["status"] == "error"
    assert final["error"]["code"] == "template_gone"
    assert final["state"]["items"] == [], "nothing was planned, so nothing is shown as stopped"
    assert not fake.created
    notes = web.client.get("/api/notifications").json()["items"]
    assert notes[0]["key"] == "notify.failed" and notes[0]["params"] == {"code": "template_gone"}


def test_a_run_that_breaks_off_leaves_no_product_waiting(web, fast_images, monkeypatch):
    from etsyotomasyon.drop import automation
    from etsyotomasyon.errors import ValidationError

    _fake, _ws = _setup_shop(web)
    _put(web, "a-retro-sunset.png", _png())
    _put(web, "b-ocean-waves.png", _png())
    real = automation.save_history
    calls = {"n": 0}

    def flaky(path, state):
        calls["n"] += 1
        if calls["n"] > 3:
            raise ValidationError("Cannot save upload history; stopped to avoid duplicates.")
        return real(path, state)

    monkeypatch.setattr(automation, "save_history", flaky)
    job = web.client.post("/api/designs/start", json={}).json()
    final = wait_for_job(web, job["id"], timeout=15)
    assert final["status"] == "error" and final["error"]["code"] == "invalid"
    statuses = [item["status"] for item in final["state"]["items"]]
    assert "queued" not in statuses and "running" not in statuses and "waiting" not in statuses


# --- mockups the run uses (FIXLIST 9, 10) and the pixel limit (design-pixel-bomb-500) --------


def test_pending_counts_mockups_by_the_mockuplar_rule(web):
    from etsyotomasyon.drop import catalog

    _fake, ws = _setup_shop(web, mockups=0)
    for n in range(21):
        (ws.mockups / f"tee-{n:02d}.jpg").write_bytes(_jpg())
    catalog.update(ws, "tee-00.jpg", enabled=False)
    catalog.arrange(ws, order=["tee-20.jpg"])
    mockups = web.client.get("/api/designs/pending").json()["mockups"]
    # 20 switched on, 19 fit in a listing next to the flat design; the chip says 19.
    assert mockups["enabled"] == 19 and mockups["used"] == 19
    assert mockups["switched_on"] == 20 and mockups["over_limit"] == 1
    assert mockups["max"] == 19 and mockups["total"] == 21
    assert mockups["main"] == "tee-20.jpg"
    assert mockups["main"] == catalog.enabled_mockups(ws)[0].name


def test_a_run_uses_the_saved_mockup_order_first_is_the_main_image(web, fast_images):
    from etsyotomasyon.drop import catalog

    _fake, ws = _setup_shop(web, mockups=3)
    catalog.arrange(ws, order=["tote-natural.jpg", "mug-white.jpg", "tshirt-white.jpg"],
                    enabled=["tote-natural.jpg", "mug-white.jpg"])
    _put(web, "retro-mountain-sunset.png", _png())
    job = web.client.post("/api/designs/start", json={}).json()
    final = wait_for_job(web, job["id"], timeout=20)
    assert final["status"] == "done", final
    images = final["state"]["items"][0]["images"]
    assert [path.rsplit("--", 1)[1] for path in images] == [
        "tote-natural.jpg", "mug-white.jpg", "flat.jpg"]


def _huge_png(size) -> bytes:
    # 1-bit: a small file that is tens of millions of pixels once decoded.
    buffer = io.BytesIO()
    Image.new("1", size).save(buffer, "PNG")
    return buffer.getvalue()


@pytest.mark.parametrize("size", [(8000, 8000), (12001, 100)])
def test_a_design_with_too_many_pixels_is_refused(web, size):
    resp = _put(web, "bigdesign.png", _huge_png(size))
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "too_many_pixels"
    assert error["params"]["name"] == "bigdesign.png"
    assert (error["params"]["width"], error["params"]["height"]) == size
    assert error["params"]["max_edge"] == 12000 and error["params"]["max_mp"] == 60
    assert not any(web.ctx.workspace().products.iterdir())


def test_a_design_pillow_itself_refuses_is_too_many_pixels(web, monkeypatch):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
    resp = _put(web, "sunset.png", _png())
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "too_many_pixels"


def test_a_large_but_sane_design_is_accepted(web):
    resp = _put(web, "poster.png", _huge_png((7000, 8000)))
    assert resp.status_code == 200, resp.text


@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
def test_a_huge_design_already_in_the_folder_gets_422_thumbnails(web):
    # The review's reproduction: 13000x13000 answered 500 internal (MemoryError) before.
    ws = web.ctx.workspace()
    (ws.products / "bigdesign.png").write_bytes(_huge_png((13000, 13000)))
    resp = web.client.get("/api/files/thumb", params={"path": "2-PRODUCTS/bigdesign.png",
                                                      "w": 400})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "too_many_pixels"


# --- dropping a folder again (folder-redrop-duplicate) -----------------------------------------


def test_dropping_the_same_folder_again_is_the_same_product(web):
    ws = web.ctx.workspace()
    for name, colour in (("01-front.jpg", (1, 1, 1)), ("02-back.jpg", (2, 2, 2))):
        _put(web, f"cozy ceramic mug/{name}", _jpg(colour), batch="b1")
    again = [_put(web, f"cozy ceramic mug/{name}", _jpg(colour), batch="b2").json()
             for name, colour in (("01-front.jpg", (1, 1, 1)), ("02-back.jpg", (2, 2, 2)))]
    assert [(a["name"], a["duplicate"], a["known"]) for a in again] == [
        ("cozy ceramic mug", True, False), ("cozy ceramic mug", True, False)]
    assert sorted(p.name for p in ws.products.iterdir()) == ["cozy ceramic mug"]
    pending = web.client.get("/api/designs/pending").json()
    assert [i["name"] for i in pending["items"]] == ["cozy ceramic mug"]
    assert pending["items"][0]["files"] == 2


def test_an_interrupted_folder_upload_is_finished_by_dropping_it_again(web):
    ws = web.ctx.workspace()
    _put(web, "cozy ceramic mug/01-front.jpg", _jpg((1, 1, 1)), batch="b1")  # then cut off
    first = _put(web, "cozy ceramic mug/01-front.jpg", _jpg((1, 1, 1)), batch="b2").json()
    rest = _put(web, "cozy ceramic mug/02-back.jpg", _jpg((2, 2, 2)), batch="b2").json()
    assert first["duplicate"] is True and rest["duplicate"] is False
    assert rest["name"] == "cozy ceramic mug" and rest["path"].endswith("cozy ceramic mug/02-back.jpg")
    assert sorted(p.name for p in (ws.products / "cozy ceramic mug").iterdir()) == [
        "01-front.jpg", "02-back.jpg"]


def test_a_folder_that_became_a_draft_takes_no_new_photos(web):
    _setup_shop(web)
    ws = web.ctx.workspace()
    _put(web, "cozy ceramic mug/01-front.jpg", _jpg((1, 1, 1)), batch="b1")
    (ws.root / "upload-history.json").write_text(json.dumps({SHOP: {
        "cozy ceramic mug": {"status": "ok", "listing_id": 2000001}}}), encoding="utf-8")
    same = _put(web, "cozy ceramic mug/01-front.jpg", _jpg((1, 1, 1)), batch="b2").json()
    new = _put(web, "cozy ceramic mug/02-back.jpg", _jpg((2, 2, 2)), batch="b2").json()
    assert (same["duplicate"], same["known"]) == (True, True)
    assert (new["duplicate"], new["known"], new["name"]) == (False, True, "cozy ceramic mug")
    assert sorted(p.name for p in ws.products.iterdir()) == ["cozy ceramic mug"]
    assert [p.name for p in (ws.products / "cozy ceramic mug").iterdir()] == ["01-front.jpg"]


# --- the review list and forgetting (forget-allows-known-draft, review-raw-english) -------------


def _history_with_every_kind(ws):
    (ws.root / "upload-history.json").write_text(json.dumps({SHOP: {
        "a-uncertain.png": {"status": "pending", "listing_id": None},
        "b-lost.png": {"status": "error", "listing_id": None,
                       "message": "network error: read timeout — the request may still have "
                                  "been accepted by Etsy."},
        "c-partial.png": {"status": "partial", "listing_id": 2000003,
                          "message": "created as draft (id 2000003), but image 3 of 7 failed"},
        "d-drafted.png": {"status": "pending", "listing_id": 2000004},
    }}), encoding="utf-8")


def test_review_rows_say_what_happened_as_a_code(web):
    _setup_shop(web)
    ws = web.ctx.workspace()
    for name in ("a-uncertain.png", "b-lost.png", "c-partial.png", "d-drafted.png"):
        _put(web, name, _png())
    _history_with_every_kind(ws)
    review = web.client.get("/api/designs/pending").json()["review"]
    assert {r["name"]: r["problem"] for r in review} == {
        "a-uncertain.png": "uncertain", "b-lost.png": "uncertain",
        "c-partial.png": "partial", "d-drafted.png": "drafted"}
    assert next(r for r in review if r["name"] == "b-lost.png")["message"].startswith("network")


@pytest.mark.parametrize("name", ["c-partial.png", "d-drafted.png"])
def test_a_design_whose_draft_exists_is_never_forgotten(web, name):
    # A crash between the create and the last image leaves "pending" with an id: the
    # draft exists, and trying again would make a second one.
    _setup_shop(web)
    ws = web.ctx.workspace()
    _put(web, name, _png())
    _history_with_every_kind(ws)
    resp = web.client.post("/api/designs/review/forget", json={"name": name, "confirm": True})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "already_drafted"
    assert resp.json()["error"]["params"]["listing_id"] in (2000003, 2000004)
    history = json.loads((ws.root / "upload-history.json").read_text(encoding="utf-8"))
    assert name in history[SHOP]


# --- the lock (lock-removed-midrun) ------------------------------------------------------------


def _ended_pid() -> int:
    ended = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                           capture_output=True, text=True, check=True)
    return int(ended.stdout)


def test_unlock_is_refused_while_a_run_is_active(web):
    _setup_shop(web)
    release = threading.Event()
    blocker = web.ctx.jobs.start("designs", "designs:job.title", lambda job: release.wait(5))
    try:
        resp = web.client.post("/api/designs/unlock", json={"confirm": True})
        assert resp.status_code == 409 and resp.json()["error"]["code"] == "busy"
    finally:
        release.set()
        blocker.wait(5)


def test_a_lock_whose_process_still_runs_needs_the_seller_to_insist(web):
    _setup_shop(web)
    ws = web.ctx.workspace()
    _put(web, "ocean-waves.png", _png())
    (ws.root / ".auto-upload.lock").write_text(f"{os.getpid()}\n", encoding="utf-8")
    pending = web.client.get("/api/designs/pending").json()
    assert pending["blockers"] == ["locked"]
    assert pending["lock"] == {"pid": os.getpid(), "alive": True, "stale": False}
    refused = web.client.post("/api/designs/unlock", json={"confirm": True})
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "lock_active"
    assert refused.json()["error"]["params"]["pid"] == os.getpid()
    assert (ws.root / ".auto-upload.lock").exists()
    forced = web.client.post("/api/designs/unlock", json={"confirm": True, "force": True})
    assert forced.status_code == 200 and not (ws.root / ".auto-upload.lock").exists()


def test_a_crashed_runs_lock_is_reported_stale(web):
    _setup_shop(web)
    ws = web.ctx.workspace()
    (ws.root / ".auto-upload.lock").write_text(str(_ended_pid()), encoding="utf-8")
    pending = web.client.get("/api/designs/pending").json()
    assert pending["lock"]["stale"] is True and pending["lock"]["alive"] is False
    assert web.client.post("/api/designs/unlock", json={"confirm": True}).status_code == 200


def test_a_run_whose_lock_is_removed_midway_finishes_and_is_saved(web, fast_images):
    fake, ws = _setup_shop(web)
    _put(web, "retro-mountain-sunset.png", _png())
    ids = iter(range(2000001, 2000100))

    def create_and_unlock(request):
        lock = ws.root / ".auto-upload.lock"
        if lock.exists():
            lock.unlink()
        return {"listing_id": next(ids)}

    fake.add("POST", f"/shops/{ETSY_SHOP_ID}/listings", create_and_unlock)
    job = web.client.post("/api/designs/start", json={}).json()
    final = wait_for_job(web, job["id"], timeout=20)
    assert final["status"] == "done", final
    assert final["result"]["created"] == 1
    last = web.client.get("/api/designs/last").json()["run"]
    assert last["job_id"] == job["id"] and last["summary"]["created"] == 1


# --- uploads a draft could never use (oversize-or-truncated-accepted) ----------------------------


@pytest.mark.parametrize("name, fmt", [("cozy-cabin-photo.jpg", "JPEG"),
                                       ("cozy-cabin-art.png", "PNG")])
def test_a_file_cut_short_is_refused_at_upload(web, name, fmt):
    buffer = io.BytesIO()
    Image.effect_noise((300, 300), 60).convert("RGB").save(buffer, fmt)
    whole = buffer.getvalue()
    resp = _put(web, name, whole[: len(whole) // 2])
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "not_image"
    assert not any(web.ctx.workspace().products.iterdir())
    assert _put(web, name, whole).status_code == 200


# --- the request estimate (estimate-undercount) --------------------------------------------------


def test_the_estimate_counts_only_what_will_run_with_its_real_images(web):
    _setup_shop(web)  # 2 mockups: a design is 3 images
    ws = web.ctx.workspace()
    _put(web, "retro-mountain-sunset.png", _png())
    _put(web, "ocean-waves-photo.jpg", _photo_jpg())
    _put(web, "IMG_2043.png", _png((10, 200, 30)))  # junk: never runs
    for name in ("01.jpg", "02.jpg", "03.jpg", "04.jpg"):
        _put(web, f"desert cactus print/{name}", _jpg((int(name[1]), 0, 0)), batch="b1")
    data = web.client.get("/api/designs/pending").json()
    # 3 products and 3 concepts: 6 research pages, 3 creates, 3 + 1 + 4 images, and
    # (variations unknown for this template) 3 inventory updates + the template's read.
    assert data["estimate_requests"] == 6 + 3 + 8 + 3 + 1
    template = json.loads(ws.template_path.read_text(encoding="utf-8"))
    template["has_variations"] = False
    ws.write_template(template)
    data = web.client.get("/api/designs/pending").json()
    assert data["template"]["has_variations"] is False
    assert data["estimate_requests"] == 6 + 3 + 8 + 1


# --- the job state a page reloads from (restore-stale-rows) --------------------------------------


class _FakeJob:
    def __init__(self):
        self.states: list[dict] = []
        self.cancelled = False

    def set_state(self, **state):
        self.states.append(state)

    def progress(self, *args, **kwargs):
        pass

    def emit(self, *args, **kwargs):
        pass


def test_a_products_outcome_reaches_the_job_state_at_once():
    job = _FakeJob()
    tracker = designs._Tracker(job, dry_run=False, template=None, mockups={})
    tracker.on_event("", "batch", "running", {"index": -1, "items": [
        {"index": 0, "name": "a.png"}, {"index": 1, "name": "b.png"}]})
    tracker.on_event("a.png", "draft", "running", {"index": 0, "images_uploaded": 3,
                                                    "images_total": 7})
    tracker.on_event("a.png", "item", "ok", {"index": 0, "listing_id": 2000001,
                                             "flat": "3-DRAFTS/b/a--flat.jpg"})
    last = job.states[-1]["items"][0]
    assert last["status"] == "ok" and last["listing_id"] == 2000001
    assert last["flat"] == "3-DRAFTS/b/a--flat.jpg"


def test_the_run_state_marks_the_flat_design(web, fast_images):
    _fake, _ws = _setup_shop(web)
    _put(web, "retro-mountain-sunset.png", _png())
    job = web.client.post("/api/designs/start", json={}).json()
    final = wait_for_job(web, job["id"], timeout=20)
    item = final["state"]["items"][0]
    assert item["flat"].endswith("--flat.jpg") and item["flat"] == item["images"][-1]
    assert len(item["images"]) == 3  # two mockups and the flat design


# --- digital templates (type download / both) -----------------------------------------------


def _digital_template(ws, listing_type="download"):
    # Not made to order: a made-to-order digital draft needs no download file.
    fields = {"taxonomy_id": 2078, "price": 4.5, "quantity": 999, "who_made": "i_did",
              "when_made": "2020_2026", "type": listing_type}
    if listing_type == "both":
        fields["shipping_profile_id"] = 5551
    ws.write_template(Template(TEMPLATE_ID, source_title="Boho Planner Printable",
                               fields=fields, description="Printable PDF planner.",
                               tags=["printable planner"]).to_dict())


def _file_routes(fake, fail_listing=None):
    """uploadListingFile for the listings the fake creates: {listing id: [(name, rank)]}."""
    received: dict[int, list[tuple[str, str, str]]] = {}

    def upload(listing_id):
        def respond(request: httpx.Request):
            if listing_id == fail_listing:
                return httpx.Response(400, json={"error": "The file could not be processed"})
            body = request.content
            name = body.split(b'name="name"\r\n\r\n', 1)[1].split(b"\r\n", 1)[0].decode()
            rank = body.split(b'name="rank"\r\n\r\n', 1)[1].split(b"\r\n", 1)[0].decode()
            filename = body.split(b'name="file"; filename="', 1)[1].split(b'"', 1)[0].decode()
            received.setdefault(listing_id, []).append((filename, name, rank))
            return httpx.Response(201, json={"listing_file_id": 9000 + len(received),
                                             "listing_id": listing_id, "rank": int(rank),
                                             "filename": name})
        return respond

    for listing_id in range(2000001, 2000020):
        fake.add("POST", f"/shops/{ETSY_SHOP_ID}/listings/{listing_id}/files", upload(listing_id))
    return received


def _run_to_the_end(web):
    job = web.client.post("/api/designs/start", json={})
    assert job.status_code == 200, job.text
    final = wait_for_job(web, job.json()["id"], timeout=30)
    assert final["status"] == "done", final
    return final


def test_the_pending_view_of_a_digital_template(web, fast_images):
    _fake, ws = _setup_shop(web)
    _digital_template(ws)
    _put(web, "retro-mountain-sunset.png", _png())
    _put(web, "boho planner/01-front.jpg", _jpg(), batch="b1")
    assert _put(web, "boho planner/dosyalar/planner.pdf", b"%PDF-1.4 planner",
                batch="b1").status_code == 200
    _put(web, "sunset poster set/01-front.jpg", _jpg((9, 9, 9)), batch="b2")
    data = web.client.get("/api/designs/pending").json()

    assert data["listing_type"] == "download"
    template = data["template"]
    assert template["listing_type"] == "download" and template["digital"] is True
    assert template["needs_shipping"] is False and template["shipping_profile"] is False
    assert "no_shipping_profile" not in data["warnings"], "a download needs no profile"
    by_name = {item["name"]: item for item in data["items"]}
    assert by_name["retro-mountain-sunset.png"]["deliverables"] == [{
        "name": "retro-mountain-sunset.png", "path": "2-PRODUCTS/retro-mountain-sunset.png",
        "size": len(_png())}]
    assert [d["path"] for d in by_name["boho planner"]["deliverables"]] == [
        "2-PRODUCTS/boho planner/dosyalar/planner.pdf"]
    assert by_name["boho planner"]["deliverable_problem"] is None
    missing = by_name["sunset poster set"]
    assert missing["deliverables"] == []
    assert missing["deliverable_problem"] == {
        "code": "no_deliverable",
        "params": {"name": "sunset poster set", "folder": "dosyalar", "missing": True}}
    assert data["runnable"] == 2 and "deliverables" in data["warnings"]
    assert data["files_total"] == 2
    # A download's photos show no physical product: the T-shirt and mug mockups are left
    # out, so the loose design gets its flat preview only.
    assert data["mockups"]["names"] == [] and data["mockups"]["main"] is None
    assert data["mockups"]["left_out"] == [{"name": "mug-white.jpg", "type": "mug"},
                                           {"name": "tshirt-white.jpg", "type": "tshirt"}]
    # 2 products, 2 concepts: 4 research pages, 2 creates, 1 + 1 images, 2 download files,
    # 2 inventory updates (variations unknown) and the template's inventory.
    assert data["estimate_requests"] == 4 + 2 + 2 + 2 + 2 + 1


def test_a_physical_template_attaches_nothing_and_still_wants_a_profile(web):
    _fake, ws = _setup_shop(web)
    data = json.loads(ws.template_path.read_text(encoding="utf-8"))
    del data["fields"]["shipping_profile_id"]
    ws.write_template(data)
    _put(web, "retro-mountain-sunset.png", _png())
    pending = web.client.get("/api/designs/pending").json()
    assert pending["template"]["listing_type"] == "physical"
    assert pending["template"]["digital"] is False
    assert "no_shipping_profile" in pending["warnings"]
    assert pending["items"][0]["deliverables"] == [] and pending["files_total"] == 0


def test_download_files_of_a_dropped_folder_land_in_its_dosyalar(web):
    ws = web.ctx.workspace()
    photo = _put(web, "boho planner/01-front.jpg", _jpg(), batch="b1").json()
    first = _put(web, "boho planner/Dosyalar/planner.pdf", b"%PDF-1", batch="b1").json()
    assert first == {"name": "boho planner", "file": "planner.pdf", "folder": "boho planner",
                     "path": "2-PRODUCTS/boho planner/Dosyalar/planner.pdf",
                     "duplicate": False, "known": False, "ignored": None, "size": 6,
                     "deliverable": True}
    assert photo["name"] == first["name"]
    same = _put(web, "boho planner/dosyalar/planner.pdf", b"%PDF-1", batch="b1").json()
    assert same["duplicate"] is True, "the existing Dosyalar folder is used, any case"
    assert sorted(p.name for p in (ws.products / "boho planner").iterdir()) == [
        "01-front.jpg", "Dosyalar"]
    # The same folder dropped again later is the same product, whichever file comes first.
    again = _put(web, "boho planner/dosyalar/planner.pdf", b"%PDF-1", batch="b2").json()
    assert again["name"] == "boho planner" and again["duplicate"] is True


@pytest.mark.parametrize("path, code", [
    ("boho planner/dosyalar/setup.exe", "not_deliverable"),
    ("boho planner/dosyalar/README", "not_deliverable"),
    ("boho planner/extras/planner.pdf", "invalid"),
    ("a/b/c/planner.pdf", "invalid"),
    ("planner.pdf", "not_image"),
])
def test_a_download_file_is_only_taken_where_it_belongs(web, path, code):
    resp = _put(web, path, b"%PDF-1", batch="b1")
    assert resp.status_code == 422 and resp.json()["error"]["code"] == code


def test_a_digital_run_attaches_each_products_download_after_its_images(web, fast_images):
    fake, ws = _setup_shop(web)
    _digital_template(ws)
    received = _file_routes(fake)
    _put(web, "retro-mountain-sunset.png", _png())
    _put(web, "boho planner/01-front.jpg", _jpg(), batch="b1")
    _put(web, "boho planner/dosyalar/2-planner.pdf", b"%PDF-1.4 planner", batch="b1")
    _put(web, "boho planner/dosyalar/10-extras.zip", b"PK\x03\x04", batch="b1")
    _put(web, "sunset poster set/01-front.jpg", _jpg((9, 9, 9)), batch="b2")
    final = _run_to_the_end(web)

    result = final["result"]
    assert result["listing_type"] == "download" and result["files_uploaded"] == 3
    assert result["created"] == 2 and result["errors"] == 1
    by_name = {item["name"]: item for item in final["state"]["items"]}
    loose = by_name["retro-mountain-sunset.png"]
    assert loose["status"] == "ok" and loose["deliverables"] == [
        "2-PRODUCTS/retro-mountain-sunset.png"]
    assert loose["files_uploaded"] == 1 and loose["files_total"] == 1
    folder = by_name["boho planner"]
    assert folder["files_uploaded"] == 2 and folder["files_total"] == 2
    missing = by_name["sunset poster set"]
    assert missing["status"] == "error" and missing["error"]["code"] == "no_deliverable"
    assert missing["steps"]["check"] == "error" and missing["listing_id"] is None

    assert [form["type"] for form in fake.created] == ["download", "download"]
    assert all("shipping_profile_id" not in form for form in fake.created)
    ids = {item["name"]: item["listing_id"] for item in final["state"]["items"]}
    assert received[ids["retro-mountain-sunset.png"]] == [
        ("retro-mountain-sunset.png", "retro-mountain-sunset.png", "1")]
    assert received[ids["boho planner"]] == [("2-planner.pdf", "2-planner.pdf", "1"),
                                             ("10-extras.zip", "10-extras.zip", "2")]
    # Photos first, then the downloads, for each draft.
    posts = [path for method, path in fake.calls if method == "POST"]
    for listing_id in ids.values():
        if listing_id is None:
            continue
        mine = [p.rsplit("/", 1)[1] for p in posts if f"/listings/{listing_id}/" in p]
        assert mine == sorted(mine, key=lambda kind: kind == "files") and "files" in mine
    history = json.loads((ws.root / "upload-history.json").read_text(encoding="utf-8"))
    assert history[SHOP]["boho planner"]["files_uploaded"] == 2
    last = web.client.get("/api/designs/last").json()["run"]
    assert last["summary"]["files_uploaded"] == 3
    assert last["template"]["listing_type"] == "download"


def test_a_download_etsy_refuses_leaves_a_partial_draft(web, fast_images):
    fake, ws = _setup_shop(web)
    _digital_template(ws)
    _file_routes(fake, fail_listing=2000001)
    _put(web, "retro-mountain-sunset.png", _png())
    final = _run_to_the_end(web)
    item = final["state"]["items"][0]
    assert item["status"] == "partial" and item["steps"]["draft"] == "warn"
    assert item["files_uploaded"] == 0 and item["listing_id"] == 2000001
    warning = next(w for w in item["warnings"] if w["code"] == "partial_files")
    assert warning["params"]["name"] == "retro-mountain-sunset.png"
    history = json.loads((ws.root / "upload-history.json").read_text(encoding="utf-8"))
    entry = history[SHOP]["retro-mountain-sunset.png"]
    assert entry["status"] == "partial" and entry["files_uploaded"] == 0
    review = web.client.get("/api/designs/pending").json()["review"]
    assert review[0]["problem"] == "partial" and review[0]["listing_id"] == 2000001


def test_a_both_template_ships_and_attaches(web, fast_images):
    fake, ws = _setup_shop(web)
    _digital_template(ws, "both")
    received = _file_routes(fake)
    _put(web, "retro-mountain-sunset.png", _png())
    pending = web.client.get("/api/designs/pending").json()
    assert pending["template"]["needs_shipping"] is True
    final = _run_to_the_end(web)
    assert final["state"]["items"][0]["status"] == "ok"
    assert fake.created[0]["type"] == "both" and fake.created[0]["shipping_profile_id"] == "5551"
    assert received[2000001][0][0] == "retro-mountain-sunset.png"


def test_a_digital_run_with_no_usable_download_is_blocked_with_its_own_reason(web):
    _fake, ws = _setup_shop(web)
    _digital_template(ws)
    _put(web, "sunset poster set/01-front.jpg", _jpg(), batch="b1")
    pending = web.client.get("/api/designs/pending").json()
    assert pending["runnable"] == 0 and pending["blockers"] == ["deliverables_only"]
    start = web.client.post("/api/designs/start", json={})
    assert start.status_code == 409
    assert start.json()["error"]["params"]["blockers"] == ["deliverables_only"]
    # A readable design next to it is its own download: the run can start.
    _put(web, "retro-mountain-sunset.png", _png())
    assert web.client.get("/api/designs/pending").json()["blockers"] == []



# --- a product folder with only its downloads (no photos) -----------------------------------------


def test_a_folder_with_only_dosyalar_is_listed_and_fails_the_check(web, fast_images):
    _fake, ws = _setup_shop(web)
    _digital_template(ws)
    assert _put(web, "boho planner/dosyalar/planner.pdf", b"%PDF-1.4 planner",
                batch="b1").status_code == 200
    pending = web.client.get("/api/designs/pending").json()
    [item] = pending["items"]
    assert item["name"] == "boho planner" and item["kind"] == "folder"
    assert item["no_photos"] is True and item["files"] == 0 and item["thumb_path"] == ""
    assert pending["runnable"] == 0 and pending["blockers"] == ["photos_only"]
    assert "no_photos" in pending["warnings"]
    assert web.ctx.refresh_status(force=True)["setup"]["designs_pending"] == 1

    # Beside a design that can run, it goes through the run and stops at the check.
    _put(web, "retro-mountain-sunset.png", _png())
    pending = web.client.get("/api/designs/pending").json()
    assert pending["runnable"] == 1 and pending["blockers"] == []
    _file_routes(_fake)
    final = _run_to_the_end(web)
    items = {i["name"]: i for i in final["state"]["items"]}
    folder = items["boho planner"]
    assert folder["status"] == "error" and folder["steps"]["check"] == "error"
    assert folder["error"]["code"] == "no_photos"
    assert folder["error"]["params"] == {"name": "boho planner", "folder": "dosyalar"}
    assert items["retro-mountain-sunset.png"]["status"] == "ok"


# --- the shop section (the start card's "Mağaza bölümü") -------------------------------------------

SECTIONS_PATH = f"/shops/{ETSY_SHOP_ID}/sections"
TEES, MUGS, GIFTS = 9001, 9002, 9003


def _sections(fake, *records):
    """getShopSections answers with these (id, title, rank) sections."""
    fake.add("GET", SECTIONS_PATH, {"count": len(records), "results": [
        {"shop_section_id": sid, "title": title, "rank": rank, "user_id": 7654321,
         "active_listing_count": 3} for sid, title, rank in records]})


def _template_section(ws, section_id):
    data = json.loads(ws.template_path.read_text(encoding="utf-8"))
    if section_id is None:
        data["fields"].pop("shop_section_id", None)
    else:
        data["fields"]["shop_section_id"] = section_id
    ws.write_template(data)


def _section_run(web, fake, **body):
    fake.created.clear()
    job = web.client.post("/api/designs/start", json=body)
    assert job.status_code == 200, job.text
    final = wait_for_job(web, job.json()["id"], timeout=30)
    assert final["status"] == "done", final
    return final


def _sections_calls(fake):
    return sum(1 for call in fake.calls if call == ("GET", SECTIONS_PATH))


def test_the_start_card_lists_the_shops_sections_and_starts_on_the_templates(web):
    fake, ws = _setup_shop(web)
    _template_section(ws, TEES)
    # Etsy's rank decides the order (Tişörtler first), and its titles arrive escaped.
    _sections(fake, (MUGS, "Mugs &amp; Cups", 2), (TEES, "Tişörtler", 1))
    data = web.client.get("/api/designs/sections").json()
    assert data == {
        "available": True, "error": None,
        "sections": [{"id": TEES, "title": "Tişörtler", "count": 3},
                     {"id": MUGS, "title": "Mugs & Cups", "count": 3}],
        "template": {"id": TEES, "title": "Tişörtler", "missing": False},
        "choice": "template", "remembered": None, "remembered_missing": False,
    }
    # Kept a minute per Etsy client; refresh=1 reads them again.
    web.client.get("/api/designs/sections")
    assert _sections_calls(fake) == 1
    web.client.get("/api/designs/sections", params={"refresh": 1})
    assert _sections_calls(fake) == 2


def test_a_chosen_section_goes_on_every_draft_and_is_remembered(web, fast_images):
    fake, ws = _setup_shop(web)
    _template_section(ws, TEES)
    _sections(fake, (TEES, "Tişörtler", 1), (MUGS, "Kupalar", 2))
    _put(web, "retro-mountain-sunset.png", _png())
    _put(web, "but-first-coffee.png", _png((90, 60, 30)))
    final = _section_run(web, fake, section=MUGS)
    assert [form["shop_section_id"] for form in fake.created] == [str(MUGS)] * 2
    assert final["state"]["section"] == {"choice": MUGS, "id": MUGS, "title": "Kupalar",
                                         "template_gone": None}
    last = web.client.get("/api/designs/last").json()["run"]
    assert last["section"]["id"] == MUGS
    # The template itself is not changed; the choice is the shop's for the next card.
    assert json.loads(ws.template_path.read_text(encoding="utf-8"))["fields"][
        "shop_section_id"] == TEES
    assert web.ctx.shop_prefs()["drop_section"] == {
        "choice": MUGS, "title": "Kupalar", "etsy_shop_id": ETSY_SHOP_ID}
    data = web.client.get("/api/designs/sections").json()
    assert data["choice"] == MUGS and data["remembered"] == {"choice": MUGS, "title": "Kupalar"}


def test_no_section_leaves_it_off_every_draft(web, fast_images):
    fake, ws = _setup_shop(web)
    _template_section(ws, TEES)
    _sections(fake, (TEES, "Tişörtler", 1))
    _put(web, "retro-mountain-sunset.png", _png())
    final = _section_run(web, fake, section="none")
    assert fake.created and all("shop_section_id" not in form for form in fake.created)
    assert final["state"]["section"]["choice"] == "none"
    assert web.client.get("/api/designs/sections").json()["choice"] == "none"


def test_the_templates_section_is_copied_as_before(web, fast_images):
    fake, ws = _setup_shop(web)
    _template_section(ws, TEES)
    _sections(fake, (TEES, "Tişörtler", 1))
    _put(web, "retro-mountain-sunset.png", _png())
    final = _section_run(web, fake, section="template")
    assert fake.created[0]["shop_section_id"] == str(TEES)
    assert final["state"]["section"] == {"choice": "template", "id": TEES,
                                         "title": "Tişörtler", "template_gone": None}
    # A start that names no section (an older page) is exactly the old run: the
    # template's section, not even checked, and nothing remembered.
    _put(web, "but-first-coffee.png", _png((90, 60, 30)))
    calls = _sections_calls(fake)
    final = _section_run(web, fake)
    assert fake.created[0]["shop_section_id"] == str(TEES)
    assert final["state"]["section"] is None and _sections_calls(fake) == calls


def test_a_deleted_template_section_makes_drafts_without_one(web, fast_images):
    fake, ws = _setup_shop(web)
    _template_section(ws, GIFTS)  # deleted on Etsy since the template was picked
    _sections(fake, (TEES, "Tişörtler", 1))
    data = web.client.get("/api/designs/sections").json()
    assert data["template"] == {"id": GIFTS, "title": None, "missing": True}
    _put(web, "retro-mountain-sunset.png", _png())
    final = _section_run(web, fake, section="template")
    assert fake.created and "shop_section_id" not in fake.created[0]
    assert final["state"]["section"] == {"choice": "template", "id": None, "title": None,
                                         "template_gone": GIFTS}
    assert final["state"]["items"][0]["status"] == "ok"


def test_a_section_deleted_before_the_start_is_refused_and_nothing_is_sent(web, fast_images):
    fake, ws = _setup_shop(web)
    _sections(fake, (TEES, "Tişörtler", 1), (MUGS, "Kupalar", 2))
    _put(web, "retro-mountain-sunset.png", _png())
    assert web.client.get("/api/designs/sections").json()["sections"][1]["id"] == MUGS
    _sections(fake, (TEES, "Tişörtler", 1))  # the seller deletes Kupalar on Etsy
    resp = web.client.post("/api/designs/start", json={"section": MUGS})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "section_gone"
    assert resp.json()["error"]["params"] == {"id": MUGS}
    assert not fake.created and web.ctx.jobs.list(kind="designs") == []
    assert "drop_section" not in web.ctx.shop_prefs()


def test_a_remembered_section_that_is_gone_falls_back_to_the_template(web):
    fake, ws = _setup_shop(web)
    web.ctx.update_shop_prefs(drop_section={"choice": MUGS, "title": "Kupalar",
                                            "etsy_shop_id": ETSY_SHOP_ID})
    _sections(fake, (TEES, "Tişörtler", 1))
    data = web.client.get("/api/designs/sections").json()
    assert data["choice"] == "template" and data["remembered_missing"] is True
    assert data["remembered"] == {"choice": MUGS, "title": "Kupalar"}
    # A choice made while the keys signed in to another Etsy shop is not this shop's.
    web.ctx.update_shop_prefs(drop_section={"choice": TEES, "title": "Tişörtler",
                                            "etsy_shop_id": 999})
    data = web.client.get("/api/designs/sections").json()
    assert data["remembered"] is None and data["choice"] == "template"


def test_a_shop_without_sections(web, fast_images):
    fake, ws = _setup_shop(web)
    _sections(fake)
    web.ctx.update_shop_prefs(drop_section={"choice": "none", "title": None,
                                            "etsy_shop_id": ETSY_SHOP_ID})
    data = web.client.get("/api/designs/sections").json()
    assert data["available"] is True and data["sections"] == []
    assert data["template"] == {"id": None, "title": None, "missing": False}
    assert data["choice"] == "template"  # nothing else to offer
    _put(web, "retro-mountain-sunset.png", _png())
    _section_run(web, fake, section="template")
    assert fake.created and "shop_section_id" not in fake.created[0]


def test_sections_that_cannot_be_read_leave_the_template_and_no_section(web):
    fake, ws = _setup_shop(web)
    _template_section(ws, TEES)
    fake.error("GET", SECTIONS_PATH, 400, "Sections are not available")
    web.ctx.update_shop_prefs(drop_section={"choice": "none", "title": None,
                                            "etsy_shop_id": ETSY_SHOP_ID})
    data = web.client.get("/api/designs/sections").json()
    assert data["available"] is False and data["error"] == "etsy_error"
    assert data["sections"] == [] and data["template"]["missing"] is False
    assert data["choice"] == "none"


def test_without_keys_the_sections_say_what_is_missing(web):
    data = web.client.get("/api/designs/sections").json()
    assert data["available"] is False and data["error"] == "setup_needed"
    assert data["choice"] == "template" and data["template"]["id"] is None


@pytest.mark.parametrize("section", ["mugs", 0, -4, True, 1.5, [], {"id": 1}])
def test_the_section_must_be_template_none_or_an_id(web, section):
    _setup_shop(web)
    resp = web.client.post("/api/designs/start", json={"section": section})
    assert resp.status_code == 422
    assert resp.json()["error"]["params"] == {"field": "section"}


def test_a_check_ignores_the_section(web, fast_images):
    fake, ws = _setup_shop(web)
    _sections(fake, (TEES, "Tişörtler", 1))
    _put(web, "retro-mountain-sunset.png", _png())
    job = web.client.post("/api/designs/start", json={"dry_run": True, "section": 424242})
    assert job.status_code == 200, job.text
    final = wait_for_job(web, job.json()["id"], timeout=30)
    assert final["status"] == "done" and final["state"]["section"] is None
    assert _sections_calls(fake) == 0 and "drop_section" not in web.ctx.shop_prefs()


SECTION_KEYS = ("ready.section_label", "ready.section.loading", "ready.section.template",
                "ready.section.template_plain", "ready.section.no_section",
                "ready.section.deleted", "ready.section.none", "ready.section_empty",
                "ready.section_unavailable", "ready.section_template_gone",
                "ready.section_remembered_gone", "errors.section_gone")


def test_the_section_strings_are_there_in_both_languages():
    from test_vp_upload import _strings

    strings = _strings("designs")
    for lang in ("tr", "en"):
        for key in SECTION_KEYS:
            assert strings[lang].get(key), (lang, key)
        assert "{name}" in strings[lang]["ready.section.template"]
        assert "{name}" in strings[lang]["ready.section_remembered_gone"]
    assert strings["tr"]["ready.section_label"] == "Mağaza bölümü"
    assert strings["tr"]["ready.section.template"] == "Şablondaki gibi ({name})"


def test_the_section_select_and_its_notes():
    from test_vp_upload import NODE, _run

    if NODE is None:
        pytest.skip("node is not installed")
    got = _run("designs", """
      const t = (k, p) => (p ? k + ' ' + JSON.stringify(p) : k);
      const base = { available: true, error: null,
        sections: [{ id: 9001, title: 'Tees', count: 3 }, { id: 9002, title: '', count: 1 }],
        template: { id: 9001, title: 'Tees', missing: false }, choice: 'template',
        remembered: null, remembered_missing: false };
      const gone = { ...base, sections: [base.sections[1]], template: { id: 9001, title: null, missing: true },
        remembered: { choice: 9003, title: 'Mugs' }, remembered_missing: true };
      const empty = { ...base, sections: [], template: { id: null, title: null, missing: false } };
      return {
        normal: m.sectionChoices(base, t),
        remembered: m.sectionChoices({ ...base, choice: 9002 }, t).start,
        stale: m.sectionChoices({ ...base, choice: 4242 }, t).start,
        gone: m.sectionChoices(gone, t),
        empty: m.sectionChoices(empty, t),
        unread: m.sectionChoices(null, t),
        unavailable: m.sectionChoices({ ...empty, available: false, error: 'offline', choice: 'none' }, t),
        notes: [m.sectionNotes(base, 'template'), m.sectionNotes(gone, 'template'),
                m.sectionNotes(gone, '9002'), m.sectionNotes(empty, 'template'),
                m.sectionNotes(null, 'template'), m.sectionNotes({ ...gone, sections: [] }, 'template')],
        values: [m.sectionValue('template'), m.sectionValue('none'), m.sectionValue('9002')],
      };""")
    template = 'ready.section.template {"name":"%s"}'
    none = {"value": "none", "label": "ready.section.none"}
    assert got["normal"] == {"options": [
        {"value": "template", "label": template % "Tees"}, none,
        {"value": "9001", "label": "Tees"}, {"value": "9002", "label": "#9002"},
    ], "start": "template"}
    assert got["remembered"] == "9002" and got["stale"] == "template"
    assert got["gone"] == {"options": [
        {"value": "template", "label": template % "ready.section.deleted"}, none,
        {"value": "9002", "label": "#9002"},
    ], "start": "template"}
    # A shop without sections: only the template's (no section), nothing to pick.
    assert got["empty"] == {"options": [
        {"value": "template", "label": template % "ready.section.no_section"}], "start": "template"}
    # Not read: the template's (unnamed) and "no section" can still be picked.
    plain = {"value": "template", "label": "ready.section.template_plain"}
    assert got["unread"] == {"options": [plain, none], "start": "template"}
    assert got["unavailable"] == {"options": [plain, none], "start": "none"}
    assert got["notes"] == [
        [],
        [["warning", "ready.section_remembered_gone", {"name": "Mugs"}],
         ["warning", "ready.section_template_gone"]],
        [],
        [["hint", "ready.section_empty"]],
        [["hint", "ready.section_unavailable"]],
        [["hint", "ready.section_empty"]],
    ]
    assert got["values"] == ["template", "none", 9002]
