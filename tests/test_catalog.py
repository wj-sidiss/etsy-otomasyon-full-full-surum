"""The mockup catalog: type/colour/enabled per mockup, and which print area applies."""

from __future__ import annotations

import io
import json

import pytest
from PIL import Image

from etsyotomasyon.drop import catalog, mockup
from etsyotomasyon.drop.workspace import Workspace
from etsyotomasyon.errors import ValidationError


def image_bytes(size=(40, 30), fmt="PNG") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 200, 200)).save(buffer, format=fmt)
    return buffer.getvalue()


@pytest.fixture
def ws(tmp_path) -> Workspace:
    workspace = Workspace(tmp_path / "Etsy Studio").create()
    for name, size in (("a-shirt-white.png", (40, 30)), ("b-shirt-black.png", (40, 30)),
                       ("c-mug.png", (50, 50))):
        (workspace.mockups / name).write_bytes(image_bytes(size))
    return workspace


@pytest.mark.parametrize("filename, expected", [
    ("tshirt-white.jpg", ("tshirt", "Beyaz")),
    ("T-Shirt Black 01.png", ("tshirt", "Siyah")),
    ("tişört-lacivert.jpg", ("tshirt", "Lacivert")),
    ("KUPA_beyaz.png", ("mug", "Beyaz")),
    ("coffee-mug.png", ("mug", "")),
    ("hoodie-grey.jpg", ("hoodie", "Gri")),
    ("kapüşonlu-siyah.jpg", ("hoodie", "Siyah")),
    ("sweatshirt-cream.jpg", ("sweatshirt", "Krem")),
    ("poster-oak-frame.jpg", ("poster", "Meşe")),
    ("çerçeve meşe.jpg", ("poster", "Meşe")),
    ("canvas-print.jpg", ("canvas", "")),
    ("iphone-case.png", ("phone_case", "")),
    ("telefon-kılıf-pembe.png", ("phone_case", "Pembe")),
    ("tote-bag-natural.png", ("tote", "Krem")),
    ("çanta.png", ("tote", "")),
    ("yastık-kırmızı.png", ("pillow", "Kırmızı")),
    ("sticker.png", ("sticker", "")),
    ("IMG_0001.jpg", ("other", "")),
])
def test_guess_reads_english_and_turkish_file_names(filename, expected):
    assert catalog.guess(filename) == expected


def test_load_guesses_what_has_no_entry_and_ignores_orphans(ws):
    catalog.catalog_path(ws).write_text(json.dumps({
        "c-mug.png": {"type": "mug", "color": "Krem", "enabled": False},
        "gone.png": {"type": "poster", "color": "", "enabled": True},
    }), encoding="utf-8")
    infos = catalog.load(ws)
    assert list(infos) == ["a-shirt-white.png", "b-shirt-black.png", "c-mug.png"]
    assert infos["a-shirt-white.png"].to_dict() == {
        "name": "a-shirt-white.png", "type": "tshirt", "color": "Beyaz", "enabled": True,
        "order": None}
    assert infos["c-mug.png"].enabled is False and infos["c-mug.png"].color == "Krem"


def test_a_broken_catalog_file_is_not_fatal(ws):
    catalog.catalog_path(ws).write_text("{nope", encoding="utf-8")
    assert len(catalog.load(ws)) == 3


def test_update_records_one_mockup(ws):
    info = catalog.update(ws, "c-mug.png", type="mug", color="Siyah", enabled=False)
    assert (info.type, info.color, info.enabled) == ("mug", "Siyah", False)
    assert catalog.load(ws)["c-mug.png"].enabled is False
    with pytest.raises(ValidationError):
        catalog.update(ws, "c-mug.png", type="spaceship")
    with pytest.raises(FileNotFoundError):
        catalog.update(ws, "../product.json", enabled=True)
    with pytest.raises(FileNotFoundError):
        catalog.update(ws, "missing.png", enabled=True)


def test_enabled_mockups_skip_disabled_ones_and_stop_at_19(ws):
    catalog.update(ws, "b-shirt-black.png", enabled=False)
    assert [p.name for p in catalog.enabled_mockups(ws)] == ["a-shirt-white.png", "c-mug.png"]
    for n in range(25):
        (ws.mockups / f"z-{n:02d}.png").write_bytes(image_bytes())
    assert len(catalog.enabled_mockups(ws)) == catalog.MAX_ENABLED


def test_add_saves_a_safe_name_and_never_overwrites(ws):
    assert catalog.add(ws, "new shirt.PNG", image_bytes()) == "new shirt.png"
    assert catalog.add(ws, "new shirt.png", image_bytes()) == "new shirt-2.png"
    assert catalog.add(ws, "..\\..\\evil.png", image_bytes()) == "evil.png"
    assert catalog.add(ws, "CON.png", image_bytes()) == "mockup-CON.png"
    assert (ws.mockups / "new shirt-2.png").is_file()
    with pytest.raises(ValidationError):
        catalog.add(ws, "notes.txt", b"hello")
    with pytest.raises(ValidationError):
        catalog.add(ws, "fake.png", b"not an image at all")
    with pytest.raises(ValidationError):
        catalog.add(ws, "empty.png", b"")


