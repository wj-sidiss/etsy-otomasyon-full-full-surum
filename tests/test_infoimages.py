"""The shop's info images (drop.infoimages): stored once, appended to every draft.

Library, the 20-picture limit, the CLI's pipeline and `drop auto`, the app's stream, the
Etsy client's CDN download. Invented data only (ExampleShop, listing 1000013, ...).
"""

from __future__ import annotations

import io
import json

import httpx
import pytest
from PIL import Image
from test_client_extra import _parts, make_client, recorder
from test_drop_stream import Client as StreamClient
from test_drop_stream import (
    Events,
    _artwork,
    _history,
    _photo,
    _run,
    studio,  # noqa: F401 - the app-path fixture
)
from typer.testing import CliRunner

from etsyotomasyon import client as client_mod
from etsyotomasyon.cli import app
from etsyotomasyon.config import MAX_LISTING_IMAGES
from etsyotomasyon.drop import automation, catalog, infoimages, pipeline, stream
from etsyotomasyon.drop.template import Template
from etsyotomasyon.drop.workspace import INFO_DIR, Workspace, root_for
from etsyotomasyon.errors import EtsyApiError, ValidationError


def picture(fmt="JPEG", size=(60, 40), colour=(30, 90, 160), mode="RGB") -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, size, colour if mode == "RGB" else (*colour, 120)).save(buffer, format=fmt)
    return buffer.getvalue()


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path / "studio").create()


# --------------------------------------------------------------------------- storage


def test_pictures_are_kept_in_order_with_their_alt_text_and_etsy_ids(ws):
    first = infoimages.add(ws, "materials.jpg", picture(), alt="  Materials: vinyl  ",
                           listing_id=1000013, listing_image_id=4401)
    infoimages.add(ws, "size-chart.png", picture("PNG"))
    assert first.name == "materials.jpg" and first.alt == "Materials: vinyl"
    loaded = infoimages.load(ws)
    assert [i.name for i in loaded] == ["materials.jpg", "size-chart.png"]
    assert loaded[0].to_dict() == {"name": "materials.jpg", "alt": "Materials: vinyl",
                                   "listing_id": 1000013, "listing_image_id": 4401}
    assert loaded[1].listing_image_id is None
    index = json.loads(infoimages.index_path(ws).read_text(encoding="utf-8"))
    assert [e["file"] for e in index["images"]] == ["materials.jpg", "size-chart.png"]
    # Full size and byte for byte: a JPG or PNG Etsy takes is never re-encoded.
    assert (ws.root / INFO_DIR / "size-chart.png").read_bytes() == picture("PNG")
    assert infoimages.count(ws) == 2


def test_a_second_file_of_the_same_name_never_overwrites_the_first(ws):
    infoimages.add(ws, "card.jpg", picture(colour=(10, 10, 10)))
    second = infoimages.add(ws, "card.jpg", picture(colour=(250, 250, 250)))
    assert second.name == "card-2.jpg"
    assert [i.name for i in infoimages.load(ws)] == ["card.jpg", "card-2.jpg"]


def test_a_picture_etsy_would_refuse_is_kept_as_a_jpeg(ws):
    image = infoimages.add(ws, "install.webp", picture("WEBP"))
    assert image.name == "install.jpg"
    with Image.open(image.path) as opened:
        assert opened.format == "JPEG" and opened.size == (60, 40)


def test_a_name_that_says_another_type_gets_the_real_one_and_the_bytes_stay(ws):
    image = infoimages.add(ws, "sample.jpg", picture("PNG"))
    assert image.name == "sample.png" and image.path.read_bytes() == picture("PNG")


@pytest.mark.parametrize("name, data", [
    ("notes.jpg", b"not a picture"),
    ("empty.png", b""),
    ("notes.txt", picture()),
])
def test_what_is_not_a_picture_is_refused(ws, name, data):
    with pytest.raises(ValidationError):
        infoimages.add(ws, name, data)
    assert infoimages.load(ws) == []


def test_at_most_ten_info_images(ws):
    for n in range(infoimages.MAX_INFO_IMAGES):
        infoimages.add(ws, f"card-{n}.jpg", picture())
    with pytest.raises(infoimages.TooManyInfoImages):
        infoimages.add(ws, "one-too-many.jpg", picture())
    assert infoimages.count(ws) == infoimages.MAX_INFO_IMAGES


