"""0.3.2 review fixes for the run features: stamped copies that never share a file or
make a path too long, one mockup rule (a download-only template filters, then caps) for
the app, `drop run` and `drop auto`, alt texts and info images told apart by path and
by Etsy's image id, `listings push` of a `drop run` batch, and the client's image
reads. All data is invented."""

from __future__ import annotations

import io
import json
from pathlib import Path

import httpx
import pytest
import test_drop_stream
from PIL import Image, ImageStat
from test_client_extra import make_client
from test_drop_stream import SHOP, _artwork, _history, _run
from test_infoimages import picture
from test_round4_digital import _opaque, _template
from test_watermark import MarkClient, _cli_ws, _set_mark, _stamped
from test_web_designs import _digital_template, _png, _put, _setup_shop
from typer.testing import CliRunner
from web_helpers import wait_for_job

from etsyotomasyon.cli import app
from etsyotomasyon.drop import automation, catalog, infoimages, pipeline, stream
from etsyotomasyon.drop import watermark as wm
from etsyotomasyon.drop.workspace import INFO_DIR
from etsyotomasyon.errors import EtsyApiError
from etsyotomasyon.web.api import designs
from etsyotomasyon.web.api import listings as listings_api

studio = test_drop_stream.studio

RED, BLUE = (200, 40, 40), (40, 60, 200)


def _tinted(path: Path, colour, fmt=None) -> Path:
    """A finished photo: a colour with noise, so no border is one colour (never "flat")."""
    base = Image.new("RGB", (80, 80), colour)
    noise = Image.effect_noise((80, 80), 60).convert("RGB")
    picture_ = Image.blend(base, noise, 0.25)
    if fmt == "GIF":
        picture_ = picture_.convert("P")
    picture_.save(path, fmt)
    return path


def _nearer(path: Path) -> tuple[int, int, int]:
    with Image.open(path) as opened:
        mean = ImageStat.Stat(opened.convert("RGB")).mean
    return min((RED, BLUE), key=lambda c: sum((a - b) ** 2 for a, b in zip(mean, c)))


# --- P1: two loose photos whose stamped copies would share a name ------------------------------


@pytest.mark.parametrize(("first", "second", "fmt"), [
    ("sunset.jpg", "sunset.jpeg", None), ("sunset-poster.png", "sunset-poster.gif", "GIF")])
def test_the_apps_run_never_gives_two_products_one_stamped_file(studio, first, second, fmt):
    ws, template = studio
    _set_mark(ws, scope="all", position="corner")
    _tinted(ws.products / first, RED)
    _tinted(ws.products / second, BLUE, fmt)
    report = _run(ws, template, MarkClient(ws))
    items = {item.name: item for item in report.items}
    assert all(item.status == stream.OK for item in items.values())
    [one], [two] = items[first].images, items[second].images
    assert one != two and _stamped(one) and _stamped(two)
    # Each draft shows its own photo.
    assert _nearer(one) == RED and _nearer(two) == BLUE


@pytest.mark.parametrize(("first", "second", "fmt"), [
    ("sunset.jpg", "sunset.jpeg", None), ("sunset-poster.png", "sunset-poster.gif", "GIF")])
def test_drop_run_never_gives_two_products_one_stamped_file(tmp_path, monkeypatch, first,
                                                            second, fmt):
    ws = _cli_ws(tmp_path, monkeypatch)
    template = _template(ws, listing_type="physical", when_made="made_to_order")
    _set_mark(ws, scope="all")
    _tinted(ws.products / first, RED)
    _tinted(ws.products / second, BLUE, fmt)
    report = pipeline.run(ws, template, client=None, use_cache=False, mockups=ws.mockup_files())
    rows = {row.source.name: row for row in report.rows}
    [one], [two] = rows[first].images, rows[second].images
    assert one != two and _stamped(one) and _stamped(two)
    assert _nearer(one) == RED and _nearer(two) == BLUE


# --- P6: the stamped copy's path has the design's name once --------------------------------------


