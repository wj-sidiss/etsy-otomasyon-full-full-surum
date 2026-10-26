"""drop.stream: the per-product run behind the web UI's Tasarım Yükle screen."""

from __future__ import annotations

import json
import threading
import time

import httpx
import pytest
from PIL import Image

from etsyotomasyon import csvio
from etsyotomasyon.drop import automation, catalog, mockup, stream
from etsyotomasyon.drop.template import Template
from etsyotomasyon.drop.workspace import Workspace
from etsyotomasyon.errors import AuthError, EtsyApiError, ValidationError

SHOP = "123"


def _artwork(path, colour=(200, 60, 40)):
    """A transparent design: a filled square on a see-through ground."""
    image = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    for x in range(10, 30):
        for y in range(10, 30):
            image.putpixel((x, y), (*colour, 255))
    image.save(path)
    return path


def _photo(path, colour=(90, 120, 150)):
    Image.new("RGB", (40, 40), colour).save(path)
    return path


@pytest.fixture
def studio(tmp_path, monkeypatch):
    ws = Workspace(tmp_path / "studio").create()
    _photo(ws.mockups / "tshirt-white.jpg", (240, 240, 240))
    _photo(ws.mockups / "mug-white.jpg", (250, 250, 250))
    template = Template(1000001, fields={
        "taxonomy_id": 1, "price": 21, "quantity": 5, "who_made": "i_did",
        "when_made": "made_to_order", "type": "physical", "shipping_profile_id": 55,
    }, description="Soft cotton tee.", tags=["gift idea", "retro style"])
    ws.write_template(template.to_dict())

    # Compositing at the real 2000 px output is slow and beside the point here.
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
    # These tiny pictures would each get a "may look soft" warning: tests that are about
    # that warning turn it back on.
    monkeypatch.setattr(stream, "SMALL_IMAGE_EDGE", 0)
    monkeypatch.setattr(mockup, "MAX_UPSCALE", float("inf"))
    return ws, template


class Client:
    """A stand-in for EtsyClient with the calls the stream makes."""

    def __init__(self, ws, *, search_delay=0.0):
        self.ws = ws
        self.lock = threading.Lock()
        self.creates: list[dict] = []
        self.images: list[tuple[int, str, int]] = []
        self.alts: dict[str, str] = {}  # image name -> the alt text it was sent with
        self.searches: list[str] = []
        self.search_delay = search_delay
        self.active_searches = 0
        self.max_active_searches = 0
        self.fail_create: dict[str, BaseException] = {}  # title substring -> error
        self.fail_search: BaseException | None = None
        self.files: list[tuple[int, str, int]] = []
        self.fail_file: dict[str, BaseException] = {}  # file name substring -> error
        self.calls: list[tuple[str, int, str]] = []  # images and files, in upload order
        self.next_id = 1000001

    def shop_id(self):
        return int(SHOP)

    def listing_inventory(self, listing_id):
        return {"products": [{"property_values": [], "offerings": [
            {"price": {"amount": 2100, "divisor": 100}, "quantity": 5, "is_enabled": True}]}]}

    def search_active_listings(self, *, keywords, max_items=100, **filters):
        with self.lock:
            self.searches.append(keywords)
            self.active_searches += 1
            self.max_active_searches = max(self.max_active_searches, self.active_searches)
        try:
            if self.search_delay:
                time.sleep(self.search_delay)
            if self.fail_search is not None:
                raise self.fail_search
            words = keywords.split()
            return [
                {"title": f"{keywords} shirt vintage gift {n}",
                 "tags": [f"{words[0]} tee", "vintage gift", f"extra tag {n % 12}"],
                 "price": {"amount": 2000, "divisor": 100, "currency_code": "USD"},
                 "num_favorers": n}
                for n in range(30)
            ]
        finally:
            with self.lock:
                self.active_searches -= 1

    def create_draft_listing(self, fields):
        history = json.loads((self.ws.root / "upload-history.json").read_text(encoding="utf-8"))
        pending = [name for name, entry in history[SHOP].items() if entry["status"] == "pending"]
        assert len(pending) == 1, "intent is saved before the create"
        for needle, error in self.fail_create.items():
            if needle in fields["title"].lower():
                raise error
        with self.lock:
            self.creates.append(dict(fields))
            listing_id = self.next_id
            self.next_id += 1
        return {"listing_id": listing_id}

    def upload_listing_image(self, listing_id, image, *, rank, alt_text=""):
        with self.lock:
            self.images.append((listing_id, image.name, rank))
            self.calls.append(("image", listing_id, image.name))
            self.alts[image.name] = alt_text
        return {}

    def upload_listing_file(self, listing_id, path, *, rank):
        for needle, error in self.fail_file.items():
            if needle in path.name:
                raise error
        with self.lock:
            self.files.append((listing_id, path.name, rank))
            self.calls.append(("file", listing_id, path.name))
        return {"listing_file_id": 7000 + len(self.files), "listing_id": listing_id,
                "rank": rank, "filename": path.name}


class Events:
    def __init__(self):
        self.lock = threading.Lock()
        self.items: list[tuple[str, str, str, dict]] = []

    def __call__(self, name, step, status, data):
        with self.lock:
            self.items.append((name, step, status, data))

    def of(self, name):
        return [(step, status) for n, step, status, _ in self.items if n == name]

    def outcome(self, name):
        return next(data for n, step, status, data in reversed(self.items)
                    if n == name and step == "item" and status != "waiting")


def _run(ws, template, client, **kw):
    kw.setdefault("mockups", catalog.enabled_mockups(ws))
    return stream.run_stream(ws, template, client, **kw)


def _history(ws):
    return json.loads((ws.root / "upload-history.json").read_text(encoding="utf-8"))