def test_a_new_order_keeps_each_pictures_facts_and_takes_the_others_out(ws):
    infoimages.add(ws, "a.jpg", picture(), alt="A", listing_id=1000013, listing_image_id=11)
    infoimages.add(ws, "b.jpg", picture(), alt="B")
    infoimages.add(ws, "c.jpg", picture(), listing_id=1000013, listing_image_id=13)
    kept = infoimages.set_order(ws, ["b.jpg", "a.jpg"])
    assert [(i.name, i.alt, i.listing_image_id) for i in kept] == [("b.jpg", "B", None),
                                                                   ("a.jpg", "A", 11)]
    # etsyotomasyon's copy of an Etsy photo is deleted; the listing keeps its own.
    assert not (ws.root / INFO_DIR / "c.jpg").exists()
    infoimages.remove(ws, "b.jpg")
    # The seller's own picture is never deleted: it goes to the archive.
    assert (ws.archive / INFO_DIR / "b.jpg").is_file()
    assert [i.name for i in infoimages.load(ws)] == ["a.jpg"]
    with pytest.raises(FileNotFoundError):
        infoimages.set_order(ws, ["a.jpg", "gone.jpg"])
    with pytest.raises(FileNotFoundError):
        infoimages.remove(ws, "gone.jpg")
    assert infoimages.find_etsy(ws, 11).name == "a.jpg" and infoimages.find_etsy(ws, 13) is None


def test_pictures_put_in_the_folder_by_hand_follow_in_name_order(ws):
    infoimages.add(ws, "z-listed.jpg", picture())
    folder = ws.root / INFO_DIR
    for name in ("10-care.jpg", "2-size.png", "1-materials.jpg"):
        (folder / name).write_bytes(picture())
    (folder / "notes.txt").write_text("not a picture", encoding="utf-8")
    assert [i.name for i in infoimages.load(ws)] == [
        "z-listed.jpg", "1-materials.jpg", "2-size.png", "10-care.jpg"]


def test_pictures_past_the_limit_are_never_used(ws):
    folder = ws.root / INFO_DIR
    folder.mkdir()
    for n in range(12):
        (folder / f"{n + 1:02d}.jpg").write_bytes(picture())
    assert [i.name for i in infoimages.load(ws)] == [f"{n + 1:02d}.jpg" for n in range(10)]
    assert [i.name for i in infoimages.unused(ws)] == ["11.jpg", "12.jpg"]
    # Taking one out lets the next one in; the extra ones are not deleted meanwhile.
    infoimages.remove(ws, "01.jpg")
    assert [i.name for i in infoimages.load(ws)][-1] == "11.jpg"
    assert [i.name for i in infoimages.unused(ws)] == ["12.jpg"]


def test_a_broken_index_falls_back_to_the_folder(ws):
    infoimages.add(ws, "b.jpg", picture())
    infoimages.add(ws, "a.jpg", picture())
    infoimages.index_path(ws).write_text("{not json", encoding="utf-8")
    assert [i.name for i in infoimages.load(ws)] == ["a.jpg", "b.jpg"]


def test_the_info_images_folder_belongs_to_its_workspace(ws):
    folder = ws.root / INFO_DIR
    folder.mkdir()
    assert root_for(folder) == ws.root
    assert "info-images/" in (ws.root / "README.txt").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- the limit


@pytest.mark.parametrize("info", range(0, infoimages.MAX_INFO_IMAGES + 1))
def test_mockups_flat_and_info_images_never_pass_etsys_twenty(info):
    cap = infoimages.mockup_cap(info)
    assert cap + 1 + info == MAX_LISTING_IMAGES == 20
    assert infoimages.mockup_cap(info, include_flat=False) == cap + 1


def test_the_limit_math():
    cards = list("abcde")
    assert infoimages.mockup_cap(0) == catalog.MAX_ENABLED == 19
    assert infoimages.mockup_cap(5) == 14
    assert infoimages.mockup_cap(40) == 0
    assert infoimages.fitting(15, cards) == cards  # 15 photos + 5 cards = 20
    assert infoimages.fitting(18, cards) == ["a", "b"]  # the first ones
    assert infoimages.fitting(20, cards) == []
    assert infoimages.fitting(0, cards) == cards