def test_remove_deletes_the_file_and_what_is_recorded_about_it(ws):
    catalog.update(ws, "c-mug.png", color="Krem")
    mockup.save_positions(ws.positions_path, {
        "c-mug.png": mockup.PrintArea(0.1, 0.1, 0.5, 0.5),
        "a-shirt-white.png": mockup.PrintArea(0.2, 0.2, 0.4, 0.4),
    })
    catalog.remove(ws, "c-mug.png")
    assert not (ws.mockups / "c-mug.png").exists()
    assert "c-mug.png" not in json.loads(catalog.catalog_path(ws).read_text(encoding="utf-8"))
    assert set(mockup.load_positions(ws.positions_path)) == {"a-shirt-white.png"}
    with pytest.raises(FileNotFoundError):
        catalog.remove(ws, "c-mug.png")


def test_effective_area_matches_the_pipeline_lookup(ws):
    assert catalog.effective_area(ws, "a-shirt-white.png") == (
        mockup.DEFAULT_PRINT_AREA, catalog.SOURCE_DEFAULT)
    own = mockup.PrintArea(0.2, 0.2, 0.4, 0.4)
    mockup.save_positions(ws.positions_path, {"a-shirt-white.png": own})
    areas = catalog.effective_areas(ws)
    assert areas["a-shirt-white.png"] == (own, catalog.SOURCE_OWN)
    assert areas["b-shirt-black.png"] == (own, catalog.SOURCE_SAME_SIZE)  # same 40x30
    assert areas["c-mug.png"] == (mockup.DEFAULT_PRINT_AREA, catalog.SOURCE_DEFAULT)


def test_same_size_names_and_save_area(ws):
    assert catalog.same_size_names(ws, "a-shirt-white.png") == ["a-shirt-white.png", "b-shirt-black.png"]
    assert catalog.same_size_names(ws, "missing.png") == []
    area = mockup.PrintArea(0.1, 0.2, 0.3, 0.4)
    assert catalog.save_area(ws, "a-shirt-white.png", area, same_size=True, dry_run=True) == [
        "a-shirt-white.png", "b-shirt-black.png"]
    assert not ws.positions_path.exists()
    assert catalog.save_area(ws, "c-mug.png", area) == ["c-mug.png"]
    assert set(mockup.load_positions(ws.positions_path)) == {"c-mug.png"}
    catalog.save_area(ws, "a-shirt-white.png", area, same_size=True)
    assert set(mockup.load_positions(ws.positions_path)) == {
        "a-shirt-white.png", "b-shirt-black.png", "c-mug.png"}
    assert catalog.clear_area(ws, "c-mug.png") is True
    assert catalog.clear_area(ws, "c-mug.png") is False


# --- order and which mockups drafts use (FIXLIST 9, 10) ------------------------------------


def test_an_older_catalog_without_order_keeps_folder_order(ws):
    # mockups.json from v0.2.0: no "order" anywhere.
    catalog.catalog_path(ws).write_text(json.dumps({
        "a-shirt-white.png": {"type": "tshirt", "color": "Beyaz", "enabled": True},
        "c-mug.png": {"type": "mug", "color": "", "enabled": True},
    }), encoding="utf-8")
    assert catalog.ordered_names(ws) == ["a-shirt-white.png", "b-shirt-black.png", "c-mug.png"]
    assert [p.name for p in catalog.enabled_mockups(ws)] == catalog.ordered_names(ws)


def test_the_saved_order_decides_the_main_image(ws):
    infos = catalog.arrange(ws, order=["c-mug.png", "a-shirt-white.png"])
    # b- was not listed: it keeps its place after the listed ones.
    assert list(infos) == ["c-mug.png", "a-shirt-white.png", "b-shirt-black.png"]
    assert [p.name for p in catalog.enabled_mockups(ws)] == [
        "c-mug.png", "a-shirt-white.png", "b-shirt-black.png"]
    assert catalog.enabled_mockups(ws)[0] == ws.mockups / "c-mug.png"  # the main image
    stored = json.loads(catalog.catalog_path(ws).read_text(encoding="utf-8"))
    assert stored["c-mug.png"]["order"] == 0 and stored["b-shirt-black.png"]["order"] == 2
    # Changing type/colour/enabled keeps the place in the order.
    catalog.update(ws, "c-mug.png", color="Siyah", enabled=True)
    assert catalog.ordered_names(ws)[0] == "c-mug.png"
    assert json.loads(catalog.catalog_path(ws).read_text(encoding="utf-8"))["c-mug.png"] == {
        "type": "mug", "color": "Siyah", "enabled": True, "order": 0}


