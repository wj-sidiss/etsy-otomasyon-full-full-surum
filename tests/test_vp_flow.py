"""The usage flow the product video promises, where the app used to fall short.

- A design saved without transparency is never uploaded silently as a "photo": the row
  says so, and the seller may have one on a solid background placed on the mockups.
- Every picture a draft gets carries an alt text naming its product and colour.
- Pictures that would look soft on Etsy are warned about before the draft exists.
- The start card's facts: opaque files, a template whose product the main mockup does
  not show, and a digital template's mockups.

All data is invented.
"""

from __future__ import annotations

import io
import json

import pytest
from PIL import Image, ImageDraw
from test_drop_stream import SHOP, Client, _artwork, _history, _photo, _run, studio  # noqa: F401
from test_web_designs import TEMPLATE_ID, _digital_template, _png, _put, _setup_shop
from web_helpers import wait_for_job

from etsyotomasyon.drop import mockup, stream
from etsyotomasyon.drop.template import Template
from etsyotomasyon.web.api import designs

# --- a design saved without transparency ---------------------------------------------------------


def _on_white(path, size=60):
    """A design exported on a white background: a red square, white all round."""
    image = Image.new("RGB", (size, size), (255, 255, 255))
    ImageDraw.Draw(image).rectangle((size // 4, size // 4, size * 3 // 4, size * 3 // 4),
                                    fill=(200, 40, 40))
    image.save(path)
    return path


def _real_photo(path, size=60):
    """A photo: no border is one colour."""
    Image.effect_noise((size, size), 80).convert("RGB").save(path, quality=95)
    return path


def test_an_opaque_design_goes_up_as_it_is_and_the_row_says_so(studio):  # noqa: F811
    ws, template = studio
    _on_white(ws.products / "sunny-lemon-garden.png")
    client = Client(ws)
    report = _run(ws, template, client)
    item = report.items[0]
    assert item.status == stream.OK and item.mode == "as_is"
    assert [(w.code, w.step) for w in item.warnings] == [("as_is", "mockup")]
    assert item.warnings[0].params == {"name": "sunny-lemon-garden.png"}
    assert item.steps["mockup"] == stream.WARN  # never a plain "done"
    assert report.warnings == 1
    assert [name for _id, name, _rank in client.images] == ["sunny-lemon-garden.png"]


def test_a_design_on_a_solid_ground_is_placed_when_the_seller_asks(studio, monkeypatch):  # noqa: F811
    ws, template = studio
    _on_white(ws.products / "sunny-lemon-garden.png")
    _real_photo(ws.products / "ocean-waves-photo.jpg")
    placed: list = []
    real_compose = mockup.compose

    def spy(design, template_image, out, **kw):
        placed.append(design)
        return real_compose(design, template_image, out, **kw)

    monkeypatch.setattr(mockup, "compose", spy)
    report = _run(ws, template, Client(ws), opaque=stream.OPAQUE_PLACE, dry_run=True)
    by_name = {item.name: item for item in report.items}
    lemon = by_name["sunny-lemon-garden.png"]
    assert lemon.mode == "composited" and len(lemon.images) == 3  # 2 mockups + flat
    assert not [w for w in lemon.warnings if w.code == "as_is"]
    # The mockups got a copy with its white ground see-through, the red square kept.
    keyed = placed[0]
    assert keyed != ws.products / "sunny-lemon-garden.png" and keyed.suffix == ".png"
    with Image.open(keyed) as image:
        assert image.mode == "RGBA"
        assert image.getpixel((1, 1))[3] == 0 and image.getpixel((30, 30)) == (200, 40, 40, 255)
    # A real photo (no flat border) stays a photo even then.
    photo = by_name["ocean-waves-photo.jpg"]
    assert photo.mode == "as_is" and [w.code for w in photo.warnings][:1] == ["as_is"]


def test_the_ground_is_found_only_on_a_flat_border(tmp_path):
    assert mockup.ground_colour(_on_white(tmp_path / "a.png")) == (255, 255, 255)
    assert mockup.ground_colour(_real_photo(tmp_path / "b.jpg")) is None
    assert mockup.ground_colour(_artwork(tmp_path / "c.png")) is None  # transparent already


def test_white_inside_the_design_stays_when_the_ground_goes(tmp_path):
    image = Image.new("RGB", (300, 300), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.ellipse((50, 50, 250, 250), fill=(30, 90, 200))
    draw.ellipse((130, 130, 170, 170), fill=(255, 255, 255))  # an eye: white, enclosed
    image.save(tmp_path / "face.png")
    out = mockup.remove_ground(tmp_path / "face.png", tmp_path / "face-t.png", (255, 255, 255))
    with Image.open(out) as keyed:
        assert keyed.getpixel((5, 5))[3] == 0
        assert keyed.getpixel((150, 150)) == (255, 255, 255, 255)
        assert keyed.getpixel((80, 150)) == (30, 90, 200, 255)


def test_an_unknown_choice_is_refused(studio):  # noqa: F811
    ws, template = studio
    with pytest.raises(ValueError):
        stream.run_stream(ws, template, Client(ws), mockups=[], opaque="maybe")


# --- alt texts ------------------------------------------------------------------------------------


def test_every_picture_says_what_it_shows(studio):  # noqa: F811
    ws, template = studio
    (ws.mockups / "mockups.json").write_text(json.dumps({
        "tshirt-white.jpg": {"type": "tshirt", "color": "Beyaz", "enabled": True, "order": 0},
        "mug-white.jpg": {"type": "mug", "color": "Meşe çerçeve", "enabled": True, "order": 1},
    }), encoding="utf-8")
    _artwork(ws.products / "retro-mountain-sunset.png")
    _photo(ws.products / "ocean-waves-photo.jpg")
    client = Client(ws)
    report = _run(ws, template, client)
    assert all(item.status == stream.OK for item in report.items)
    alts = [client.alts[name] for _id, name, _rank in client.images]
    assert alts == [
        "Ocean Waves Photo",  # a seller's own photo: its concept only
        "Retro Mountain Sunset t-shirt, white",
        "Retro Mountain Sunset mug, oak frame",
        "Retro Mountain Sunset design",
    ]
    assert all(len(alt) <= stream.ALT_TEXT_MAX for alt in alts)


def test_an_alt_text_never_carries_words_it_cannot_put_in_english():
    assert stream.alt_text("cat mom club", "tote", "Füme") == "Cat Mom Club tote bag"
    assert stream.alt_text("cat mom club", "tote", "Heather Grey") == "Cat Mom Club tote bag, gray"
    assert stream.alt_text("planner", flat=True, digital=True) == "Planner digital download preview"
    assert len(stream.alt_text("word " * 100, "mug", "Beyaz")) <= stream.ALT_TEXT_MAX


# --- pictures that would look soft ----------------------------------------------------------------


def test_small_pictures_are_warned_about_before_the_draft(studio, monkeypatch):  # noqa: F811
    ws, template = studio
    monkeypatch.setattr(stream, "SMALL_IMAGE_EDGE", 1000)
    monkeypatch.setattr(mockup, "MAX_UPSCALE", 2.0)
    _photo(ws.products / "tiny-red-square.jpg")  # 40 px, up as it is
    _artwork(ws.products / "retro-mountain-sunset.png")  # 40 px, onto 2000 px mockups
    report = _run(ws, template, Client(ws), dry_run=True)
    by_name = {item.name: item for item in report.items}
    tiny = by_name["tiny-red-square.jpg"]
    small = [w for w in tiny.warnings if w.code == "small_image"]
    assert [(w.step, w.params["name"], w.params["px"]) for w in small] == [
        ("check", "tiny-red-square.jpg", 40)]
    assert tiny.status == stream.CHECKED and tiny.steps["check"] == stream.WARN
    art = by_name["retro-mountain-sunset.png"]
    soft = [w for w in art.warnings if w.code == "design_small"]
    assert len(soft) == 1 and soft[0].params["factor"] > 2 and soft[0].step == "check"


def test_a_big_enough_picture_is_not_warned_about(tmp_path):
    assert mockup.upscale_factor((4500, 5400), (2000, 2000)) < 1
    assert mockup.upscale_factor((600, 600), (2000, 2000)) == pytest.approx(1.2)
    assert mockup.upscale_factor((300, 300), None) > mockup.MAX_UPSCALE


# --- the start card's facts (web) -----------------------------------------------------------------


def _white_ground_png() -> bytes:
    buffer = io.BytesIO()
    image = Image.new("RGB", (60, 60), (255, 255, 255))
    ImageDraw.Draw(image).rectangle((15, 15, 45, 45), fill=(200, 40, 40))
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _noise_jpg() -> bytes:
    buffer = io.BytesIO()
    Image.effect_noise((60, 60), 80).convert("RGB").save(buffer, "JPEG")
    return buffer.getvalue()


def test_the_pending_view_tells_each_designs_background(web):
    _setup_shop(web)
    _put(web, "retro-mountain-sunset.png", _png())
    _put(web, "sunny-lemon-garden.png", _white_ground_png())
    _put(web, "ocean-waves-photo.jpg", _noise_jpg())
    data = web.client.get("/api/designs/pending").json()
    grounds = {item["name"]: item["ground"] for item in data["items"]}
    assert grounds == {"retro-mountain-sunset.png": "transparent",
                       "sunny-lemon-garden.png": "flat", "ocean-waves-photo.jpg": "photo"}
    assert data["opaque"] == {"flat": 1, "photo": 1, "unknown": 0}


def test_the_start_takes_the_sellers_choice_for_opaque_designs(web, monkeypatch):
    _setup_shop(web)
    _put(web, "sunny-lemon-garden.png", _white_ground_png())
    bad = web.client.post("/api/designs/start", json={"opaque": "sometimes"})
    assert bad.status_code == 422 and bad.json()["error"]["params"]["field"] == "opaque"
    seen: dict = {}

    def fake_run(ws, template, client, **kw):
        seen.update(kw)
        raise stream.TemplateGone("stop here")

    monkeypatch.setattr(stream, "run_stream", fake_run)
    job = web.client.post("/api/designs/start", json={"opaque": "place", "dry_run": True})
    assert job.status_code == 200, job.text
    wait_for_job(web, job.json()["id"], timeout=20)
    assert seen["opaque"] == "place" and seen["dry_run"] is True
    assert designs.GROUND_BUDGET > 0


def test_a_template_whose_product_the_main_mockup_does_not_show_is_known(web):
    _fake, ws = _setup_shop(web)  # mockups: mug-white.jpg first, then tshirt-white.jpg
    ws.write_template(Template(TEMPLATE_ID, source_title="Retro Mountain Sunset Shirt", fields={
        "taxonomy_id": 482, "price": 21.0, "quantity": 10, "who_made": "i_did",
        "when_made": "made_to_order", "type": "physical", "shipping_profile_id": 5551,
    }, description="Soft cotton tee.", tags=["gift"]).to_dict())
    _put(web, "retro-mountain-sunset.png", _png())
    data = web.client.get("/api/designs/pending").json()
    assert data["template"]["product"] == "tshirt"  # from the title: no taxonomy cached
    assert data["mockups"]["main"] == "mug-white.jpg"
    assert data["mockups"]["main_type"] == "mug"


def test_a_download_template_uses_only_mockups_that_show_no_physical_product(web, monkeypatch):
    _fake, ws = _setup_shop(web, mockups=2)
    Image.new("RGB", (40, 40), (220, 200, 180)).save(ws.mockups / "poster-oak-frame.jpg")
    _digital_template(ws)
    _put(web, "retro-mountain-sunset.png", _png())
    data = web.client.get("/api/designs/pending").json()
    mk = data["mockups"]
    assert mk["names"] == ["poster-oak-frame.jpg"] and mk["main"] == "poster-oak-frame.jpg"
    assert [m["type"] for m in mk["left_out"]] == ["mug", "tshirt"]
    assert mk["enabled"] == 1 and data["images_each"] == 2
    seen: dict = {}

    def fake_run(ws, template, client, **kw):
        seen.update(kw)
        raise stream.TemplateGone("stop here")

    monkeypatch.setattr(stream, "run_stream", fake_run)
    job = web.client.post("/api/designs/start", json={"dry_run": True})
    assert job.status_code == 200, job.text
    wait_for_job(web, job.json()["id"], timeout=20)
    assert [p.name for p in seen["mockups"]] == ["poster-oak-frame.jpg"]


def test_every_warning_the_run_gives_has_words_in_both_languages():
    import re
    from pathlib import Path

    source = Path(stream.__file__).read_text(encoding="utf-8")
    codes = set(re.findall(r'self\._warn\(\s*item,\s*"([a-z_]+)"', source))
    # The check step's notes from listings.prepare, named in a loop (code = "...").
    notes = source[source.index("for message in prepared.result.warnings"):]
    notes = notes[:notes.index("self._warn(item, code")]
    codes |= set(re.findall(r'code = "([a-z_]+)"', notes))
    assert {"as_is", "small_image", "design_small", "measure_not_sent", "check_warning"} <= codes
    strings = json.loads((Path(designs.__file__).parents[1] / "static" / "i18n" / "designs.json")
                         .read_text(encoding="utf-8"))
    for lang in ("tr", "en"):
        missing = sorted(code for code in codes if f"warn.{code}" not in strings[lang])
        assert not missing, (lang, missing)