def test_info_images_shrink_the_mockups_a_draft_uses(ws):
    for n in range(19):
        Image.new("RGB", (30, 30), (200, 200, 200)).save(ws.mockups / f"m{n:02d}-tshirt.png")
    assert len(catalog.enabled_mockups(ws)) == 19
    for n in range(5):
        infoimages.add(ws, f"card-{n}.jpg", picture())
    use = catalog.usage(ws)
    assert use["max"] == 14 and use["info"] == 5
    assert use["used"] == [f"m{n:02d}-tshirt.png" for n in range(14)]
    assert use["over_limit"] == [f"m{n:02d}-tshirt.png" for n in range(14, 19)]
    assert len(catalog.enabled_mockups(ws)) == 14
    assert catalog.max_enabled(ws) == 14


# --------------------------------------------------------------------------- the CLI path


@pytest.fixture
def shirt(tmp_path):
    """A workspace with one product folder of two photos (drop auto's own test shape)."""
    ws = Workspace(tmp_path / "studio").create()
    folder = ws.products / "mountain sunset shirt"
    folder.mkdir()
    for name in ("2-back.png", "1-front.png"):
        Image.new("RGBA", (20, 20), (20, 30, 40, 100)).save(folder / name)
    template = Template(1000013, fields={"taxonomy_id": 1, "price": 20, "quantity": 5,
                                         "who_made": "i_did", "when_made": "made_to_order",
                                         "type": "physical"}, description="Cotton shirt.")
    ws.write_template(template.to_dict())
    return ws, template


class AutoClient:
    """drop auto's calls, with the alt text each picture goes up with."""

    def __init__(self):
        self.creates = 0
        self.images: list[tuple[str, int, str]] = []

    def shop_id(self):
        return 123

    def listing_inventory(self, listing_id):
        return {"products": [{"property_values": [], "offerings": [
            {"price": {"amount": 2000, "divisor": 100}, "quantity": 5, "is_enabled": True}]}]}

    def search_active_listings(self, **kwargs):
        return iter([])

    def create_draft_listing(self, fields):
        self.creates += 1
        return {"listing_id": 900}

    def upload_listing_image(self, listing_id, image, *, rank, alt_text=""):
        self.images.append((image.name, rank, alt_text))
        return {"listing_image_id": 5000 + rank}


def test_pipeline_rows_end_with_copies_of_the_info_images(shirt):
    ws, template = shirt
    infoimages.add(ws, "materials.jpg", picture(), alt="Materials")
    infoimages.add(ws, "care.png", picture("PNG"))
    report = pipeline.run(ws, template)
    row = report.ready[0]
    assert [p.name for p in row.images] == ["1-front.png", "2-back.png", "materials.jpg",
                                            "care.png"]
    # The batch keeps what it sends: review.csv points at copies in its own folder.
    copies = row.images[2:]
    assert all(p.parent == report.out_dir / INFO_DIR for p in copies)
    assert copies[1].read_bytes() == (ws.root / INFO_DIR / "care.png").read_bytes()
    assert [(i.name, i.alt) for i in report.info_images] == [("materials.jpg", "Materials"),
                                                             ("care.png", "")]
    assert "info-images/care.png" in report.csv_path.read_text(encoding="utf-8")


def test_a_product_folder_with_many_photos_gets_the_first_info_images_that_fit(shirt):
    ws, template = shirt
    folder = ws.products / "mountain sunset shirt"
    for n in range(3, 19):
        Image.new("RGB", (20, 20), (n, n, n)).save(folder / f"{n}-extra.png")
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        infoimages.add(ws, name, picture())
    row = pipeline.run(ws, template).ready[0]
    assert len(row.images) == MAX_LISTING_IMAGES
    assert [p.name for p in row.images[-2:]] == ["a.jpg", "b.jpg"]
    assert any("only 2 of the 3 info images fit" in w for w in row.warnings)


def test_too_many_mockups_beside_the_info_images_is_refused_before_any_work(shirt):
    ws, template = shirt
    for n in range(16):
        Image.new("RGB", (30, 30), (200, 200, 200)).save(ws.mockups / f"m{n:02d}.png")
    for n in range(5):
        infoimages.add(ws, f"card-{n}.jpg", picture())
    _artwork(ws.products / "retro-mountain-sunset.png")
    with pytest.raises(ValidationError, match=r"plus 5 info image\(s\) is 22 images"):
        pipeline.run(ws, template, mockups=ws.mockup_files())