def test_a_new_mockup_joins_at_the_end_of_a_saved_order(ws):
    catalog.arrange(ws, order=["c-mug.png", "b-shirt-black.png", "a-shirt-white.png"])
    catalog.add(ws, "0-first-by-name.png", image_bytes())
    assert catalog.ordered_names(ws) == [
        "c-mug.png", "b-shirt-black.png", "a-shirt-white.png", "0-first-by-name.png"]


def test_a_removed_mockup_leaves_no_gap_in_what_is_used(ws):
    catalog.arrange(ws, order=["c-mug.png", "b-shirt-black.png", "a-shirt-white.png"])
    catalog.remove(ws, "b-shirt-black.png")
    assert [p.name for p in catalog.enabled_mockups(ws)] == ["c-mug.png", "a-shirt-white.png"]


def test_arrange_sets_exactly_the_enabled_set(ws):
    catalog.arrange(ws, enabled=["b-shirt-black.png"])
    infos = catalog.load(ws)
    assert [n for n, i in infos.items() if i.enabled] == ["b-shirt-black.png"]
    assert [p.name for p in catalog.enabled_mockups(ws)] == ["b-shirt-black.png"]
    # Order untouched when only the selection changes.
    assert list(infos) == ["a-shirt-white.png", "b-shirt-black.png", "c-mug.png"]
    catalog.arrange(ws, enabled=[])
    assert catalog.enabled_mockups(ws) == []


def test_arrange_checks_everything_before_writing(ws):
    with pytest.raises(FileNotFoundError):
        catalog.arrange(ws, order=["c-mug.png", "../product.json"])
    with pytest.raises(FileNotFoundError):
        catalog.arrange(ws, enabled=["missing.png"])
    with pytest.raises(ValidationError):
        catalog.arrange(ws, order=["c-mug.png", "c-mug.png"])
    with pytest.raises(ValidationError):
        catalog.arrange(ws)
    assert not catalog.catalog_path(ws).exists()


def test_a_broken_order_value_is_ignored(ws):
    catalog.catalog_path(ws).write_text(json.dumps({
        "c-mug.png": {"order": 0},
        "a-shirt-white.png": {"order": True},
        "b-shirt-black.png": {"order": "1"},
    }), encoding="utf-8")
    assert catalog.ordered_names(ws) == ["c-mug.png", "a-shirt-white.png", "b-shirt-black.png"]


def test_usage_marks_what_does_not_fit_in_order(ws):
    for n in range(25):
        (ws.mockups / f"z-{n:02d}.png").write_bytes(image_bytes())
    catalog.update(ws, "b-shirt-black.png", enabled=False)
    names = catalog.ordered_names(ws)
    # The last mockup becomes the main image; the first switched-on ones follow.
    catalog.arrange(ws, order=[names[-1], *names[:-1]])
    use = catalog.usage(ws)
    assert use["total"] == 28 and use["enabled"] == 27 and use["max"] == catalog.MAX_ENABLED
    assert use["used"][0] == "z-24.png"
    assert len(use["used"]) == 19 and len(use["over_limit"]) == 8
    assert "b-shirt-black.png" not in use["used"] + use["over_limit"]
    assert use["used"][1:3] == ["a-shirt-white.png", "c-mug.png"]
    assert use["over_limit"][0] == "z-16.png"
    assert [p.name for p in catalog.enabled_mockups(ws)] == use["used"]


# --- pixel limits (review finding design-pixel-bomb-500) -------------------------------------


def test_add_refuses_too_many_pixels(ws):
    # A 1-bit PNG of 8000x8000 is tiny on disk and 64M pixels once decoded.
    buffer = io.BytesIO()
    Image.new("1", (8000, 8000)).save(buffer, format="PNG")
    with pytest.raises(catalog.TooManyPixels) as caught:
        catalog.add(ws, "huge.png", buffer.getvalue())
    assert (caught.value.width, caught.value.height) == (8000, 8000)
    buffer = io.BytesIO()
    Image.new("1", (12001, 10)).save(buffer, format="PNG")
    with pytest.raises(catalog.TooManyPixels):
        catalog.add(ws, "long.png", buffer.getvalue())
    assert not (ws.mockups / "huge.png").exists() and not (ws.mockups / "long.png").exists()
    assert catalog.too_many_pixels((7000, 8000)) is False
    assert catalog.too_many_pixels((7746, 7746)) is True
    assert catalog.too_many_pixels((12000, 5000)) is False


def test_add_reports_pillows_own_bomb_refusal_as_too_many_pixels(ws, monkeypatch):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    buffer = io.BytesIO()
    Image.new("1", (100, 100)).save(buffer, format="PNG")
    with pytest.raises(catalog.TooManyPixels) as caught:
        catalog.add(ws, "bomb.png", buffer.getvalue())
    assert caught.value.width == 100