def test_a_long_design_name_keeps_the_stamped_path_short(studio):
    ws, template = studio
    _set_mark(ws, scope="all")
    name = ("retro-mountain-sunset-vintage-hiking-camping-outdoor-adventure-nature-lover-"
            "gift-idea-for-dad-and-mom")
    name = (name + "-x" * 20)[:100]
    assert len(name) == 100
    _artwork(ws.products / f"{name}.png")
    item = _run(ws, template, MarkClient(ws)).items[0]
    assert item.status == stream.OK and item.images
    for image in item.images:
        assert _stamped(image)
        assert len(image.relative_to(ws.root).as_posix()) < 240
        assert image.relative_to(ws.root).as_posix().count(name) == 1


# --- P2 / P3 / UI-4: one mockup rule, filter then cap -----------------------------------------------


def _many_mockups(ws, tshirts=15, posters=4):
    for n in range(1, tshirts + 1):
        Image.new("RGB", (40, 40), (230, 230, 230)).save(ws.mockups / f"{n:02d}-tshirt-white.jpg")
    for n in range(tshirts + 1, tshirts + posters + 1):
        Image.new("RGB", (40, 40), (220, 200, 180)).save(ws.mockups / f"{n:02d}-poster-oak-frame.jpg")


def test_a_download_template_filters_the_physical_mockups_before_the_cap(tmp_path):
    from etsyotomasyon.drop.workspace import Workspace

    ws = Workspace(tmp_path / "studio").create()
    _many_mockups(ws)
    for n in range(5):
        infoimages.add(ws, f"info-{n}.jpg", picture())
    posters = [f"{n:02d}-poster-oak-frame.jpg" for n in range(16, 20)]
    use = catalog.usage(ws, listing_type="download")
    assert use["max"] == 14 and use["used"] == posters and use["over_limit"] == []
    assert len(use["left_out"]) == 15 and use["enabled"] == 4
    assert [p.name for p in catalog.run_mockups(ws, "download")] == posters
    assert designs.used_mockups(ws, "download") == (posters, use["left_out"])
    # A physical template keeps the old rule: the first 14.
    physical = catalog.usage(ws, listing_type="physical")
    assert len(physical["used"]) == 14 and len(physical["over_limit"]) == 5
    assert physical["left_out"] == []


def test_the_start_card_and_the_run_use_the_posters_after_the_tshirts(web, monkeypatch):
    _fake, ws = _setup_shop(web, mockups=0)
    _many_mockups(ws)
    for n in range(5):
        infoimages.add(ws, f"info-{n}.jpg", picture())
    _digital_template(ws)
    _put(web, "retro-mountain-sunset.png", _png())
    data = web.client.get("/api/designs/pending").json()
    mk = data["mockups"]
    posters = [f"{n:02d}-poster-oak-frame.jpg" for n in range(16, 20)]
    assert mk["names"] == posters and mk["over_limit"] == 0 and len(mk["left_out"]) == 15
    assert mk["used_items"] == [{"name": n, "type": "poster", "color": "Meşe"} for n in posters]
    assert "no_mockups" not in data["warnings"]
    seen: dict = {}

    def fake_run(ws, template, client, **kw):
        seen.update(kw)
        raise stream.TemplateGone("stop here")

    monkeypatch.setattr(stream, "run_stream", fake_run)
    job = web.client.post("/api/designs/start", json={"dry_run": True})
    assert job.status_code == 200, job.text
    wait_for_job(web, job.json()["id"], timeout=20)
    assert [p.name for p in seen["mockups"]] == posters


def test_the_cli_runs_leave_out_physical_mockups_for_a_download(tmp_path, monkeypatch):
    monkeypatch.setenv("ETSYOTOMASYON_HOME", str(tmp_path / "home"))
    from etsyotomasyon.drop.workspace import Workspace

    ws = Workspace(tmp_path / "studio").create()
    for name in ("tshirt-white.jpg", "mug-white.jpg", "poster-frame.jpg"):
        Image.new("RGB", (300, 300), (235, 235, 235)).save(ws.mockups / name)
    template = _template(ws)  # a download
    _opaque(ws.products / "sunset.jpg", size=(300, 375))
    report = automation.run(ws, template, dry_run=True)
    [row] = report.prepared.ready
    assert sorted(p.name for p in row.images) == ["sunset--flat.jpg", "sunset--poster-frame.jpg"]
    (ws.root / "upload-history.json").unlink(missing_ok=True)
    result = CliRunner().invoke(app, ["drop", "auto", "--path", str(ws.root), "--dry-run"])
    assert result.exit_code == 0, result.output
    said = " ".join(result.output.split())
    assert "Mockups: 1 of 3 used" in said
    assert "2 left out because they show a physical product" in said


