"""Filigran: the seller's watermark (drop.watermark), stamped on every listing PHOTO a
draft uploads and never on the files buyers download, by the app's run (drop.stream) and
the CLI's (drop.pipeline / drop.automation), set on the Mockuplar page (web/api/watermark)
or with `etsyotomasyon drop watermark`."""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import httpx
import pytest
import test_drop_stream
from PIL import Image, ImageChops, ImageCms, ImageDraw
from test_client_extra import make_client
from test_drop_stream import SHOP, _artwork, _run
from test_round4_digital import _folder, _opaque, _template
from typer.testing import CliRunner

from etsyotomasyon.cli import app
from etsyotomasyon.drop import automation, catalog, mockup, pipeline, stream
from etsyotomasyon.drop import watermark as wm
from etsyotomasyon.drop.workspace import Workspace
from etsyotomasyon.errors import ValidationError
from etsyotomasyon.web.server import STATIC_DIR

studio = test_drop_stream.studio

WHITE = (255, 255, 255)


# --- helpers ------------------------------------------------------------------------------


def _mark(size=(200, 60), colour=(0, 0, 0, 255), hole=False) -> Image.Image:
    """An opaque bar (optionally with a see-through hole in its middle) on a clear ground."""
    image = Image.new("RGBA", size, colour)
    if hole:
        w, h = size
        ImageDraw.Draw(image).rectangle((w * 2 // 5, h // 5, w * 3 // 5, h * 4 // 5), fill=(0, 0, 0, 0))
    return image


def _png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _logo_bytes(size=(300, 90), margin=20) -> bytes:
    """A logo as sellers export it: dark text-like strokes on a transparent ground (a
    see-through gap between them), with clear margins around."""
    image = Image.new("RGBA", (size[0] + 2 * margin, size[1] + 2 * margin), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle((margin, margin, margin + size[0] - 1, margin + size[1] - 1), fill=(20, 30, 50, 255))
    draw.rectangle((margin + size[0] // 3, margin + size[1] // 3, margin + size[0] * 2 // 3,
                    margin + size[1] * 2 // 3), fill=(0, 0, 0, 0))
    return _png_bytes(image)


def _changed(base: Image.Image, out: Image.Image):
    """The box of pixels the stamp changed (None when nothing did)."""
    return ImageChops.difference(base.convert("RGB"), out.convert("RGB")).getbbox()


def _set_mark(ws: Workspace, **settings) -> None:
    wm.save_upload(ws, "logo.png", _logo_bytes())
    if settings:
        wm.update_settings(ws, settings)


def _stamped(path: Path) -> bool:
    return path.parent.name == wm.STAMPED_DIR


# --- rendering ---------------------------------------------------------------------------------


@pytest.mark.parametrize(("opacity", "expected"), [(10, 230), (35, 166), (90, 26)])
def test_opacity_is_the_share_of_the_mark_that_shows(opacity, expected):
    base = Image.new("RGB", (1000, 800), WHITE)
    out = wm.apply(base, _mark(), wm.Settings(position=wm.CENTER, opacity=opacity))
    grey = out.getpixel((500, 400))
    assert all(abs(channel - expected) <= 2 for channel in grey), grey


def test_the_marks_own_transparency_is_kept():
    base = Image.new("RGB", (1000, 800), WHITE)
    out = wm.apply(base, _mark(hole=True), wm.Settings(position=wm.CENTER, opacity=90))
    assert out.getpixel((500, 400)) == WHITE, "the hole in the mark stays see-through"
    assert out.getpixel((380, 400))[0] < 60, "the mark itself shows"


def test_center_is_centred_and_its_size_a_share_of_the_width():
    for width, height in ((1000, 800), (3000, 3000), (1200, 1600)):
        base = Image.new("RGB", (width, height), WHITE)
        out = wm.apply(base, _mark(), wm.Settings(position=wm.CENTER, size=30))
        x0, y0, x1, y1 = _changed(base, out)
        assert abs((x1 - x0) - width * 0.30) <= 2, (width, x1 - x0)
        assert abs((x0 + x1) / 2 - width / 2) <= 1.5 and abs((y0 + y1) / 2 - height / 2) <= 1.5


def test_corner_sits_in_the_bottom_right_margin():
    base = Image.new("RGB", (1000, 800), WHITE)
    out = wm.apply(base, _mark(), wm.Settings(position=wm.CORNER, size=20))
    x0, y0, x1, y1 = _changed(base, out)
    margin = round(800 * wm.CORNER_MARGIN)
    assert abs(x1 - (1000 - margin)) <= 1 and abs(y1 - (800 - margin)) <= 1
    assert x0 > 700 and y0 > 650
    assert abs((x1 - x0) - 200) <= 2


def test_tiled_covers_the_whole_photo():
    base = Image.new("RGB", (900, 600), WHITE)
    out = wm.apply(base, _mark(), wm.Settings(position=wm.TILED, tile_size=15, opacity=60))
    diff = ImageChops.difference(base, out).convert("L").point(lambda v: 255 if v else 0)
    for row in range(3):
        for col in range(3):
            cell = diff.crop((col * 300, row * 200, col * 300 + 300, row * 200 + 200))
            assert cell.getbbox() is not None, f"cell {row},{col} has no mark"
    covered = diff.histogram()[255] / (900 * 600)
    assert 0.1 < covered < 0.6, covered


def test_tiled_marks_follow_their_own_size():
    base = Image.new("RGB", (1000, 1000), WHITE)
    small = ImageChops.difference(base, wm.apply(base, _mark(), wm.Settings(position=wm.TILED, tile_size=5)))
    big = ImageChops.difference(base, wm.apply(base, _mark(), wm.Settings(position=wm.TILED, tile_size=40)))
    # Bigger marks mean fewer, larger ones: the same photo, a different pattern.
    assert small.getbbox() and big.getbbox() and small.tobytes() != big.tobytes()


@pytest.mark.parametrize("size", [(40, 30), (16, 16), (3000, 3000), (3000, 200), (200, 3000)])
@pytest.mark.parametrize("position", wm.POSITIONS)
def test_small_large_and_narrow_photos_keep_their_size(size, position):
    base = Image.new("RGB", size, WHITE)
    out = wm.apply(base, _mark(), wm.Settings(position=position))
    assert out.size == size and out.mode == "RGB"
    box = _changed(base, out)
    assert box is not None, "the mark is on it"
    if position != wm.TILED:
        # Never wider or taller than the photo (inside its margins): a strip gets a mark
        # that fits, not one cut off.
        x0, y0, x1, y1 = box
        assert x0 >= 0 and y0 >= 0 and x1 <= size[0] and y1 <= size[1]


@pytest.mark.parametrize(("mode", "expected"), [
    ("RGB", "RGB"), ("RGBA", "RGBA"), ("L", "RGB"), ("LA", "RGBA"), ("CMYK", "RGB"),
    ("P", "RGB"), ("P+transparency", "RGBA"), ("1", "RGB"),
])
def test_every_picture_mode_is_stamped(mode, expected):
    base = Image.new("RGB", (300, 200), (120, 160, 200))
    if mode == "P+transparency":
        base = base.convert("P")
        base.info["transparency"] = 0
    else:
        base = base.convert(mode)
    out = wm.apply(base, _mark(), wm.Settings())
    assert out.mode == expected and out.size == (300, 200)
    assert _changed(base, out) is not None


def test_a_see_through_photo_keeps_its_see_through_ground():
    base = Image.new("RGBA", (400, 300), (0, 0, 0, 0))
    out = wm.apply(base, _mark(), wm.Settings(position=wm.CENTER, opacity=50))
    assert out.mode == "RGBA"
    assert out.getpixel((2, 2))[3] == 0, "outside the mark the photo stays clear"
    assert out.getpixel((200, 150))[3] > 0


# --- a stamped copy --------------------------------------------------------------------------


def test_a_stamped_jpeg_is_upright_same_size_without_exif_profile_or_comment(tmp_path):
    source = tmp_path / "photo.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6  # turned a quarter: shown 600 x 800
    exif[0x010F] = "ExampleCam"
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    Image.new("RGB", (800, 600), (90, 120, 150)).save(source, "JPEG", exif=exif.tobytes(),
                                                      icc_profile=icc, comment=b"private")
    before = source.read_bytes()
    loaded = wm.Watermark(_mark(), wm.Settings())
    out = loaded.stamp(source, tmp_path / "out")
    assert source.read_bytes() == before, "the photo itself is never changed"
    assert out.parent == tmp_path / "out" and out.name == "photo.jpg"
    with Image.open(out) as image:
        assert image.format == "JPEG" and image.size == (600, 800)
        assert not image.getexif() and "icc_profile" not in image.info
        assert "comment" not in image.info


def test_a_png_keeps_its_transparency_and_a_gif_becomes_a_png(tmp_path):
    png = tmp_path / "front.png"
    Image.new("RGBA", (120, 80), (0, 0, 0, 0)).save(png)
    gif = tmp_path / "front.gif"
    Image.new("RGB", (120, 80), (10, 200, 10)).convert("P").save(gif)
    loaded = wm.Watermark(_mark(), wm.Settings())
    taken: set[str] = set()
    first = loaded.stamp(png, tmp_path / "out", taken)
    second = loaded.stamp(gif, tmp_path / "out", taken)
    assert first.name == "front.png" and second.name == "front-2.png", "a clash gets -2"
    with Image.open(first) as image:
        assert image.format == "PNG" and image.mode == "RGBA" and image.getpixel((1, 1))[3] == 0
    with Image.open(second) as image:
        assert image.format == "PNG" and image.size == (120, 80)


# --- settings ----------------------------------------------------------------------------------


def test_the_defaults_are_the_ones_asked_for(tmp_path):
    ws = Workspace(tmp_path / "studio").create()
    settings = wm.load_settings(ws)
    assert (settings.enabled, settings.scope, settings.position) == (True, "digital", "center")
    assert (settings.opacity, settings.size, settings.tile_size) == (35, 30, 15)
    assert wm.describe(ws)["on"] is False, "no picture, nothing stamped"


@pytest.mark.parametrize("changes", [
    {"opacity": 9}, {"opacity": 91}, {"opacity": "35"}, {"opacity": True}, {"opacity": 35.5},
    {"size": 61}, {"size": 4}, {"tile_size": 41}, {"position": "left"}, {"scope": "some"},
    {"enabled": "yes"}, {"name": "x.png"}, {"ws": 1}, {"colour": "red"},
])
def test_bad_settings_are_refused_with_the_field_named(tmp_path, changes):
    ws = Workspace(tmp_path / "studio").create()
    _set_mark(ws)
    with pytest.raises(wm.WatermarkError) as caught:
        wm.update_settings(ws, changes)
    assert caught.value.code == "invalid" and caught.value.params["field"] == next(iter(changes))


def test_good_settings_are_saved_beside_the_folders(tmp_path):
    ws = Workspace(tmp_path / "studio").create()
    _set_mark(ws)
    wm.update_settings(ws, {"opacity": 90.0, "position": "tiled"}, tile_size=40, scope="all")
    saved = json.loads((ws.root / "watermark.json").read_text(encoding="utf-8"))
    assert (saved["opacity"], saved["position"], saved["tile_size"], saved["scope"]) == (90, "tiled", 40, "all")
    assert wm.load_settings(ws).mark_size == 40


def test_switching_on_needs_a_picture(tmp_path):
    ws = Workspace(tmp_path / "studio").create()
    with pytest.raises(wm.WatermarkError) as caught:
        wm.update_settings(ws, enabled=True)
    assert caught.value.code == "watermark_missing"
    wm.update_settings(ws, enabled=False)  # switching off is always fine


def test_a_settings_file_edited_by_hand_is_read_forgivingly(tmp_path):
    ws = Workspace(tmp_path / "studio").create()
    (ws.root / "watermark.json").write_text('{"opacity": 500, "size": 1, "position": "x", "scope": "all"}',
                                            encoding="utf-8")
    settings = wm.load_settings(ws)
    assert (settings.opacity, settings.size, settings.position, settings.scope) == (90, 5, "center", "all")
    (ws.root / "watermark.json").write_text("not json", encoding="utf-8")
    assert wm.load_settings(ws) == wm.DEFAULTS


def test_the_watermark_files_are_the_workspaces_own(tmp_path):
    ws = Workspace(tmp_path / "studio").create()
    _set_mark(ws)
    assert not any(p.name.startswith("watermark") for p in ws.mockup_files())
    assert all("watermark" not in p.name for p, _photos in ws.product_groups())
    assert wm.watermark_path(ws).parent == ws.root and wm.settings_path(ws).parent == ws.root


# --- the uploaded picture --------------------------------------------------------------------


def test_an_upload_is_trimmed_upright_and_stored_as_a_clean_png(tmp_path):
    ws = Workspace(tmp_path / "studio").create()
    settings = wm.save_upload(ws, "Shop Logo.PNG", _logo_bytes((300, 90), margin=25))
    assert settings.enabled and settings.name == "Shop Logo.png"
    with Image.open(wm.watermark_path(ws)) as stored:
        assert stored.format == "PNG" and stored.mode == "RGBA" and stored.size == (300, 90)
        assert not stored.getexif()
    info = wm.file_info(ws)
    assert info["ground"] == "transparent" and info["width"] == 300 and info["readable"]


def test_replacing_the_picture_keeps_its_settings(tmp_path):
    ws = Workspace(tmp_path / "studio").create()
    _set_mark(ws, enabled=False, position="corner")
    settings = wm.save_upload(ws, "second.png", _logo_bytes())
    assert settings.enabled is False and settings.position == "corner" and settings.name == "second.png"
    assert wm.remove(ws) and not wm.watermark_path(ws).exists() and not wm.remove(ws)
    assert wm.save_upload(ws, "third.png", _logo_bytes()).enabled, "a first picture is switched on"


def _two_bars_on_white() -> Image.Image:
    """A mark saved on white: two dark bars, the white between them joined to the edges."""
    image = Image.new("RGB", (200, 100), WHITE)
    draw = ImageDraw.Draw(image)
    draw.rectangle((50, 30, 90, 70), fill=(10, 20, 40))
    draw.rectangle((110, 30, 150, 70), fill=(10, 20, 40))
    return image


def _jpeg_bytes(size=(64, 64), colour=(200, 200, 200)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, "JPEG")
    return buffer.getvalue()


def _gif_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64)).save(buffer, "GIF")
    return buffer.getvalue()


@pytest.mark.parametrize(("name", "data", "code"), [
    ("logo.gif", _gif_bytes(), "watermark_type"),
    ("logo.svg", b"<svg/>", "watermark_type"),
    ("logo.png", _gif_bytes(), "watermark_type"),  # the content decides too
    ("logo.png", b"", "watermark_image"),
    ("logo.png", b"not a picture at all", "watermark_image"),
    ("logo.png", _png_bytes(Image.new("RGBA", (10, 40), (0, 0, 0, 255))), "watermark_too_small"),
    ("logo.png", _png_bytes(Image.new("RGBA", (64, 64), (0, 0, 0, 0))), "watermark_blank"),
])
def test_a_picture_that_cannot_be_a_watermark_is_refused(tmp_path, name, data, code):
    ws = Workspace(tmp_path / "studio").create()
    with pytest.raises(wm.WatermarkError) as caught:
        wm.save_upload(ws, name, data)
    assert caught.value.code == code
    assert not wm.watermark_path(ws).exists()


def test_a_jpg_and_a_webp_are_taken(tmp_path):
    ws = Workspace(tmp_path / "studio").create()
    wm.save_upload(ws, "logo.jpg", _jpeg_bytes())
    assert wm.file_info(ws)["ground"] == "flat"
    buffer = io.BytesIO()
    Image.new("RGBA", (64, 64), (200, 10, 10, 200)).save(buffer, "WEBP")
    wm.save_upload(ws, "logo.webp", buffer.getvalue())
    assert wm.file_info(ws)["ground"] == "transparent"


def test_too_large_a_file_or_too_many_pixels_is_refused(tmp_path, monkeypatch):
    ws = Workspace(tmp_path / "studio").create()
    monkeypatch.setattr(wm, "MAX_UPLOAD_BYTES", 100)
    with pytest.raises(wm.WatermarkError) as caught:
        wm.save_upload(ws, "logo.png", _logo_bytes())
    assert caught.value.code == "watermark_too_large" and caught.value.params["max_mb"] == 0
    monkeypatch.setattr(wm, "MAX_UPLOAD_BYTES", 10 * 1024 * 1024)
    monkeypatch.setattr(catalog, "MAX_EDGE", 200)
    with pytest.raises(catalog.TooManyPixels):
        wm.save_upload(ws, "logo.png", _logo_bytes((300, 90)))


def test_a_solid_background_can_be_made_see_through(tmp_path):
    ws = Workspace(tmp_path / "studio").create()
    image = _two_bars_on_white()
    wm.save_upload(ws, "logo.png", _png_bytes(image))
    assert wm.file_info(ws)["ground"] == "flat"
    wm.remove_ground(ws)
    with Image.open(wm.watermark_path(ws)) as keyed:
        assert keyed.size == (101, 41), "the clear margins are trimmed again"
        assert keyed.getpixel((5, 20))[3] == 255 and keyed.getpixel((50, 20))[3] == 0
    assert wm.file_info(ws)["ground"] == "transparent"
    with pytest.raises(wm.WatermarkError) as caught:
        wm.remove_ground(ws)
    assert caught.value.code == "watermark_no_ground"


# --- which runs stamp ---------------------------------------------------------------------------


def test_which_runs_stamp(tmp_path):
    ws = Workspace(tmp_path / "studio").create()
    assert wm.for_run(ws, "download") is None, "no picture"
    _set_mark(ws)
    assert wm.for_run(ws, "physical") is None, "digital only by default"
    assert isinstance(wm.for_run(ws, "download"), wm.Watermark)
    assert isinstance(wm.for_run(ws, "both"), wm.Watermark)
    wm.update_settings(ws, scope="all")
    assert isinstance(wm.for_run(ws, "physical"), wm.Watermark)
    wm.update_settings(ws, enabled=False)
    assert wm.for_run(ws, "download") is None
    info = wm.describe(ws, "download")
    assert info["file"] and not info["on"] and info["applies"] is False


def test_an_unreadable_picture_that_is_on_stops_the_run(tmp_path):
    ws = Workspace(tmp_path / "studio").create()
    _set_mark(ws)
    wm.watermark_path(ws).write_bytes(b"\x89PNG\r\n\x1a\n broken")
    assert wm.describe(ws, "download")["problem"] == "unreadable"
    with pytest.raises(wm.WatermarkError) as caught:
        wm.for_run(ws, "download")
    assert caught.value.code == "watermark_unreadable"
    assert wm.for_run(ws, "physical") is None, "a run it does not apply to is not stopped"


def test_the_cli_summary_line():
    info = {"file": True, "on": True, "scope": "digital", "position": "tiled", "opacity": 35,
            "size": 15, "applies": False}
    line = wm.summary(info)
    assert "digital products only" in line and "repeated diagonally" in line and "physical" in line
    assert wm.summary({**info, "on": False}) == "Watermark: off."
    assert wm.summary({**info, "file": False}) == ""


# --- the app's run (drop.stream) ------------------------------------------------------------


class MarkClient(test_drop_stream.Client):
    """The stream tests' Etsy stand-in, also taking uploadListingImage's is_watermarked."""

    def __init__(self, ws, **kw):
        super().__init__(ws, **kw)
        self.marked: dict[str, bool] = {}
        self.image_paths: list[Path] = []
        self.file_paths: list[Path] = []

    def upload_listing_image(self, listing_id, image, *, rank, alt_text="", is_watermarked=False):
        self.marked[image.name] = is_watermarked
        self.image_paths.append(Path(image))
        return super().upload_listing_image(listing_id, image, rank=rank, alt_text=alt_text)

    def upload_listing_file(self, listing_id, path, *, rank):
        self.file_paths.append(Path(path))
        return super().upload_listing_file(listing_id, path, rank=rank)


def test_the_apps_run_stamps_every_listing_photo_and_never_a_download(studio):
    ws, template = studio
    template = _template(ws, template)  # a digital download
    design = _opaque(ws.products / "boho-sunset-wall-art.jpg")
    folder = _folder(ws, "kids coloring pages", photos=("01-cover.jpg", "02-inside.jpg"),
                     files={"pages.pdf": b"%PDF-1.4 example"})
    before = {p: p.read_bytes() for p in [design, *folder.rglob("*.*")]}
    _set_mark(ws, position="tiled")
    client = MarkClient(ws)
    events = test_drop_stream.Events()
    report = _run(ws, template, client, on_event=events)

    assert [item.status for item in report.items] == [stream.OK, stream.OK]
    # Every picture that went up is a stamped copy, and Etsy was told so.
    assert client.image_paths and all(_stamped(p) for p in client.image_paths)
    assert set(client.marked.values()) == {True}
    for item in report.items:
        assert item.images and all(_stamped(p) for p in item.images)
        assert len(item.stamped) == len(item.images)
    # The folder's own photos and the composites and the preview all got it.
    names = sorted(p.name for p in client.image_paths)
    assert "01-cover.jpg" in names and "02-inside.jpg" in names
    assert any("--flat" in n for n in names)
    # The downloads went up as they are, from 2-PRODUCTS; nothing there was touched.
    assert sorted(p.name for p in client.file_paths) == [design.name, "pages.pdf"]
    assert all(ws.products.resolve() in p.resolve().parents for p in client.file_paths)
    assert {p: p.read_bytes() for p in before} == before
    # The screen hears how many pictures carry it.
    mockup_done = [d for n, step, status, d in events.items if step == "mockup" and status in ("done", "warn")]
    assert all(d["watermarked"] == len(d["images"]) for d in mockup_done)


def test_a_physical_run_with_the_digital_scope_stamps_nothing(studio):
    ws, template = studio  # physical
    _artwork(ws.products / "retro-mountain-sunset.png")
    _set_mark(ws)
    client = test_drop_stream.Client(ws)  # takes no is_watermarked: never sent
    item = _run(ws, template, client).items[0]
    assert item.status == stream.OK and not item.stamped
    assert not any(_stamped(p) for p in item.images)


def test_every_listing_scope_stamps_a_physical_run_too(studio):
    ws, template = studio
    _artwork(ws.products / "retro-mountain-sunset.png")
    _set_mark(ws, scope="all", position="corner")
    client = MarkClient(ws)
    item = _run(ws, template, client).items[0]
    assert item.status == stream.OK and len(item.stamped) == len(item.images) == 3
    assert set(client.marked.values()) == {True}


def test_a_run_asked_not_to_stamp_does_not(studio):
    ws, template = studio
    template = _template(ws, template)
    _opaque(ws.products / "boho-sunset-wall-art.jpg")
    _set_mark(ws)
    item = _run(ws, template, test_drop_stream.Client(ws), watermark=False).items[0]
    assert item.status == stream.OK and not item.stamped


def test_a_photo_the_mark_cannot_go_on_fails_its_product_before_the_draft(studio, monkeypatch):
    ws, template = studio
    template = _template(ws, template)
    _opaque(ws.products / "boho-sunset-wall-art.jpg")
    _set_mark(ws)

    def broken(self, source, out_dir, taken=None):
        raise OSError("disk full")

    monkeypatch.setattr(wm.Watermark, "stamp", broken)
    client = MarkClient(ws)
    item = _run(ws, template, client).items[0]
    assert item.status == stream.FAILED and item.error.code == "watermark_failed"
    assert client.creates == [] and client.image_paths == []


def test_an_unreadable_mark_stops_the_apps_run_before_anything_is_sent(studio):
    ws, template = studio
    template = _template(ws, template)
    _opaque(ws.products / "boho-sunset-wall-art.jpg")
    _set_mark(ws)
    wm.watermark_path(ws).write_bytes(b"broken")
    client = MarkClient(ws)
    with pytest.raises(ValidationError, match="watermark"):
        _run(ws, template, client)
    assert client.creates == []


# --- the CLI's runs (drop.pipeline, drop.automation) -----------------------------------------------


def _cli_ws(tmp_path, monkeypatch):
    monkeypatch.setenv("ETSYOTOMASYON_HOME", str(tmp_path / "home"))
    ws = Workspace(tmp_path / "studio").create()
    Image.new("RGB", (400, 400), (240, 240, 240)).save(ws.mockups / "frame-oak.jpg")
    return ws


def test_drop_run_stamps_copies_and_leaves_the_downloads_alone(tmp_path, monkeypatch):
    ws = _cli_ws(tmp_path, monkeypatch)
    design = _opaque(ws.products / "boho-sunset-wall-art.jpg", size=(600, 750))
    folder = _folder(ws, "kids coloring pages", files={"pages.pdf": b"%PDF-1.4"})
    template = _template(ws)
    _set_mark(ws)
    report = pipeline.run(ws, template, client=None, use_cache=False, mockups=ws.mockup_files())
    rows = {row.source.name: row for row in report.rows}
    assert all(row.ok for row in rows.values())
    for row in rows.values():
        assert row.images and all(_stamped(p) for p in row.images) and row.stamped == row.images
    assert rows[design.name].files == [design]
    assert rows[folder.name].files == [folder / "dosyalar" / "pages.pdf"]
    # review.csv points at the stamped copies; `listings push` sends those.
    text = report.csv_path.read_text(encoding="utf-8-sig")
    assert ",watermarked/" in text and "pages.pdf" in text
    stamped = next(p for p in rows[design.name].images if "--flat" in p.name)
    # One folder of stamped copies for the batch, beside the composites.
    assert stamped.parent == report.out_dir / wm.STAMPED_DIR
    original = stamped.parent.parent / stamped.name
    assert original.is_file() and original.read_bytes() != stamped.read_bytes()


def test_drop_run_without_the_watermark(tmp_path, monkeypatch):
    ws = _cli_ws(tmp_path, monkeypatch)
    _opaque(ws.products / "boho-sunset-wall-art.jpg", size=(600, 750))
    template = _template(ws)
    _set_mark(ws)
    report = pipeline.run(ws, template, client=None, use_cache=False, mockups=ws.mockup_files(),
                          watermark=False)
    [row] = report.rows
    assert row.ok and not row.stamped and not any(_stamped(p) for p in row.images)


def test_drop_run_skips_a_product_whose_photo_cannot_take_the_mark(tmp_path, monkeypatch):
    ws = _cli_ws(tmp_path, monkeypatch)
    _opaque(ws.products / "boho-sunset-wall-art.jpg", size=(600, 750))
    template = _template(ws)
    _set_mark(ws)
    monkeypatch.setattr(wm.Watermark, "stamp", lambda *a, **k: (_ for _ in ()).throw(OSError("no room")))
    [row] = pipeline.run(ws, template, client=None, use_cache=False, mockups=ws.mockup_files()).rows
    assert row.skipped and any("watermark could not be put on" in w for w in row.warnings)


class AutoClient:
    def __init__(self):
        self.images: list[tuple[str, bool]] = []
        self.files: list[Path] = []

    def shop_id(self):
        return int(SHOP)

    def search_active_listings(self, **kwargs):
        return iter([])

    def listing_inventory(self, listing_id):
        return {"products": [{"property_values": [], "offerings": [
            {"price": {"amount": 450, "divisor": 100}, "quantity": 999, "is_enabled": True}]}]}

    def create_draft_listing(self, fields):
        return {"listing_id": 1000001}

    def upload_listing_image(self, listing_id, image, *, rank, alt_text="", is_watermarked=False):
        self.images.append((Path(image).parent.name, is_watermarked))
        return {}

    def upload_listing_file(self, listing_id, path, *, rank):
        self.files.append(Path(path))
        return {"listing_file_id": 7001}


def test_drop_auto_tells_etsy_which_pictures_carry_the_mark(tmp_path, monkeypatch):
    ws = _cli_ws(tmp_path, monkeypatch)
    design = _opaque(ws.products / "boho-sunset-wall-art.jpg", size=(600, 750))
    template = _template(ws)
    _set_mark(ws)
    client = AutoClient()
    report = automation.run(ws, template, client=client, mockups=ws.mockup_files())
    assert report.uploaded.created == 1
    assert client.images and all(folder == wm.STAMPED_DIR and marked for folder, marked in client.images)
    assert [p.resolve() for p in client.files] == [design.resolve()]


def test_the_cli_sets_and_shows_the_watermark(tmp_path):
    ws = Workspace(tmp_path / "studio").create()
    logo = tmp_path / "logo.png"
    logo.write_bytes(_logo_bytes())
    runner = CliRunner()
    result = runner.invoke(app, ["drop", "watermark", "--path", str(ws.root), "--file", str(logo),
                                 "--position", "tiled", "--size", "20", "--opacity", "40",
                                 "--scope", "all"])
    assert result.exit_code == 0, result.output
    settings = wm.load_settings(ws)
    assert (settings.position, settings.tile_size, settings.size, settings.opacity, settings.scope) == (
        "tiled", 20, 30, 40, "all")
    assert "repeated diagonally" in result.output
    result = runner.invoke(app, ["drop", "watermark", "--path", str(ws.root), "--off"])
    assert result.exit_code == 0 and wm.load_settings(ws).enabled is False
    result = runner.invoke(app, ["drop", "watermark", "--path", str(ws.root), "--opacity", "5"])
    assert result.exit_code == 1 and "opacity" in result.output
    result = runner.invoke(app, ["drop", "watermark", "--path", str(ws.root), "--remove"])
    assert result.exit_code == 0 and not wm.watermark_path(ws).exists()


def test_the_cli_runs_say_whether_they_stamp(tmp_path, monkeypatch):
    ws = _cli_ws(tmp_path, monkeypatch)
    _opaque(ws.products / "boho-sunset-wall-art.jpg", size=(600, 750))
    _template(ws)
    _set_mark(ws)
    runner = CliRunner()
    result = runner.invoke(app, ["drop", "auto", "--path", str(ws.root), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Watermark: on" in result.output
    assert list(ws.drafts.rglob(f"{wm.STAMPED_DIR}/*.jpg"))
    (ws.root / "upload-history.json").unlink(missing_ok=True)
    result = runner.invoke(app, ["drop", "auto", "--path", str(ws.root), "--dry-run", "--no-watermark"])
    assert result.exit_code == 0 and "left off for this run" in result.output


# --- Etsy's uploadListingImage -----------------------------------------------------------------


def test_the_client_sends_is_watermarked_only_when_asked(tmp_path):
    # uploadListingImage's form field `is_watermarked` (boolean, default false in Etsy's
    # Open API spec): sent as "true" for a stamped picture, left out otherwise.
    image = tmp_path / "front.jpg"
    Image.new("RGB", (40, 40)).save(image, "JPEG")
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return httpx.Response(201, json={"listing_image_id": 1, "rank": 1})

    client = make_client(handler)
    client.upload_listing_image(1000001, image, rank=1)
    client.upload_listing_image(1000001, image, rank=2, is_watermarked=True)
    assert b'name="is_watermarked"' not in bodies[0]
    assert re.search(rb'name="is_watermarked"\r\n\r\ntrue\r\n', bodies[1])


# --- the settings API (web/api/watermark) --------------------------------------------------------


def _put_mark(web, data=None, name="logo.png"):
    return web.client.put("/api/watermark/file", params={"name": name}, content=data or _logo_bytes())


def test_the_empty_watermark(web):
    data = web.client.get("/api/watermark").json()
    assert data["file"] is None and data["on"] is False
    assert data["settings"]["scope"] == "digital" and data["settings"]["opacity"] == 35
    assert data["limits"]["opacity"] == [10, 90] and data["limits"]["max_mb"] == 10
    assert web.client.get("/api/watermark/preview").json()["error"]["code"] == "watermark_missing"


def test_upload_preview_and_remove(web):
    ws = web.ctx.workspace()
    (ws.mockups / "poster-oak-frame.jpg").write_bytes(_jpeg_bytes((300, 360), (240, 235, 225)))
    resp = _put_mark(web)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["file"]["name"] == "logo.png" and data["file"]["ground"] == "transparent" and data["on"]
    assert data["preview_mockups"] == ["poster-oak-frame.jpg"] and data["preview_default"] == "poster-oak-frame.jpg"
    image = web.client.get("/api/watermark/image", params={"v": data["file"]["version"]})
    assert image.headers["content-type"] == "image/png" and "max-age" in image.headers["cache-control"]
    for query in ({}, {"mockup": "flat"}, {"mockup": "poster-oak-frame.jpg", "position": "tiled",
                                              "tile_size": "10", "opacity": "80", "max": "400"}):
        preview = web.client.get("/api/watermark/preview", params=query)
        assert preview.status_code == 200, (query, preview.text)
        assert preview.headers["content-type"] == "image/jpeg" and preview.headers["cache-control"] == "no-store"
        with Image.open(io.BytesIO(preview.content)) as shown:
            assert max(shown.size) <= int(query.get("max", 900))
    # The preview never saves what it was sent.
    assert wm.load_settings(ws).position == "center"
    assert web.client.get("/api/watermark/preview", params={"mockup": "nope.jpg"}).status_code == 404
    assert web.client.get("/api/watermark/preview", params={"opacity": "x"}).json()["error"]["code"] == "invalid"
    assert web.client.get("/api/watermark/preview", params={"size": "99"}).json()["error"]["params"]["field"] == "size"
    removed = web.client.delete("/api/watermark/file")
    assert removed.status_code == 200 and removed.json()["removed"] is True and removed.json()["file"] is None
    assert web.client.get("/api/watermark/image").status_code == 404


def test_patch_saves_and_refuses_bad_values(web):
    assert web.client.patch("/api/watermark", json={"enabled": True}).json()["error"]["code"] == "watermark_missing"
    _put_mark(web)
    resp = web.client.patch("/api/watermark", json={"position": "corner", "opacity": 60, "scope": "all"})
    assert resp.status_code == 200 and resp.json()["settings"]["position"] == "corner"
    for body in ({"opacity": 95}, {"size": "30"}, {"ws": 1}, {"unknown": 1}, {"position": None}, {}):
        resp = web.client.patch("/api/watermark", json=body)
        assert resp.status_code == 422, (body, resp.text)
        assert resp.json()["error"]["code"] == "invalid"
    assert web.client.patch("/api/watermark", content=b"[1]", headers={"Content-Type": "application/json"}).status_code == 400
    assert wm.load_settings(web.ctx.workspace()).opacity == 60


@pytest.mark.parametrize(("name", "data", "code"), [
    ("logo.gif", _gif_bytes(), "watermark_type"),
    ("logo.png", b"nothing", "watermark_image"),
    ("logo.png", _png_bytes(Image.new("RGBA", (8, 8), (0, 0, 0, 255))), "watermark_too_small"),
    ("logo.png", _png_bytes(Image.new("RGBA", (64, 64), (0, 0, 0, 0))), "watermark_blank"),
])
def test_bad_files_are_refused_with_their_reason(web, name, data, code):
    resp = _put_mark(web, data, name)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == code
    assert web.client.get("/api/watermark").json()["file"] is None


def test_an_upload_needs_a_name(web):
    resp = web.client.put("/api/watermark/file", content=_logo_bytes())
    assert resp.status_code == 422 and resp.json()["error"]["params"]["field"] == "name"


def test_the_background_removal_endpoint(web):
    image = _two_bars_on_white()
    _put_mark(web, _png_bytes(image))
    resp = web.client.post("/api/watermark/remove-ground")
    assert resp.status_code == 200 and resp.json()["file"]["ground"] == "transparent"
    again = web.client.post("/api/watermark/remove-ground")
    assert again.status_code == 409 and again.json()["error"]["code"] == "watermark_no_ground"


def test_every_write_needs_the_session_and_the_custom_header(web):
    with web.anonymous() as http:
        assert http.get("/api/watermark").status_code == 401
        http.cookies = web.client.cookies
        assert http.put("/api/watermark/file", params={"name": "logo.png"}, content=_logo_bytes()).status_code == 403
        assert http.patch("/api/watermark", json={"opacity": 50}).status_code == 403
        assert http.delete("/api/watermark/file").status_code == 403
        assert http.post("/api/watermark/remove-ground").status_code == 403
    assert not wm.watermark_path(web.ctx.workspace()).exists()


def test_the_start_card_says_whether_the_run_stamps(web):
    from test_web_designs import _put, _setup_shop

    _fake, ws = _setup_shop(web)
    _put(web, "boho-sunset-wall-art.jpg", _jpeg_bytes())
    pending = web.client.get("/api/designs/pending").json()
    assert pending["watermark"]["file"] is False and pending["watermark"]["on"] is False
    _put_mark(web)
    pending = web.client.get("/api/designs/pending").json()
    assert pending["watermark"]["on"] and pending["watermark"]["applies"] is False, "physical template"
    _template(ws)
    pending = web.client.get("/api/designs/pending").json()
    assert pending["watermark"]["applies"] is True and pending["watermark"]["scope"] == "digital"
    assert "watermark" not in pending["blockers"]
    wm.watermark_path(ws).write_bytes(b"broken")
    pending = web.client.get("/api/designs/pending").json()
    assert "watermark" in pending["blockers"]


# --- the words on screen -----------------------------------------------------------------------------


def _strings(name: str) -> dict:
    return json.loads((STATIC_DIR / "i18n" / f"{name}.json").read_text(encoding="utf-8"))


def test_the_watermark_words_exist_in_both_languages():
    mockups = _strings("mockups")
    assert set(mockups["tr"]) == set(mockups["en"])
    ours = {k for k in mockups["tr"] if k.startswith("wm.")}
    assert len(ours) > 40
    source = Path(wm.__file__).read_text(encoding="utf-8")
    codes = set(re.findall(r'WatermarkError\(\s*"([a-z_]+)"', source)) - {"invalid"}
    assert {"watermark_type", "watermark_blank", "watermark_unreadable"} <= codes
    for lang in ("tr", "en"):
        missing = sorted(code for code in codes if f"errors.{code}" not in mockups[lang])
        assert not missing, (lang, missing)
        for key in ("wm.scope.digital", "wm.scope.all", "wm.position.center", "wm.position.corner",
                    "wm.position.tiled"):
            assert mockups[lang][key]
    assert mockups["tr"]["wm.title"] == "Filigran"
    assert mockups["tr"]["wm.scope.digital"] == "Yalnızca dijital ürünler"
    assert mockups["tr"]["wm.scope.all"] == "Tüm ilan görselleri"
    designs = _strings("designs")
    assert set(designs["tr"]) == set(designs["en"])
    tr = designs["tr"]
    line = f'{tr["ready.wm_label"]} {tr["ready.wm_on"]} · {tr["ready.wm_scope_digital"]}'
    assert line == "Filigran: açık · dijital ürünlerde"
    for key in ("blocked.watermark", "problem.watermark_failed", "ready.wm_off", "ready.wm_edit"):
        assert tr[key] and designs["en"][key]


def test_the_pages_ask_for_words_that_exist():
    designs = (STATIC_DIR / "js" / "pages" / "designs.js").read_text(encoding="utf-8")
    keys = set(re.findall(r'''"((?:ready\.wm_|blocked\.go_watermark)[a-z_.]*)"''', designs))
    assert {"ready.wm_label", "ready.wm_on", "ready.wm_off", "ready.wm_edit"} <= keys
    strings = _strings("designs")
    for lang in ("tr", "en"):
        assert not [k for k in keys if k not in strings[lang]]
    js = (STATIC_DIR / "js" / "pages" / "mockups.js").read_text(encoding="utf-8")
    # The templated keys: every scope, position and ground the server can say.
    for group, values in (("scope", wm.SCOPES), ("position", wm.POSITIONS),
                          ("ground", ("transparent", "flat", "photo"))):
        assert f"wm.{group}." in js
        for value in values:
            assert f"wm.{group}.{value}" in _strings("mockups")["en"]


def test_the_pending_view_and_stream_names_stay_in_step():
    # The start card reads describe()'s keys; the run view problem.<code>.
    assert "watermark_failed" in Path(stream.__file__).read_text(encoding="utf-8")
    info = wm.describe(Workspace(Path("unused")), "download")
    assert {"file", "on", "scope", "applies", "problem"} <= set(info)
    assert mockup.JPEG_OPTIONS["quality"] >= 90, "stamped JPEGs keep the app's own quality"
