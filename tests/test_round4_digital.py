"""Round 4: digital drafts — the download is never a photo, its folder is read whole,
made-to-order needs no file, and an image failure still says (and sends) the download."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import test_drop_stream
from PIL import Image
from test_client_extra import PREFIX, SHOP, make_client
from test_drop_stream import Client, _artwork, _photo, _run

from etsyotomasyon import listings
from etsyotomasyon.drop import mockup, pipeline, stream
from etsyotomasyon.drop.template import Template
from etsyotomasyon.drop.workspace import Workspace

# The drop.stream tests' workspace: two mockups, a physical template, tiny renders.
studio = test_drop_stream.studio


def _template(ws, template=None, *, listing_type="download", when_made="2020_2026",
              title="Printable Wall Art"):
    base = template.fields if template is not None else {
        "taxonomy_id": 1, "price": 4.5, "quantity": 999, "who_made": "i_did"}
    fields = dict(base, type=listing_type, when_made=when_made)
    if listing_type == "download":
        fields.pop("shipping_profile_id", None)
    made = Template(1000001, source_title=title, fields=fields,
                    description="Printable art.", tags=["printable wall art"])
    ws.write_template(made.to_dict())
    return made


def _opaque(path, size=(60, 75)):
    """A printable as sellers export it: an opaque JPG, no transparency at all."""
    Image.new("RGB", size, (180, 120, 90)).save(path, "JPEG")
    return path


def _folder(ws, name, photos=("01-front.jpg",), files=None, sub="dosyalar"):
    folder = ws.products / name
    folder.mkdir()
    for photo in photos:
        _photo(folder / photo)
    for rel, data in (files or {}).items():
        target = folder / sub / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    if files == {}:
        (folder / sub).mkdir()
    return folder


@pytest.fixture
def flats(monkeypatch):
    """The edge every flat render is asked for (the studio fixture's renders are tiny)."""
    edges: list[int] = []
    small = mockup.flatten_design

    def record(design, out, **kw):
        edges.append(kw.get("edge", mockup.OUTPUT_MIN_EDGE))
        return small(design, out, **kw)

    monkeypatch.setattr(mockup, "flatten_design", record)
    return edges


# --- etsy-1 / pipe-1: the file being sold is never a listing photo -----------------------------


def test_an_opaque_printable_goes_onto_the_mockups_never_up_as_it_is(studio, flats):
    ws, template = studio
    template = _template(ws, template)
    design = _opaque(ws.products / "boho-sunset-wall-art.jpg")
    client = Client(ws)
    item = _run(ws, template, client).items[0]

    assert item.status == stream.OK and item.mode == "composited"
    assert design not in item.images and len(item.images) == 3  # 2 mockups + a preview
    assert all(name != design.name for _id, name, _rank in client.images)
    assert client.files == [(1000001, design.name, 1)], "the original is the download"
    assert flats == [mockup.DIGITAL_PREVIEW_EDGE] and mockup.DIGITAL_PREVIEW_EDGE <= 1200


def test_a_physical_opaque_photo_still_goes_up_as_it_is(studio, flats):
    ws, template = studio
    design = _opaque(ws.products / "ocean-waves-photo.jpg")
    item = _run(ws, template, Client(ws)).items[0]
    assert item.mode == "as_is" and item.images == [design] and flats == []


def test_a_transparent_designs_flat_is_a_small_preview_when_it_is_digital(studio, flats):
    ws, template = studio
    _artwork(ws.products / "retro-mountain-sunset.png")
    _run(ws, template, Client(ws))
    assert flats == [mockup.OUTPUT_MIN_EDGE], "physical: the full 2000 px render"
    digital = _template(ws, template)
    (ws.root / "upload-history.json").unlink()
    flats.clear()
    _run(ws, digital, Client(ws))
    assert flats == [mockup.DIGITAL_PREVIEW_EDGE]


def test_a_photo_that_is_the_download_fails_the_check(studio, monkeypatch):
    # The guard behind the rule: whatever makes a listing photo also the download (here a
    # stand-in for a future route), the product stops before its draft exists.
    ws, template = studio
    template = _template(ws, template)
    design = _opaque(ws.products / "boho-sunset-wall-art.jpg")
    real = pipeline.deliverables

    def photos_as_downloads(source, **kw):
        if source != design:
            return real(source, **kw)
        return sorted(ws.drafts.rglob("*--tshirt-white.jpg")), None

    monkeypatch.setattr(pipeline, "deliverables", photos_as_downloads)
    client = Client(ws)
    item = _run(ws, template, client).items[0]
    assert item.status == stream.FAILED and item.error.code == "download_is_photo"
    assert item.error.step == "check" and client.creates == []


def test_photo_is_download_names_the_file():
    a = Path("x") / "art.jpg"
    assert pipeline.photo_is_download([a], [Path("y.pdf")]) is None
    code, message, params = pipeline.photo_is_download([a], [a])
    assert code == "download_is_photo" and params == {"name": "art.jpg"}
    assert "listing page" in message


def _pipeline_ws(tmp_path, monkeypatch):
    monkeypatch.setenv("ETSYOTOMASYON_HOME", str(tmp_path / "home"))
    ws = Workspace(tmp_path / "studio").create()
    Image.new("RGB", (400, 400), (240, 240, 240)).save(ws.mockups / "frame-oak.jpg")
    return ws


def test_drop_run_composites_an_opaque_printable_and_attaches_the_original(tmp_path, monkeypatch):
    ws = _pipeline_ws(tmp_path, monkeypatch)
    design = _opaque(ws.products / "boho-sunset-wall-art.jpg", size=(2400, 3000))
    template = _template(ws)
    report = pipeline.run(ws, template, client=None, use_cache=False,
                          mockups=ws.mockup_files())
    [row] = report.rows
    assert row.ok and row.files == [design]
    assert design not in row.images and len(row.images) == 2
    flat = next(p for p in row.images if "--flat" in p.name)
    with Image.open(flat) as image:
        assert max(image.size) <= mockup.DIGITAL_PREVIEW_EDGE, image.size


def test_the_pending_estimate_counts_mockups_for_a_digital_jpg(web):
    from test_web_designs import _jpg, _photo_jpg, _put, _setup_shop

    _fake, ws = _setup_shop(web)
    (ws.mockups / "poster-oak-frame.jpg").write_bytes(_jpg())
    _put(web, "boho-sunset-wall-art.jpg", _photo_jpg())
    physical = web.client.get("/api/designs/pending").json()
    _template(ws)
    digital = web.client.get("/api/designs/pending").json()
    # Physical: the JPG is a finished photo (1 image). Digital: the poster mockup (the
    # T-shirt and mug show a physical product, so a download leaves them out) + the
    # preview, plus its download file; no inventory update is needed on either side here.
    assert digital["estimate_requests"] - physical["estimate_requests"] == (2 - 1) + 1


# --- etsy-2 / pipe-4: subfolders of dosyalar are refused, never silently left out ----------------


def test_subfolders_in_dosyalar_are_named_and_refused(tmp_path):
    folder = tmp_path / "svg bundle"
    for rel in ("license.pdf", "SVG/cat.svg", "PNG/cat.png", "__MACOSX/._cat.png",
                ".git/x", "empty/.keep"):
        path = folder / "dosyalar" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    files, issue = pipeline.deliverables(folder)
    assert [p.name for p in files] == ["license.pdf"]
    code, message, params = issue
    assert code == "nested_files"
    assert params == {"name": "svg bundle", "folder": "dosyalar", "folders": "PNG, SVG", "n": 2}
    assert "zip" in message


def test_a_dosyalar_with_only_subfolders_is_not_called_empty(tmp_path):
    folder = tmp_path / "wall art set"
    for rel in ("A4/art-a4.pdf", "Letter/art-letter.pdf"):
        path = folder / "Dosyalar" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF")
    code, message, params = pipeline.deliverables(folder)[1]
    assert code == "nested_files" and "empty" not in message
    assert params["folder"] == "Dosyalar" and params["folders"] == "A4, Letter"
    (tmp_path / "blank" / "dosyalar").mkdir(parents=True)
    code, message, params = pipeline.deliverables(tmp_path / "blank")[1]
    assert code == "no_deliverable" and params["missing"] is False and "is empty" in message


def test_a_run_stops_a_bundle_with_subfolders_at_the_check(studio):
    ws, template = studio
    template = _template(ws, template)
    _folder(ws, "svg bundle", files={"license.pdf": b"%PDF", "SVG/cat.svg": b"<svg/>"})
    client = Client(ws)
    item = _run(ws, template, client).items[0]
    assert item.status == stream.FAILED and item.error.code == "nested_files"
    assert item.error.step == "check" and client.creates == [] and client.files == []


def test_the_pending_view_says_subfolders_and_empty_apart(web):
    from test_web_designs import _jpg, _put, _setup_shop

    _fake, ws = _setup_shop(web)
    _template(ws)
    _put(web, "svg bundle/01-front.jpg", _jpg(), batch="b1")
    _put(web, "svg bundle/dosyalar/license.pdf", b"%PDF", batch="b1")
    (ws.products / "svg bundle" / "dosyalar" / "SVG").mkdir()
    (ws.products / "svg bundle" / "dosyalar" / "SVG" / "cat.svg").write_bytes(b"<svg/>")
    _put(web, "blank set/01-front.jpg", _jpg((9, 9, 9)), batch="b2")
    (ws.products / "blank set" / "dosyalar").mkdir()
    items = {i["name"]: i for i in web.client.get("/api/designs/pending").json()["items"]}
    assert items["svg bundle"]["deliverable_problem"]["code"] == "nested_files"
    assert items["svg bundle"]["deliverable_problem"]["params"]["folders"] == "SVG"
    assert items["blank set"]["deliverable_problem"] == {
        "code": "no_deliverable",
        "params": {"name": "blank set", "folder": "dosyalar", "missing": False}}


# --- etsy-3: a made-to-order digital template needs no download file ---------------------------


def test_a_made_to_order_digital_run_needs_no_download(studio):
    ws, template = studio
    template = _template(ws, template, when_made="made_to_order", title="Custom Pet Portrait")
    _folder(ws, "custom pet portrait", photos=("01-example.jpg", "02-example.jpg"))
    _folder(ws, "custom invitation", files={"guide.pdf": b"%PDF"})
    _artwork(ws.products / "retro-mountain-sunset.png")
    client = Client(ws)
    report = _run(ws, template, client)
    by_name = {item.name: item for item in report.items}
    assert all(item.status == stream.OK for item in report.items), [
        (i.name, i.error) for i in report.items]
    portrait = by_name["custom pet portrait"]
    assert portrait.deliverables == []
    assert [w.code for w in portrait.warnings if w.code == "made_to_order_no_file"]
    loose = by_name["retro-mountain-sunset.png"]
    assert loose.deliverables == [], "a sample design is not what a custom buyer ordered"
    assert any(w.code == "made_to_order_no_file" for w in loose.warnings)
    assert client.files == [(by_name["custom invitation"].listing_id, "guide.pdf", 1)]
    assert not any("download file before" in w.message for i in report.items
                   for w in i.warnings)


def test_drop_auto_does_not_refuse_a_made_to_order_batch(studio):
    from etsyotomasyon.drop import automation

    ws, template = studio
    template = _template(ws, template, when_made="made_to_order")
    _folder(ws, "custom pet portrait")
    client = Client(ws)
    report = automation.run(ws, template, client=client)
    [row] = report.prepared.rows
    assert row.ok and row.files == [] and pipeline.MADE_TO_ORDER_NOTE in row.warnings
    assert report.uploaded.results[0].status == "ok" and client.files == []


def test_a_made_to_order_csv_row_is_not_told_it_needs_a_file(tmp_path):
    row = {"title": "Custom Pet Portrait", "description": "Sent within 3 days.",
           "price": "25", "quantity": "10", "who_made": "i_did",
           "when_made": "made_to_order", "taxonomy_id": "1", "type": "download"}
    prepared = listings.prepare([row], base_dir=tmp_path)[0]
    assert not prepared.result.failed
    assert not any("download file" in w for w in prepared.result.warnings)


def test_the_pending_view_of_a_made_to_order_template(web):
    from test_web_designs import _jpg, _put, _setup_shop

    _fake, ws = _setup_shop(web)
    _template(ws, when_made="made_to_order")
    _put(web, "custom pet portrait/01-example.jpg", _jpg(), batch="b1")
    data = web.client.get("/api/designs/pending").json()
    assert data["template"]["made_to_order"] is True
    [item] = data["items"]
    assert item["deliverable_problem"] is None and item["deliverables"] == []
    assert data["runnable"] == 1 and data["blockers"] == []


# --- etsy-4 / pipe-6: an image failure still sends the download, and says what happened ---------


def _push_download_row(tmp_path, fail_files=False):
    Image.new("RGB", (20, 20), (1, 2, 3)).save(tmp_path / "photo.jpg")
    (tmp_path / "planner.pdf").write_bytes(b"%PDF-1.4")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path[len(PREFIX):]
        calls.append(path)
        if path == f"/shops/{SHOP}/listings":
            return httpx.Response(201, json={"listing_id": 1000001})
        if path.endswith("/images"):
            return httpx.Response(400, json={"error": "image refused"})
        if fail_files:
            return httpx.Response(400, json={"error": "file refused"})
        return httpx.Response(201, json={"listing_file_id": 1, "listing_id": 1000001,
                                         "rank": 1, "filename": "planner.pdf"})

    row = {"title": "Boho Planner Printable", "description": "PDF planner.", "price": "4.5",
           "quantity": "999", "who_made": "i_did", "when_made": "2020_2026",
           "taxonomy_id": "2078", "type": "download", "images": "photo.jpg",
           "files": "planner.pdf"}
    result = listings.push(make_client(handler), [row], base_dir=tmp_path).results[0]
    return result, calls


def test_an_image_failure_still_attaches_the_download_and_says_so(tmp_path):
    result, calls = _push_download_row(tmp_path)
    assert result.status == "partial" and result.images_uploaded == 0
    assert result.files_uploaded == 1 and any(p.endswith("/files") for p in calls)
    assert "image 1 of 1 failed" in result.message
    assert "Its 1 download file was attached." in result.message


def test_an_image_failure_and_a_file_failure_are_both_said(tmp_path):
    result, _calls = _push_download_row(tmp_path, fail_files=True)
    assert result.status == "partial" and result.files_uploaded == 0
    assert "image 1 of 1 failed" in result.message
    assert "Download file 1 of 1 (planner.pdf) failed too" in result.message
    assert "before publishing" in result.message


def test_the_stream_names_a_missing_download_even_when_a_photo_failed(studio):
    ws, template = studio
    template = _template(ws, template)
    _folder(ws, "boho planner", files={"planner.pdf": b"%PDF"})
    client = Client(ws)
    real_image = client.upload_listing_image

    def fail_image(listing_id, image, *, rank, alt_text=""):
        if rank == 1:
            raise listings.EtsyApiError(400, "image refused", method="POST", path="/images")
        return real_image(listing_id, image, rank=rank)

    client.upload_listing_image = fail_image
    client.fail_file["planner"] = listings.EtsyApiError(400, "file refused", method="POST",
                                                         path="/files")
    item = _run(ws, template, client).items[0]
    assert item.status == stream.PARTIAL and item.files_uploaded == 0
    warning = next(w for w in item.warnings if w.code in ("partial", "partial_files"))
    assert warning.code == "partial_files" and warning.params["name"] == "planner.pdf"
    entry = json.loads((ws.root / "upload-history.json").read_text(encoding="utf-8"))
    assert entry["123"]["boho planner"]["status"] == "partial"