def test_drop_auto_ends_the_draft_with_the_info_images_and_their_alt_texts(shirt):
    ws, template = shirt
    infoimages.add(ws, "materials.jpg", picture(), alt="Materials: 100% cotton")
    infoimages.add(ws, "care.png", picture("PNG"))
    client = AutoClient()
    report = automation.run(ws, template, client=client)
    assert report.uploaded.created == 1
    assert client.images == [("1-front.png", 1, ""), ("2-back.png", 2, ""),
                             ("materials.jpg", 3, "Materials: 100% cotton"), ("care.png", 4, "")]
    entry = json.loads(automation.history_path(ws.root).read_text(encoding="utf-8"))["123"][
        "mountain sunset shirt"]
    assert entry["info_images"] == ["materials.jpg", "care.png"]
    assert entry["images"] == {"5001": "1-front.png", "5002": "2-back.png",
                               "5003": "materials.jpg", "5004": "care.png"}


def test_drop_auto_without_info_images_records_none(shirt):
    ws, template = shirt
    client = AutoClient()
    automation.run(ws, template, client=client)
    entry = json.loads(automation.history_path(ws.root).read_text(encoding="utf-8"))["123"][
        "mountain sunset shirt"]
    assert "info_images" not in entry and len(client.images) == 2


def test_cli_drop_run_names_the_info_images_and_counts_them(shirt, monkeypatch):
    ws, _ = shirt
    infoimages.add(ws, "materials.jpg", picture())
    infoimages.add(ws, "care.png", picture("PNG"))
    seen: dict = {}

    def fake_run(workspace, *args, **kwargs):
        seen.update(kwargs)
        return pipeline.DropReport(batch="b", out_dir=workspace.drafts / "b")

    monkeypatch.setattr(pipeline, "run", fake_run)
    result = CliRunner().invoke(app, ["drop", "run", "--path", str(ws.root)])
    assert [i.name for i in seen["info_images"]] == ["materials.jpg", "care.png"]
    assert "Info images: 2" in result.output and "materials.jpg, care.png" in result.output
    assert "about 3 image(s) each" in result.output  # no mockup, the flat design, 2 cards