def test_one_product_walks_the_six_steps_in_order(studio):
    ws, template = studio
    _artwork(ws.products / "retro-mountain-sunset.png")
    events = Events()
    report = _run(ws, template, Client(ws), on_event=events)

    item = report.items[0]
    assert item.status == stream.OK and item.listing_id == 1000001
    assert item.steps == {step: "done" for step in stream.STEPS}
    finished = [step for step, status in events.of("retro-mountain-sunset.png")
                if status in ("done", "warn")]
    assert finished == list(stream.STEPS)
    starts = [step for step, status in events.of("retro-mountain-sunset.png")
              if status == "running"]
    assert list(dict.fromkeys(starts)) == list(stream.STEPS)
    outcome = events.outcome("retro-mountain-sunset.png")
    assert outcome["listing_id"] == 1000001
    assert outcome["title"].startswith("Retro Mountain Sunset")
    assert len(outcome["tags"]) == 13
    assert all(path.startswith("3-DRAFTS/") for path in outcome["images"])
    batch = next(data for name, step, _s, data in events.items if step == "batch")
    assert [i["name"] for i in batch["items"]] == ["retro-mountain-sunset.png"]
    assert _history(ws)[SHOP]["retro-mountain-sunset.png"]["status"] == "ok"
    assert report.csv_path is not None and report.csv_path.is_file()


def test_image_counts_mockups_plus_flat_opaque_as_is_and_folder_photos(studio):
    ws, template = studio
    _artwork(ws.products / "cat-mom-club.png")
    _photo(ws.products / "ocean-waves-photo.jpg")
    folder = ws.products / "desert cactus print"
    folder.mkdir()
    for name in ("2-back.jpg", "1-front.jpg", "10-detail.jpg"):
        _photo(folder / name)
    client = Client(ws)
    report = _run(ws, template, client)

    by_name = {item.name: item for item in report.items}
    art = by_name["cat-mom-club.png"]
    assert art.mode == "composited" and len(art.images) == 3  # 2 mockups + the flat design
    assert by_name["ocean-waves-photo.jpg"].mode == "as_is"
    assert [p.name for p in by_name["ocean-waves-photo.jpg"].images] == ["ocean-waves-photo.jpg"]
    photos = by_name["desert cactus print"]
    assert photos.mode == "photos"
    assert [p.name for p in photos.images] == ["1-front.jpg", "2-back.jpg", "10-detail.jpg"]
    per_listing: dict[int, int] = {}
    for listing_id, _name, _rank in client.images:
        per_listing[listing_id] = per_listing.get(listing_id, 0) + 1
    assert sorted(per_listing.values()) == [1, 3, 3]


def test_without_the_flat_render_only_the_mockups_go_up(studio):
    ws, template = studio
    _artwork(ws.products / "stay-wild-moon.png")
    report = _run(ws, template, Client(ws), include_flat=False)
    assert len(report.items[0].images) == 2
    assert all("--flat" not in p.name for p in report.items[0].images)


def test_drafts_never_carry_a_state_or_an_id(studio):
    ws, template = studio
    _artwork(ws.products / "but-first-coffee.png")
    client = Client(ws)
    _run(ws, template, client)
    fields = client.creates[0]
    assert "state" not in fields and "listing_id" not in fields
    assert fields["price"] == 21.0 and fields["shipping_profile_id"] == 55


def test_a_draft_keeps_its_design_as_the_listings_print_file(studio):
    ws, template = studio
    design = _artwork(ws.products / "but-first-coffee.png") or ws.products / "but-first-coffee.png"
    report = _run(ws, template, Client(ws))
    item = report.items[0]
    assert item.status == stream.OK
    kept = ws.root / "print-files" / str(item.listing_id) / "but-first-coffee.png"
    assert kept.read_bytes() == design.read_bytes()


def test_research_runs_once_per_concept_and_products_prepare_in_parallel(studio):
    ws, template = studio
    for n in range(4):
        _artwork(ws.products / f"{n + 1:03d}-wildflower-botanical.png")
    for name in ("cat-mom-club.png", "ocean-waves.png", "desert-cactus.png"):
        _artwork(ws.products / name)
    client = Client(ws, search_delay=0.15)
    report = _run(ws, template, client, concurrency=3, use_cache=False)

    assert report.created == 7
    # The template is a tee ("Soft cotton tee."), so each concept is searched as one.
    assert sorted(client.searches) == sorted(
        ["wildflower botanical tee", "cat mom club tee", "ocean waves tee", "desert cactus tee"]
    )
    assert client.max_active_searches >= 2, "different concepts are researched side by side"
    assert report.researched == 4
    listing_ids = [item.listing_id for item in report.items]
    assert listing_ids == sorted(listing_ids), "drafts are created in folder order"


def test_a_junk_file_name_fails_only_that_product(studio):
    ws, template = studio
    _artwork(ws.products / "IMG_2043.png")
    _artwork(ws.products / "retro-mountain-sunset.png")
    events = Events()
    client = Client(ws)
    report = _run(ws, template, client, on_event=events)

    junk = next(item for item in report.items if item.name == "IMG_2043.png")
    assert junk.status == stream.FAILED
    assert junk.error.code == "junk_name" and junk.steps["mockup"] == "error"
    assert junk.steps["draft"] == "todo"
    good = next(item for item in report.items if item.name == "retro-mountain-sunset.png")
    assert good.status == stream.OK
    assert len(client.creates) == 1
    assert events.outcome("IMG_2043.png")["problem"]["code"] == "junk_name"
    assert "IMG_2043.png" not in _history(ws)[SHOP], "never attempted, so still pending"