# --- estimate-opaque-place ----------------------------------------------------------------------


def test_the_estimate_counts_a_jpeg_on_a_solid_ground_as_placed(web):
    _fake, ws = _setup_shop(web)  # two mockups, a physical template

    def jpeg(colour_border):
        image = Image.new("RGB", (60, 60), colour_border)
        image.paste(Image.effect_noise((30, 30), 60).convert("RGB"), (15, 15))
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=95)
        return buffer.getvalue()

    _put(web, "sunny-lemon-garden.jpg", jpeg((255, 255, 255)))
    flat = web.client.get("/api/designs/pending").json()
    assert flat["items"][0]["ground"] == "flat"
    (ws.products / "sunny-lemon-garden.jpg").unlink()
    buffer = io.BytesIO()
    Image.effect_noise((60, 60), 80).convert("RGB").save(buffer, "JPEG", quality=95)
    _put(web, "sunny-lemon-garden.jpg", buffer.getvalue())
    photo = web.client.get("/api/designs/pending").json()
    assert photo["items"][0]["ground"] == "photo"
    # The "place" choice puts it onto both mockups plus the flat render: 3 uploads, not 1.
    assert flat["estimate_requests"] - photo["estimate_requests"] == 2


# --- P4 / alt-by-name: alt texts by path, info images by Etsy's image id -----------------------------


class IdClient(MarkClient):
    """Records each upload's folder, name and alt text, and answers with an image id."""

    def __init__(self, ws):
        super().__init__(ws)
        self.sent: list[tuple[str, str, str, int]] = []

    def upload_listing_image(self, listing_id, image, *, rank, alt_text="", is_watermarked=False):
        super().upload_listing_image(listing_id, image, rank=rank, alt_text=alt_text,
                                     is_watermarked=is_watermarked)
        self.sent.append((Path(image).parent.name, Path(image).name, alt_text, 5000 + rank))
        return {"listing_image_id": 5000 + rank}


def test_an_info_image_named_like_a_product_photo_keeps_its_own_alt(studio):
    ws, template = studio
    folder = ws.products / "mountain sunset shirt"
    folder.mkdir()
    for name in ("1-front.jpg", "size.jpg"):
        _tinted(folder / name, RED)
    infoimages.add(ws, "size.jpg", picture())  # no alt text
    client = IdClient(ws)
    item = _run(ws, template, client).items[0]
    assert item.status == stream.OK
    by_folder = {(d, n): (a, i) for d, n, a, i in client.sent}
    assert by_folder[("mountain sunset shirt", "size.jpg")][0] == "Mountain Sunset Shirt"
    assert by_folder[(INFO_DIR, "size.jpg")][0] == ""
    entry = _history(ws)[SHOP]["mountain sunset shirt"]
    info_id = str(by_folder[(INFO_DIR, "size.jpg")][1])
    assert entry["info_images"] == ["size.jpg"] and entry["info_image_ids"] == [info_id]
    # The listing page: the product's own size.jpg is not an info image.
    ids = set(entry["info_image_ids"])
    names = set(entry["info_images"])
    own = listings_api._image_view({"listing_image_id": 5002}, "size.jpg", "", {}, names, ids)
    info = listings_api._image_view({"listing_image_id": int(info_id)}, "size.jpg", "", {},
                                    names, ids)
    assert own["info"] is False and info["info"] is True
    # An entry from before 0.3.2 (no ids) still goes by the names.
    assert listings_api._image_view({"listing_image_id": 1}, "size.jpg", "", {}, names)["info"]