def test_cli_drop_auto_says_so_too(shirt):
    ws, _ = shirt
    infoimages.add(ws, "materials.jpg", picture())
    result = CliRunner().invoke(app, ["drop", "auto", "--path", str(ws.root), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Info images: 1" in result.output


# --------------------------------------------------------------------------- the app path


def test_every_draft_ends_with_the_info_images_in_order(studio):  # noqa: F811
    ws, template = studio
    infoimages.add(ws, "materials.jpg", picture(), alt="Materials: 100% cotton")
    infoimages.add(ws, "size-chart.png", picture("PNG"))
    _artwork(ws.products / "retro-mountain-sunset.png")
    client = StreamClient(ws)
    events = Events()
    report = _run(ws, template, client, on_event=events)

    item = report.items[0]
    assert item.status == stream.OK
    names = [name for _listing, name, _rank in client.images]
    assert len(names) == 5 and names[3:] == ["materials.jpg", "size-chart.png"]
    assert [rank for *_x, rank in client.images] == [1, 2, 3, 4, 5]
    assert client.alts["materials.jpg"] == "Materials: 100% cotton"
    assert client.alts["size-chart.png"] == ""
    # The product's own pictures (what the screen shows as mockups) and the info images
    # are kept apart; the info images sent are the run's copies.
    assert len(item.images) == 3 and all("info-images" not in str(p) for p in item.images)
    assert all(p.parent.name == INFO_DIR and ws.drafts in p.parents for p in item.info)
    entry = _history(ws)["123"]["retro-mountain-sunset.png"]
    assert entry["info_images"] == ["materials.jpg", "size-chart.png"]
    assert entry["images_total"] == 5
    outcome = events.outcome("retro-mountain-sunset.png")
    assert [p.rsplit("/", 1)[-1] for p in outcome["info_images"]] == ["materials.jpg",
                                                                     "size-chart.png"]
    batch = next(data for _n, step, _s, data in events.items if step == "batch")
    assert batch["info_images"] == ["materials.jpg", "size-chart.png"]


def test_a_folder_whose_photos_leave_little_room_gets_the_first_ones_and_a_warning(studio):  # noqa: F811
    ws, template = studio
    folder = ws.products / "desert cactus print"
    folder.mkdir()
    for n in range(18):
        _photo(folder / f"{n + 1:02d}.jpg")
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        infoimages.add(ws, name, picture())
    client = StreamClient(ws)
    item = _run(ws, template, client).items[0]
    assert [name for _l, name, _r in client.images][-2:] == ["a.jpg", "b.jpg"]
    assert len(client.images) == MAX_LISTING_IMAGES
    cut = [w for w in item.warnings if w.code == "info_images_cut"]
    assert cut and cut[0].params == {"n": 2, "total": 3, "max": 20}


def test_a_broken_info_image_stops_the_run_before_anything_is_sent(studio):  # noqa: F811
    ws, template = studio
    folder = ws.root / INFO_DIR
    folder.mkdir()
    (folder / "broken.jpg").write_bytes(b"\xff\xd8 cut short")
    _artwork(ws.products / "retro-mountain-sunset.png")
    client = StreamClient(ws)
    with pytest.raises(ValidationError, match="info image broken.jpg cannot be used"):
        _run(ws, template, client)
    assert client.creates == [] and client.images == []


def test_mockups_and_info_images_over_twenty_are_refused(studio):  # noqa: F811
    ws, template = studio
    for n in range(12):
        _photo(ws.mockups / f"extra-{n:02d}.jpg")
    for n in range(8):
        infoimages.add(ws, f"card-{n}.jpg", picture())
    _artwork(ws.products / "retro-mountain-sunset.png")
    with pytest.raises(ValidationError, match=r"plus 8 info image\(s\) is 23 images"):
        _run(ws, template, StreamClient(ws), mockups=ws.mockup_files()[:14])
    # The app's own selection never gets there: the mockups shrink instead.
    report = _run(ws, template, StreamClient(ws))
    assert report.items[0].status == stream.OK


def test_a_dry_run_counts_them_and_sends_nothing(studio):  # noqa: F811
    ws, template = studio
    infoimages.add(ws, "materials.jpg", picture())
    _artwork(ws.products / "retro-mountain-sunset.png")
    report = _run(ws, template, None, dry_run=True)
    item = report.items[0]
    assert item.status == stream.CHECKED and len(item.info) == 1
    assert "materials.jpg" in report.csv_path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- the client


CDN = "https://i.etsystatic.com/12345678/r/il/0a1b2c/4401/il_fullxfull.4401_ab12.jpg"


def test_a_listing_photo_is_read_from_etsys_image_cdn_without_the_keys():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=b"\xff\xd8picture")

    assert make_client(handler).download_image(CDN) == b"\xff\xd8picture"
    (request,) = seen
    assert request.url.host == "i.etsystatic.com"
    assert "x-api-key" not in request.headers and "authorization" not in request.headers


@pytest.mark.parametrize("url", [
    "http://i.etsystatic.com/x.jpg",
    "https://example.com/x.jpg",
    "https://i.etsystatic.com.example.com/x.jpg",
    "https://example.com/?u=i.etsystatic.com",
    "",
    None,
])
def test_no_other_address_is_ever_fetched(url):
    seen: list[httpx.Request] = []
    client = make_client(lambda request: seen.append(request) or httpx.Response(200))
    with pytest.raises(ValidationError):
        client.download_image(url)
    assert seen == [] and not client_mod.is_etsy_image_url(url)


def test_a_picture_over_the_limit_is_refused_while_it_streams():
    client = make_client(lambda request: httpx.Response(200, content=b"x" * 5000))
    with pytest.raises(ValidationError, match="larger than"):
        client.download_image(CDN, max_bytes=1000)


def test_a_server_error_is_retried_and_a_missing_picture_is_not(monkeypatch):
    monkeypatch.setattr(client_mod.time, "sleep", lambda seconds: None)
    answers = iter([httpx.Response(503), httpx.Response(200, content=b"ok")])
    assert make_client(lambda request: next(answers)).download_image(CDN) == b"ok"
    seen: list[httpx.Request] = []
    client = make_client(lambda request: seen.append(request) or httpx.Response(404))
    with pytest.raises(EtsyApiError) as caught:
        client.download_image(CDN)
    assert caught.value.status == 404 and len(seen) == 1


def test_a_picture_goes_up_with_up_to_500_characters_of_alt_text_and_no_watermark(tmp_path):
    handler, seen = recorder({"/shops/12345678/listings/1000001/images": {"listing_image_id": 1}})
    path = tmp_path / "card.jpg"
    path.write_bytes(picture())
    make_client(handler).upload_listing_image(1000001, path, rank=3, alt_text="a" * 600)
    parts = _parts(seen[0])
    assert parts["alt_text"][1] == b"a" * 500 and parts["rank"][1] == b"3"
    assert "is_watermarked" not in parts