def test_a_product_that_fails_its_check_does_not_stop_the_batch(studio):
    ws, template = studio
    folder = ws.products / "retro sunset bundle"
    folder.mkdir()
    for n in range(21):
        _photo(folder / f"{n:02d}.jpg")
    _artwork(ws.products / "ocean-waves.png")
    broken = ws.products / "cat-mom-club.jpg"
    buffer = Image.new("RGB", (300, 300), (1, 2, 3))
    buffer.save(broken, "JPEG", quality=95)
    whole = broken.read_bytes()
    broken.write_bytes(whole[: len(whole) // 2])  # a copy cut short

    client = Client(ws)
    report = _run(ws, template, client)
    by_name = {item.name: item for item in report.items}
    assert by_name["retro sunset bundle"].error.code == "too_many_images"
    assert by_name["cat-mom-club.jpg"].error.code == "invalid_image"
    assert by_name["cat-mom-club.jpg"].steps["check"] == "error"
    assert by_name["ocean-waves.png"].status == stream.OK
    assert len(client.creates) == 1


def test_the_history_guards_against_a_second_draft(studio):
    ws, template = studio
    _artwork(ws.products / "retro-mountain-sunset.png")
    client = Client(ws)
    first = _run(ws, template, client)
    assert first.created == 1
    second = _run(ws, template, client)
    assert second.items == []
    assert second.already_done == ["retro-mountain-sunset.png"]
    assert len(client.creates) == 1


def test_an_uncertain_attempt_is_kept_for_review_and_not_retried(studio):
    ws, template = studio
    _artwork(ws.products / "retro-mountain-sunset.png")
    client = Client(ws)
    lost = EtsyApiError(0, "network error: read timed out — the request may still have been "
                           "accepted by Etsy.", method="POST", path="/shops/123/listings")
    client.fail_create["retro"] = lost
    report = _run(ws, template, client)
    item = report.items[0]
    assert item.status == stream.FAILED and item.error.code == "draft_uncertain"
    assert _history(ws)[SHOP]["retro-mountain-sunset.png"]["status"] == "error"
    again = _run(ws, template, client)
    assert again.items == [] and again.needs_review


def test_a_create_etsy_refused_leaves_the_product_free_to_retry(studio):
    ws, template = studio
    _artwork(ws.products / "retro-mountain-sunset.png")
    client = Client(ws)
    client.fail_create["retro"] = EtsyApiError(400, "Invalid taxonomy", method="POST",
                                               path="/shops/123/listings")
    report = _run(ws, template, client)
    assert report.items[0].error.code == "draft_refused"
    assert "retro-mountain-sunset.png" not in _history(ws)[SHOP]
    client.fail_create.clear()
    again = _run(ws, template, client)
    assert again.created == 1


def test_a_connection_that_never_opened_is_a_refusal_too(studio):
    ws, template = studio
    _artwork(ws.products / "retro-mountain-sunset.png")
    _artwork(ws.products / "ocean-waves.png")
    client = Client(ws)
    error = EtsyApiError(0, "network error: offline", method="POST", path="/shops/123/listings")
    error.__cause__ = httpx.ConnectError("offline")
    client.fail_create["ocean"] = error
    report = _run(ws, template, client)
    ocean = next(item for item in report.items if item.name == "ocean-waves.png")
    assert ocean.error.code == "draft_refused"
    assert "ocean-waves.png" not in _history(ws)[SHOP]
    assert report.stopped is not None and report.stopped.code == "offline"
    retro = next(item for item in report.items if item.name == "retro-mountain-sunset.png")
    assert retro.status == stream.CANCELLED
    assert retro.error.code == "stopped" and retro.error.params["reason"] == "offline"


def test_a_lost_sign_in_stops_the_rest_without_marking_them(studio):
    ws, template = studio
    for name in ("a-retro-sunset.png", "b-ocean-waves.png", "c-cat-mom-club.png"):
        _artwork(ws.products / name)
    client = Client(ws)
    client.fail_create["retro"] = AuthError("refresh token revoked")
    report = _run(ws, template, client)
    assert [item.status for item in report.items] == ["error", "cancelled", "cancelled"]
    assert report.stopped.code == "reconnect"
    assert _history(ws)[SHOP] == {}, "nothing reached Etsy, so nothing is recorded"
    assert client.creates == []


def test_repeated_failures_stop_the_batch(studio):
    ws, template = studio
    for n in range(5):
        _artwork(ws.products / f"{n + 1}-retro-design-{'abcde'[n]}.png")
    client = Client(ws)
    client.fail_create["retro"] = EtsyApiError(400, "Shipping profile invalid", method="POST",
                                               path="/shops/123/listings")
    report = _run(ws, template, client)
    statuses = [item.status for item in report.items]
    assert statuses[:3] == ["error"] * 3 and statuses[3:] == ["cancelled"] * 2
    assert report.stopped.code == "repeated_failures"


def test_cancel_finishes_the_draft_in_flight_and_starts_nothing_new(studio):
    ws, template = studio
    for name in ("a-retro-sunset.png", "b-ocean-waves.png", "c-cat-mom-club.png",
                 "d-desert-cactus.png"):
        _artwork(ws.products / name)
    cancel = threading.Event()

    class Cancelling(Client):
        def upload_listing_image(self, listing_id, image, *, rank, alt_text=""):
            cancel.set()  # asked to stop while the first draft is being created
            return super().upload_listing_image(listing_id, image, rank=rank)

    client = Cancelling(ws)
    report = _run(ws, template, client, cancel=cancel)
    assert report.cancelled
    assert report.items[0].status == stream.OK
    assert len({listing_id for listing_id, _n, _r in client.images}) == 1
    assert len(client.images) == 3, "the draft in flight got all its images"
    assert [item.status for item in report.items[1:]] == ["cancelled"] * 3
    assert all(item.error is None for item in report.items[1:])
    assert len(client.creates) == 1


def test_a_research_failure_is_a_warning_not_an_error(studio):
    ws, template = studio
    _artwork(ws.products / "retro-mountain-sunset.png")
    client = Client(ws)
    client.fail_search = EtsyApiError(503, "unavailable", method="GET", path="/listings/active")
    report = _run(ws, template, client, use_cache=False)
    item = report.items[0]
    assert item.status == stream.OK
    assert item.steps["research"] == "warn"
    assert "no_market_data" in [w.code for w in item.warnings]


def test_a_dry_run_checks_everything_and_writes_nothing_to_etsy(studio):
    ws, template = studio
    _artwork(ws.products / "retro-mountain-sunset.png")
    _artwork(ws.products / "IMG_0001.png")
    report = _run(ws, template, None, dry_run=True)
    statuses = {item.name: item.status for item in report.items}
    assert statuses == {"retro-mountain-sunset.png": stream.CHECKED, "IMG_0001.png": stream.FAILED}
    assert report.items[1].steps["draft"] == "todo"
    assert not (ws.root / "upload-history.json").exists()
    text = report.csv_path.read_text(encoding="utf-8-sig")
    assert "retro-mountain-sunset.png" in text and "IMG_0001" not in text


def test_a_template_that_cannot_make_a_draft_stops_before_any_work(studio):
    ws, template = studio
    _artwork(ws.products / "retro-mountain-sunset.png")
    template.fields["price"] = -1
    client = Client(ws)
    with pytest.raises(ValidationError, match="template listing cannot make a draft"):
        _run(ws, template, client)
    assert client.creates == [] and list(ws.drafts.iterdir()) == []


def test_another_run_holding_the_lock_is_refused(studio):
    ws, template = studio
    _artwork(ws.products / "retro-mountain-sunset.png")
    (ws.root / ".auto-upload.lock").write_text("4242")
    with pytest.raises(automation.UploadLocked):
        _run(ws, template, Client(ws))


def test_too_many_mockups_for_one_listing_is_refused(studio):
    ws, template = studio
    _artwork(ws.products / "retro-mountain-sunset.png")
    many = [ws.mockups / "tshirt-white.jpg"] * 20
    with pytest.raises(ValidationError, match="Etsy allows 20"):
        _run(ws, template, Client(ws), mockups=many)


def test_transparent_photos_in_a_ready_folder_are_flagged(studio):
    ws, template = studio
    folder = ws.products / "mountain sunset shirt"
    folder.mkdir()
    _artwork(folder / "1-front.png")
    _photo(folder / "2-back.jpg")
    report = _run(ws, template, Client(ws))
    item = report.items[0]
    assert item.status == stream.OK
    assert item.steps["mockup"] == "warn"
    assert [w.code for w in item.warnings if w.step == "mockup"] == ["transparent_photos"]


def test_a_template_listing_gone_from_etsy_stops_before_the_plan(studio):
    ws, template = studio
    _artwork(ws.products / "retro-mountain-sunset.png")
    events = Events()

    class Gone(Client):
        def listing_inventory(self, listing_id):
            raise EtsyApiError(404, "Listing not found", method="GET", path="/listings/1/inventory")

    with pytest.raises(stream.TemplateGone):
        _run(ws, template, Gone(ws), on_event=events)
    assert events.items == []
    assert not (ws.root / ".auto-upload.lock").exists()


# --- failure paths (test-gaps) -----------------------------------------------------------------


def test_an_image_that_fails_after_the_create_leaves_a_partial_draft_on_record(studio):
    ws, template = studio
    _artwork(ws.products / "retro-mountain-sunset.png")
    _artwork(ws.products / "ocean-waves.png")

    class ThirdImageFails(Client):
        def upload_listing_image(self, listing_id, image, *, rank, alt_text=""):
            if listing_id == 1000001 and rank == 2:
                raise EtsyApiError(400, "bad image", method="POST", path="/images")
            return super().upload_listing_image(listing_id, image, rank=rank)

    events = Events()
    report = _run(ws, template, ThirdImageFails(ws), on_event=events)
    by_name = {item.name: item for item in report.items}
    partial = by_name["ocean-waves.png"]
    assert partial.status == stream.PARTIAL and partial.listing_id == 1000001
    assert partial.steps["draft"] == "warn" and partial.images_uploaded == 1
    assert "partial" in [w.code for w in partial.warnings]
    entry = _history(ws)[SHOP]["ocean-waves.png"]
    assert entry["status"] == "partial" and entry["listing_id"] == 1000001
    assert entry["images_uploaded"] == 1
    assert by_name["retro-mountain-sunset.png"].status == stream.OK, "the next one still goes"
    again = _run(ws, template, Client(ws))
    assert again.items == [] and any("ocean-waves.png" in n for n in again.needs_review)


def test_a_busy_history_file_does_not_stop_the_run(studio, monkeypatch):
    # Windows: a reader (antivirus, OneDrive, the app's own status check) holds the
    # file open, and os.replace answers "Access denied" now and then.
    ws, template = studio
    for name in ("a-retro-sunset.png", "b-ocean-waves.png"):
        _artwork(ws.products / name)
    monkeypatch.setattr(automation, "REPLACE_FIRST_PAUSE", 0.001)
    real = automation.os.replace
    calls = {"n": 0}

    def sometimes_busy(src, dst):
        calls["n"] += 1
        if calls["n"] % 3 == 0:
            raise PermissionError(13, "Access denied")
        return real(src, dst)

    monkeypatch.setattr(automation.os, "replace", sometimes_busy)
    report = _run(ws, template, Client(ws))
    assert [item.status for item in report.items] == [stream.OK, stream.OK]
    assert {e["status"] for e in _history(ws)[SHOP].values()} == {"ok"}


def test_the_lock_removed_mid_run_ends_the_run_normally(studio):
    ws, template = studio
    _artwork(ws.products / "retro-mountain-sunset.png")
    lock = ws.root / ".auto-upload.lock"

    class Unlocking(Client):
        def create_draft_listing(self, fields):
            if lock.exists():
                lock.unlink()
            return super().create_draft_listing(fields)

    report = _run(ws, template, Unlocking(ws))
    assert report.items[0].status == stream.OK and report.finished_at is not None


def test_a_design_too_large_to_decode_fails_only_that_product(studio):
    ws, template = studio
    Image.new("1", (13000, 13000)).save(ws.products / "huge-mountain-poster.png")
    _artwork(ws.products / "retro-mountain-sunset.png")
    client = Client(ws)
    with pytest.warns(Image.DecompressionBombWarning):
        report = _run(ws, template, client)
    by_name = {item.name: item for item in report.items}
    huge = by_name["huge-mountain-poster.png"]
    assert huge.status == stream.FAILED and huge.error.code == "too_many_pixels"
    assert huge.error.params["width"] == 13000 and huge.steps["mockup"] == "error"
    assert by_name["retro-mountain-sunset.png"].status == stream.OK
    assert len(client.creates) == 1


def test_a_finished_photo_over_etsys_limit_goes_up_smaller(studio, monkeypatch):
    from etsyotomasyon.drop import pipeline

    ws, template = studio
    photo = ws.products / "ocean-waves-photo.jpg"
    Image.effect_noise((500, 500), 90).convert("RGB").save(photo, quality=98)
    monkeypatch.setattr(pipeline, "MAX_IMAGE_BYTES", photo.stat().st_size // 3)
    client = Client(ws)
    report = _run(ws, template, client)
    item = report.items[0]
    assert item.status == stream.OK and item.mode == "as_is"
    shrunk = [w for w in item.warnings if w.step == "mockup"]
    assert [w.code for w in shrunk] == ["as_is", "shrunk"]
    shrunk = shrunk[1:]
    assert shrunk[0].params["name"] == "ocean-waves-photo.jpg"
    assert [name for _id, name, _rank in client.images] == ["ocean-waves-photo-jpg-etsy.jpg"]


def test_a_title_etsy_would_refuse_is_cleaned_and_said_so(studio, monkeypatch):
    from etsyotomasyon.drop import generate

    ws, template = studio
    _artwork(ws.products / "retro-mountain-sunset.png")
    monkeypatch.setattr(generate, "build_title",
                        lambda seed, market, **_kw: "Salt & Pepper & Co, $5 Mug \U0001f338")
    client = Client(ws)
    report = _run(ws, template, client)
    item = report.items[0]
    assert item.status == stream.OK and item.steps["title"] == "warn"
    assert "title_cleaned" in [w.code for w in item.warnings]
    assert client.creates[0]["title"] == "Salt & Pepper and Co, 5 Mug"


def test_a_title_with_nothing_etsy_accepts_fails_that_product(studio, monkeypatch):
    from etsyotomasyon.drop import generate

    ws, template = studio
    _artwork(ws.products / "retro-mountain-sunset.png")
    monkeypatch.setattr(generate, "build_title",
                        lambda seed, market, **_kw: "\U0001f338\U0001f338")
    client = Client(ws)
    report = _run(ws, template, client)
    item = report.items[0]
    assert item.status == stream.FAILED and item.error.code == "invalid_title"
    assert item.steps["title"] == "error" and client.creates == []


def test_the_flat_design_is_marked_apart_from_the_mockups(studio):
    ws, template = studio
    _artwork(ws.products / "retro-mountain-sunset.png")
    events = Events()
    report = _run(ws, template, Client(ws), on_event=events)
    item = report.items[0]
    assert item.flat is not None and item.flat == item.images[-1]
    outcome = events.outcome("retro-mountain-sunset.png")
    assert outcome["flat"].endswith("--flat.jpg") and outcome["flat"] in outcome["images"]
    assert stream.item_summary(item, ws.root)["flat"] == outcome["flat"]


@pytest.mark.parametrize("junk", ["Adsız tasarım (3).png", "Untitled design (4).png",
                                  "image (1).png", "IMG_4432.png"])
def test_a_canva_or_camera_default_name_never_becomes_a_draft(studio, junk):
    ws, template = studio
    _artwork(ws.products / junk)
    _artwork(ws.products / "retro-mountain-sunset.png")
    client = Client(ws)
    report = _run(ws, template, client)
    by_name = {item.name: item for item in report.items}
    assert by_name[junk].status == stream.FAILED and by_name[junk].error.code == "junk_name"
    assert by_name["retro-mountain-sunset.png"].status == stream.OK
    assert len(client.creates) == 1 and junk not in _history(ws)[SHOP]


# --- digital templates (type download / both) --------------------------------------------


def _digital(ws, template, listing_type="download"):
    """The studio's template turned into a digital one, as Şablon İlan would save it."""
    # Made to order would need no download file (pipeline.deliverables).
    fields = dict(template.fields, type=listing_type, when_made="2020_2026")
    if listing_type == "download":
        fields.pop("shipping_profile_id", None)
    digital = Template(template.source_listing_id, source_title="Printable Wall Art",
                       fields=fields, description=template.description, tags=template.tags)
    ws.write_template(digital.to_dict())
    return digital


def _folder(ws, name, photos=("01-front.jpg", "02-detail.jpg"), files=None, sub="dosyalar"):
    folder = ws.products / name
    folder.mkdir()
    for photo in photos:
        _photo(folder / photo)
    if files is not None:
        (folder / sub).mkdir()
        for file_name, data in files.items():
            (folder / sub / file_name).write_bytes(data)
    return folder


def test_a_digital_loose_design_gets_its_original_file_after_its_images(studio):
    ws, template = studio
    template = _digital(ws, template)
    design = _artwork(ws.products / "retro-mountain-sunset.png")
    original = design.read_bytes()
    client = Client(ws)
    events = Events()
    report = _run(ws, template, client, on_event=events)

    item = report.items[0]
    assert item.status == stream.OK and item.steps["check"] == "done"
    assert [p.name for p in item.deliverables] == ["retro-mountain-sunset.png"]
    assert client.creates[0]["type"] == "download"
    assert "shipping_profile_id" not in client.creates[0], "a download is not shipped"
    # The ORIGINAL design goes up as the download, never the flat render or a mockup,
    # and only after every image of the draft.
    assert client.files == [(1000001, "retro-mountain-sunset.png", 1)]
    kinds = [kind for kind, _id, _name in client.calls]
    assert kinds == ["image"] * len(item.images) + ["file"]
    assert item.files_uploaded == 1 and design.read_bytes() == original
    entry = _history(ws)[SHOP]["retro-mountain-sunset.png"]
    assert entry["status"] == "ok" and entry["files_uploaded"] == 1
    outcome = events.outcome("retro-mountain-sunset.png")
    assert outcome["deliverables"] == ["2-PRODUCTS/retro-mountain-sunset.png"]
    assert outcome["files_uploaded"] == 1 and outcome["files_total"] == 1
    batch = next(data for _n, step, _s, data in events.items if step == "batch")
    assert batch["listing_type"] == "download" and batch["items"][0]["files_total"] == 1
    drafts = [data for _n, step, status, data in events.items
              if step == "draft" and status == "done"]
    assert drafts[0]["files_uploaded"] == 1 and drafts[0]["files_total"] == 1
    row = csvio.read_rows(report.csv_path)[0]
    assert row["files"] == "../../2-PRODUCTS/retro-mountain-sunset.png"
    assert stream.item_summary(item, ws.root)["deliverables"] == [
        "2-PRODUCTS/retro-mountain-sunset.png"]


def test_a_digital_folder_sends_its_photos_then_its_dosyalar_files(studio):
    ws, template = studio
    template = _digital(ws, template)
    _folder(ws, "boho planner", files={
        "10-notes.pdf": b"%PDF-1.4 notes", "2-planner.pdf": b"%PDF-1.4 planner",
        "extras.zip": b"PK\x03\x04", "Thumbs.db": b"x", ".DS_Store": b"x"})
    client = Client(ws)
    report = _run(ws, template, client)

    item = report.items[0]
    assert item.status == stream.OK and item.mode == "photos"
    assert [p.name for p in item.images] == ["01-front.jpg", "02-detail.jpg"], "photos stay"
    assert client.files == [(1000001, "2-planner.pdf", 1), (1000001, "10-notes.pdf", 2),
                            (1000001, "extras.zip", 3)]
    assert [kind for kind, *_ in client.calls] == ["image", "image", "file", "file", "file"]
    assert _history(ws)[SHOP]["boho planner"]["files_uploaded"] == 3


def test_the_files_folder_may_be_called_files_in_any_case(studio):
    ws, template = studio
    template = _digital(ws, template)
    _folder(ws, "kids coloring pages", files={"pages.pdf": b"%PDF-1.4"}, sub="Files")
    client = Client(ws)
    assert _run(ws, template, client).items[0].status == stream.OK
    assert client.files == [(1000001, "pages.pdf", 1)]


@pytest.mark.parametrize("files, code", [
    (None, "no_deliverable"),
    ({}, "no_deliverable"),
    ({f"page-{n}.pdf": b"%PDF" for n in range(6)}, "too_many_files"),
    ({"setup.exe": b"MZ"}, "file_type"),
    ({"empty.pdf": b""}, "file_empty"),
])
def test_a_missing_or_unusable_download_fails_that_product_at_check(studio, files, code):
    ws, template = studio
    template = _digital(ws, template)
    _folder(ws, "boho planner", files=files)
    _artwork(ws.products / "retro-mountain-sunset.png")
    client = Client(ws)
    report = _run(ws, template, client)

    by_name = {item.name: item for item in report.items}
    bad = by_name["boho planner"]
    assert bad.status == stream.FAILED and bad.error.code == code
    assert bad.error.step == "check" and bad.steps["check"] == "error"
    assert bad.steps["draft"] == "todo"
    assert "boho planner" not in _history(ws)[SHOP], "nothing was sent for it"
    assert by_name["retro-mountain-sunset.png"].status == stream.OK, "the others still go"
    assert len(client.creates) == 1


def test_an_oversize_download_is_refused_before_the_draft(studio, monkeypatch):
    from etsyotomasyon import client as client_mod

    ws, template = studio
    template = _digital(ws, template)
    monkeypatch.setattr(client_mod, "MAX_FILE_BYTES", 1000)
    _folder(ws, "boho planner", files={"planner.pdf": b"%PDF" + b"0" * 2000})
    client = Client(ws)
    item = _run(ws, template, client).items[0]
    assert item.status == stream.FAILED and item.error.code == "file_too_large"
    assert item.error.params["name"] == "planner.pdf" and client.creates == []


def test_a_download_that_fails_after_the_create_leaves_the_draft_partial(studio):
    ws, template = studio
    template = _digital(ws, template)
    _folder(ws, "boho planner", files={"1-planner.pdf": b"%PDF-1", "2-extras.pdf": b"%PDF-2"})
    client = Client(ws)
    client.fail_file["2-extras"] = EtsyApiError(400, "file refused", method="POST",
                                                 path="/files")
    report = _run(ws, template, client)

    item = report.items[0]
    assert item.status == stream.PARTIAL and item.listing_id == 1000001
    assert item.steps["draft"] == "warn" and item.files_uploaded == 1
    warning = next(w for w in item.warnings if w.code == "partial_files")
    assert warning.params == {"listing_id": 1000001, "n": 1, "total": 2, "name": "2-extras.pdf"}
    assert "file 2 of 2 (2-extras.pdf) failed" in warning.message
    entry = _history(ws)[SHOP]["boho planner"]
    assert entry["status"] == "partial" and entry["listing_id"] == 1000001
    assert entry["files_uploaded"] == 1 and entry["images_uploaded"] == 2
    again = _run(ws, template, Client(ws))
    assert again.items == [] and any("boho planner" in n for n in again.needs_review)


def test_a_both_template_ships_and_downloads(studio):
    ws, template = studio
    template = _digital(ws, template, "both")
    _artwork(ws.products / "retro-mountain-sunset.png")
    client = Client(ws)
    item = _run(ws, template, client).items[0]
    assert item.status == stream.OK
    assert client.creates[0]["type"] == "both"
    assert client.creates[0]["shipping_profile_id"] == 55, "both still ships"
    assert client.files == [(1000001, "retro-mountain-sunset.png", 1)]


def test_a_physical_template_never_uploads_a_file(studio):
    ws, template = studio
    _artwork(ws.products / "retro-mountain-sunset.png")
    _folder(ws, "boho planner", files={"planner.pdf": b"%PDF-1"})
    client = Client(ws)
    report = _run(ws, template, client)
    assert [item.status for item in report.items] == [stream.OK, stream.OK]
    assert client.files == [] and all(not item.deliverables for item in report.items)


def test_a_digital_dry_run_checks_the_downloads_and_sends_nothing(studio):
    ws, template = studio
    template = _digital(ws, template)
    _folder(ws, "boho planner", files={"planner.pdf": b"%PDF-1"})
    _folder(ws, "sunset poster set")
    report = _run(ws, template, None, dry_run=True, use_cache=False)
    by_name = {item.name: item for item in report.items}
    assert by_name["boho planner"].status == stream.CHECKED
    assert [p.name for p in by_name["boho planner"].deliverables] == ["planner.pdf"]
    assert by_name["sunset poster set"].error.code == "no_deliverable"
    assert not (ws.root / "upload-history.json").exists()


def test_an_unknown_template_type_stops_before_any_work(studio):
    ws, template = studio
    template.fields["type"] = "subscription"
    with pytest.raises(ValidationError, match="not one Etsy knows"):
        _run(ws, template, Client(ws))


def test_the_template_tells_the_title_and_tags_what_the_product_is(studio, monkeypatch):
    from etsyotomasyon.drop import generate

    ws, template = studio
    template.source_title = "Ceramic Coffee Mug 11oz"
    _artwork(ws.products / "black-cat-magic.png")
    seen: dict[str, object] = {}
    real_title, real_tags = generate.build_title, generate.build_tags

    def title(seed, market=None, *, product_hint=None):
        seen["title"] = product_hint
        return real_title(seed, market, product_hint=product_hint)

    def tags(seed, market=None, *, product_hint=None):
        seen["tags"] = product_hint
        return real_tags(seed, market, product_hint=product_hint)

    monkeypatch.setattr(generate, "build_title", title)
    monkeypatch.setattr(generate, "build_tags", tags)
    _run(ws, template, Client(ws))
    expected = generate.hint_from(template.source_title, template.tags, template.description)
    assert seen == {"title": expected, "tags": expected}
    assert expected[0] == "Ceramic Coffee Mug 11oz"


def test_a_sign_in_lost_between_two_files_keeps_what_went_up_on_record(studio):
    ws, template = studio
    template = _digital(ws, template)
    _folder(ws, "boho planner", files={"1-planner.pdf": b"%PDF-1", "2-extras.pdf": b"%PDF-2"})
    _artwork(ws.products / "retro-mountain-sunset.png")
    client = Client(ws)
    client.fail_file["2-extras"] = AuthError("The sign-in expired and could not be renewed.")
    report = _run(ws, template, client)

    by_name = {item.name: item for item in report.items}
    item = by_name["retro-mountain-sunset.png"]
    assert item.status == stream.OK, "the loose design went first"
    folder = by_name["boho planner"]
    assert folder.status == stream.PARTIAL and folder.files_uploaded == 1
    assert folder.images_uploaded == 2
    entry = _history(ws)[SHOP]["boho planner"]
    assert entry["status"] == "partial" and entry["files_uploaded"] == 1
    assert report.stopped is not None and report.stopped.code == "reconnect"



# --- a product folder with only its downloads ------------------------------------------------


@pytest.mark.parametrize("listing_type", ["download", "physical"])
def test_a_folder_with_only_its_dosyalar_fails_the_check_step(studio, listing_type):
    ws, template = studio
    if listing_type == "download":
        template = _digital(ws, template)
    _folder(ws, "boho planner", photos=(), files={"planner.pdf": b"%PDF-1"}, sub="Dosyalar")
    _artwork(ws.products / "retro-mountain-sunset.png")
    events = Events()
    client = Client(ws)
    report = _run(ws, template, client, on_event=events)

    by_name = {item.name: item for item in report.items}
    assert set(by_name) == {"boho planner", "retro-mountain-sunset.png"}, "listed, not lost"
    item = by_name["boho planner"]
    assert item.status == stream.FAILED and item.kind == "folder"
    assert item.error.code == "no_photos" and item.error.step == "check"
    assert item.error.params == {"name": "boho planner", "folder": "dosyalar"}
    assert "no photos" in item.error.message
    assert item.steps["check"] == "error" and item.steps["draft"] == "todo"
    assert item.steps["mockup"] == "todo" and item.steps["research"] == "todo"
    # Nothing is spent on it: no research for its name, no draft, no history entry.
    assert not any("boho planner" in s for s in client.searches)
    assert "boho planner" not in _history(ws)[SHOP]
    assert by_name["retro-mountain-sunset.png"].status == stream.OK
    batch = next(data for name, step, _s, data in events.items if step == "batch")
    shown = next(i for i in batch["items"] if i["name"] == "boho planner")
    assert shown["kind"] == "folder" and shown["files"] == 0 and shown["source"] == ""


# --- the template's own tags fill free slots only when they suit any design ----------------------


def test_free_tag_slots_skip_the_template_designs_own_tags(studio):
    ws, template = studio
    template = Template(
        template.source_listing_id,
        source_title="Retro Mountain Sunset Shirt, Vintage Hiking Tee, Nature Lover Gift",
        fields=template.fields, description=template.description,
        tags=["retro mountain sun", "hiking gift", "nature lover gift", "graphic tee",
              "comfort colors", "gift for her"])
    ws.write_template(template.to_dict())
    _artwork(ws.products / "dog-dad-paw-print.png")
    client = Client(ws)
    client.fail_search = EtsyApiError(503, "down")  # no market: the template fills in
    report = _run(ws, template, client)

    item = report.items[0]
    assert item.status == stream.OK
    assert {"graphic tee", "comfort colors", "gift for her"} <= set(item.tags)
    assert not {"retro mountain sun", "hiking gift", "nature lover gift"} & set(item.tags)
    assert "your template listing's tags" in item.evidence
    # The search named the template's product: "print" alone would search posters.
    assert client.searches and set(client.searches) == {"dog dad paw print shirt"}


# --- a draft avoids the tags the shop's other drafts carry ------------------------------------------


def _expected_tags(report, template, hint):
    """What the tags of each product are when every product before it is known: the same rule,
    applied one product at a time."""
    from etsyotomasyon.drop import generate

    done: list[list[str]] = []
    for item in report.items:
        tags = generate.build_tags(item.seed, item.market, product_hint=hint, taken=list(done))
        generate.fill_tags(tags, generate.product_tags(template.tags, template.source_title,
                                                       item.seed, hint=hint))
        done.append(tags[:13])
    return done


def test_each_product_avoids_the_tags_of_the_products_before_it(studio):
    from etsyotomasyon.drop import generate

    ws, template = studio
    template.source_title = "Black Cat Shirt"
    template.tags = ["gift idea", "graphic tee", "unisex tshirt", "cat lover gift"]
    for name in ("black-cat-magic", "black-cat-moon", "black-cat-garden", "black-cat-night",
                 "black-cat-witch"):
        _artwork(ws.products / f"{name}.png")
    # Three products are prepared at once: the tags still come out as if one at a time.
    report = _run(ws, template, Client(ws), concurrency=3)
    assert all(item.status == stream.OK for item in report.items)
    hint = generate.hint_from(template.source_title, template.tags, template.description,
                              template.materials)
    expected = _expected_tags(report, template, hint)
    assert [item.tags for item in report.items] == expected
    for a in range(len(expected)):
        for b in range(a):
            assert len(set(expected[a]) & set(expected[b])) < 7, (a, b)
    # Without that rule these five would share most of their thirteen tags.
    alone = [generate.build_tags(i.seed, i.market, product_hint=hint) for i in report.items]
    assert max(len(set(alone[a]) & set(alone[b])) for a in range(5) for b in range(a)) >= 7


def test_the_history_keeps_each_drafts_tags_and_the_next_run_avoids_them(studio):
    ws, template = studio
    _artwork(ws.products / "black-cat-magic.png")
    first = _run(ws, template, Client(ws))
    first_tags = first.items[0].tags
    entry = _history(ws)[SHOP]["black-cat-magic.png"]
    assert entry["tags"] == first_tags and entry["status"] == "ok"

    _artwork(ws.products / "black-cat-moon.png")
    second = _run(ws, template, Client(ws))
    assert [i.name for i in second.items] == ["black-cat-moon.png"]
    new = second.items[0].tags
    assert len(set(new) & set(first_tags)) < 7
    assert _history(ws)[SHOP]["black-cat-moon.png"]["tags"] == new


def test_history_written_before_tags_were_kept_does_not_stop_a_run(studio):
    ws, template = studio
    _artwork(ws.products / "black-cat-magic.png")
    _run(ws, template, Client(ws))
    history = _history(ws)
    del history[SHOP]["black-cat-magic.png"]["tags"]
    (ws.root / "upload-history.json").write_text(json.dumps(history), encoding="utf-8")
    _artwork(ws.products / "black-cat-moon.png")
    report = _run(ws, template, Client(ws))
    assert report.items[0].status == stream.OK and len(report.items[0].tags) == 13


def test_a_product_that_fails_before_its_copy_does_not_hold_up_the_ones_after_it(studio):
    ws, template = studio
    (ws.products / "IMG_0001.png").write_bytes(b"not a picture")  # a junk name: fails at once
    _artwork(ws.products / "black-cat-magic.png")
    _artwork(ws.products / "black-cat-moon.png")
    started = time.perf_counter()
    report = _run(ws, template, Client(ws), concurrency=3)
    assert time.perf_counter() - started < 30
    by_name = {i.name: i for i in report.items}
    assert by_name["IMG_0001.png"].status == stream.FAILED
    assert by_name["black-cat-magic.png"].status == stream.OK
    assert by_name["black-cat-moon.png"].status == stream.OK