def test_drop_auto_records_the_info_images_etsy_ids(tmp_path):
    from test_drop_combined import AutoClient

    from etsyotomasyon.drop.template import Template
    from etsyotomasyon.drop.workspace import Workspace

    ws = Workspace(tmp_path / "studio").create()
    folder = ws.products / "mountain sunset shirt"
    folder.mkdir()
    (folder / "1-front.jpg").write_bytes(picture(size=(80, 80)))
    infoimages.add(ws, "care.jpg", picture(), alt="Care guide")
    template = Template(1000013, source_title="Mountain Sunset Shirt", fields={
        "taxonomy_id": 1, "price": 20, "quantity": 5, "who_made": "i_did",
        "when_made": "made_to_order", "type": "physical"}, description="Soft cotton.")
    ws.write_template(template.to_dict())
    automation.run(ws, template, client=AutoClient())
    entry = json.loads(automation.history_path(ws.root).read_text(encoding="utf-8"))["123"][
        "mountain sunset shirt"]
    assert entry["info_images"] == ["care.jpg"] and entry["info_image_ids"] == ["5002"]


# --- P7: `listings push` of a drop run batch ---------------------------------------------------------


class PushClient:
    def __init__(self):
        self.images: list[tuple[str, str, str, bool]] = []

    def create_draft_listing(self, fields):
        return {"listing_id": 900}

    def upload_listing_image(self, listing_id, image, *, rank, alt_text="", is_watermarked=False):
        self.images.append((Path(image).parent.name, Path(image).name, alt_text, is_watermarked))
        return {"listing_image_id": 7000 + rank}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_listings_push_of_a_drop_run_batch_flags_the_mark_and_sends_info_alts(tmp_path,
                                                                             monkeypatch):
    import etsyotomasyon.cli as cli

    ws = _cli_ws(tmp_path, monkeypatch)
    template = _template(ws, listing_type="physical", when_made="made_to_order")
    _set_mark(ws, scope="all")
    _tinted(ws.products / "sunset.jpg", RED)
    infoimages.add(ws, "how-to-order.jpg", picture(), alt="How to order")
    infoimages.add(ws, "care.png", picture("PNG"))
    report = pipeline.run(ws, template, client=None, use_cache=False, mockups=ws.mockup_files())
    assert report.csv_path is not None
    assert json.loads((report.out_dir / pipeline.INFO_ALTS_FILE).read_text(encoding="utf-8")) == {
        f"{INFO_DIR}/how-to-order.jpg": "How to order"}
    client = PushClient()
    monkeypatch.setattr(cli, "_client", lambda **_kw: client)
    result = CliRunner().invoke(app, ["listings", "push", str(report.csv_path), "--yes"])
    assert result.exit_code == 0, result.output
    assert client.images == [
        (wm.STAMPED_DIR, "sunset.jpg", "", True),
        (INFO_DIR, "how-to-order.jpg", "How to order", False),
        (INFO_DIR, "care.png", "", False),
    ]
    # A CSV that is not a drop batch goes to the client as it is.
    assert automation.for_batch(client, tmp_path) is client


# --- the Etsy client ------------------------------------------------------------------------------------


def test_listing_images_decode_the_alt_text():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"count": 2, "results": [
            {"listing_image_id": 2, "rank": 2, "alt_text": "Size &amp; care &#39;guide&#39;"},
            {"listing_image_id": 1, "rank": 1, "alt_text": None},
        ]})

    images = make_client(handler).listing_images(1000001)
    assert [i["listing_image_id"] for i in images] == [1, 2]
    assert images[1]["alt_text"] == "Size & care 'guide'" and images[0]["alt_text"] is None


def test_download_image_never_takes_a_redirect_for_the_picture():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://i.etsystatic.com/x.jpg"},
                              content=b"<html>moved</html>")

    with pytest.raises(EtsyApiError) as caught:
        make_client(handler).download_image("https://i.etsystatic.com/1/il_fullxfull.1.jpg")
    assert caught.value.status == 302


def test_the_batch_wrapper_flags_only_the_batchs_stamped_copies(tmp_path):
    image = tmp_path / "watermarked" / "a.jpg"
    image.parent.mkdir()
    Image.new("RGB", (40, 40)).save(image)
    wrapped = automation.for_batch(PushClient(), tmp_path)
    assert isinstance(wrapped, automation.BatchImageClient)
    wrapped.upload_listing_image(1, image, rank=1)
    assert wrapped.client.images == [("watermarked", "a.jpg", "", True)]
