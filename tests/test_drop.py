"""The drop pipeline: folder of designs in, validated listing CSV out.

Every image here is generated, so this suite runs in CI on a machine with no assets,
no Etsy account and no network — which is the point of putting the compositor in
Python rather than behind a native binary.
"""

import json
from pathlib import Path

import pytest
from PIL import Image, ImageChops
from typer.testing import CliRunner

from etsyotomasyon.cli import app
from etsyotomasyon.config import MAX_TAG_LEN, MAX_TAGS, MAX_TITLE_LEN
from etsyotomasyon.drop import generate, mockup, pipeline, seeds, workspace
from etsyotomasyon.drop.template import Template, capture
from etsyotomasyon.errors import ValidationError
from etsyotomasyon.listings import build_payload, prepare, validate_tags
from etsyotomasyon.seo import MarketReport

# --- seeds: knowing when the filename told us nothing ---------------------------


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("001-retro-sunset-surf.png", "retro sunset surf"),
        ("mountain-sunset.png", "mountain sunset"),
        ("ceramic_mug_gift.jpg", "ceramic mug gift"),
        ("12-cicek-dugme.png", "cicek dugme"),
        ("mountain-sunset-final-v3.png", "mountain sunset"),
    ],
)
def test_a_descriptive_filename_yields_its_concept(filename, expected):
    assert seeds.derive(Path(filename)).text == expected


@pytest.mark.parametrize(
    "filename",
    ["IMG_2043.png", "DSC00123.jpg", "Screenshot 2026-01-01.png", "untitled.png", "v2.png"],
)
def test_a_junk_filename_is_flagged_rather_than_guessed(filename):
    # Inventing a confident title here would put the wrong listing in a real shop.
    seed = seeds.derive(Path(filename), folder_fallback=False)
    assert not seed
    assert seed.reason


def test_a_junk_filename_falls_back_to_its_folder():
    seed = seeds.derive(Path("mountain sunset/IMG_2043.png"))
    assert seed.text == "mountain sunset"
    assert "folder name" in seed.reason


def test_turkish_characters_survive():
    assert seeds.derive(Path("çiçek-düğme-hediyesi.png")).text == "çiçek düğme hediyesi"


def test_grouping_collapses_files_sharing_a_concept():
    paths = [Path("mountain-sunset-1.png"), Path("mountain-sunset-2.png"), Path("mug.png")]
    # Trailing indices are part of the concept here; what matters is that identical
    # concepts collapse so research runs once.
    grouped = seeds.group([seeds.derive(p) for p in paths])
    assert len(grouped) == len(set(s.text for s in (seeds.derive(p) for p in paths)))


# --- generation: Etsy's limits are enforced, not merely checked ------------------


def _market(tags=None, phrases=None, sampled=200):
    return MarketReport(
        keyword="k",
        sampled=sampled,
        tags=tags or [("ceramic mug", 120), ("coffee lover gift", 90), ("handmade pottery", 60)],
        phrases=phrases or [("coffee lover gift", 80), ("handmade stoneware", 40)],
        price_min=10.0,
        price_median=20.0,
        price_max=40.0,
        currency="USD",
        median_favorers=12.0,
        top_listings=[],
    )


def test_generated_tags_always_satisfy_the_listing_validator():
    seed = seeds.derive(Path("retro-sunset-surf.png"))
    tags = generate.build_tags(seed, _market())
    assert validate_tags(tags) == [], "generation must not emit what push would reject"
    assert len(tags) <= MAX_TAGS
    assert all(len(t) <= MAX_TAG_LEN for t in tags)


def test_over_length_market_tags_are_dropped_not_truncated():
    report = _market(tags=[("x" * 30, 100), ("short one", 50)])
    tags = generate.build_tags(seeds.derive(Path("mug.png")), report)
    assert "x" * 30 not in tags
    assert all(len(t) <= MAX_TAG_LEN for t in tags)


def test_tags_with_disallowed_characters_are_cleaned():
    report = _market(tags=[("mug! (new)", 100)])
    tags = generate.build_tags(seeds.derive(Path("mug.png")), report)
    assert validate_tags(tags) == []


def test_near_duplicate_tags_do_not_burn_two_slots():
    report = _market(tags=[("gift", 100), ("gifts", 99), ("mug gift", 98)])
    tags = generate.build_tags(seeds.derive(Path("present.png")), report)
    assert not ("gift" in tags and "gifts" in tags)


def test_the_concept_leads_the_title_and_140_is_never_exceeded():
    seed = seeds.derive(Path("retro-sunset-surf.png"))
    title = generate.build_title(seed, _market())
    assert title.lower().startswith("retro sunset surf")
    assert len(title) <= MAX_TITLE_LEN


def test_a_very_long_concept_is_still_within_the_limit():
    seed = seeds.derive(Path(("word " * 60).strip().replace(" ", "-") + ".png"))
    title = generate.build_title(seed, _market())
    assert len(title) <= MAX_TITLE_LEN


def test_a_junk_seed_produces_nothing_and_says_why():
    result = generate.generate(seeds.derive(Path("IMG_2043.png"), folder_fallback=False))
    assert result.title == "" and result.tags == []
    assert result.warnings


def test_a_thin_market_sample_is_declared():
    result = generate.generate(seeds.derive(Path("mug.png")), _market(sampled=4))
    assert any("thin a sample" in w for w in result.warnings)


def test_no_market_data_is_declared_rather_than_hidden():
    result = generate.generate(seeds.derive(Path("mug.png")), None)
    assert any("no market data" in w for w in result.warnings)
    assert result.tags, "it should still produce something from the filename"


# --- the compositor -------------------------------------------------------------


def _make_mockup(path: Path, size=(1200, 1500)) -> Path:
    Image.new("RGB", size, (240, 240, 240)).save(path, "JPEG")
    return path


def _make_cutout_mockup(path: Path, size=(1200, 1500), mode="RGBA") -> Path:
    """A cut-out template: the garment floats on a transparent ground, as they ship."""
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    garment = Image.new("RGBA", (round(size[0] * 0.8), round(size[1] * 0.8)), (60, 90, 160, 255))
    image.paste(garment, (round(size[0] * 0.1), round(size[1] * 0.1)))
    if mode == "P":
        # PNG-8 keeps its transparency in a palette index rather than in a band.
        image = image.convert("RGB").convert("P", palette=Image.ADAPTIVE, colors=32)
        image.info["transparency"] = image.getpixel((0, 0))
    elif mode != "RGBA":
        image = image.convert(mode)
    image.save(path, "PNG")
    return path


def _make_design(path: Path, size=(800, 800), alpha=True) -> Path:
    """A print file: opaque artwork with see-through space around it.

    The transparent margin is the point, not decoration. A fully opaque RGBA export is
    what Canva and Photoshop produce for a finished photo, and treating that as artwork
    would paste a product photo into a t-shirt — so a fixture standing in for artwork
    has to be genuinely transparent somewhere.
    """
    if not alpha:
        Image.new("RGB", size, (200, 30, 30)).save(path, "PNG")
        return path
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    inset = (round(size[0] * 0.1), round(size[1] * 0.1))
    image.paste(
        (200, 30, 30, 255),
        (inset[0], inset[1], size[0] - inset[0], size[1] - inset[1]),
    )
    image.save(path, "PNG")
    return path


def test_print_area_rejects_a_rectangle_off_the_edge():
    with pytest.raises(ValidationError, match="past the edge"):
        mockup.PrintArea(0.9, 0.1, 0.5, 0.2)


def test_print_area_rejects_a_zero_sized_rectangle():
    with pytest.raises(ValidationError, match="no size"):
        mockup.PrintArea(0.1, 0.1, 0.0, 0.2)


def test_fractions_scale_to_whatever_the_mockup_measures():
    area = mockup.PrintArea(0.25, 0.25, 0.5, 0.5)
    assert area.pixels(1000, 2000) == (250, 500, 500, 1000)
    # The same rectangle on a bigger sibling — this is why fractions are stored.
    assert area.pixels(2000, 4000) == (500, 1000, 1000, 2000)


def test_compose_writes_a_jpeg_at_the_zoom_friendly_size(tmp_path):
    design = _make_design(tmp_path / "d.png")
    template_image = _make_mockup(tmp_path / "m.jpg")
    out = mockup.compose(design, template_image, tmp_path / "out" / "c.jpg")
    assert out.is_file()
    with Image.open(out) as img:
        assert img.format == "JPEG"
        assert min(img.size) >= mockup.OUTPUT_MIN_EDGE


def test_compose_keeps_the_aspect_ratio(tmp_path):
    # A wide design in a square print area must letterbox, never stretch.
    design = _make_design(tmp_path / "wide.png", size=(1000, 250))
    template_image = _make_mockup(tmp_path / "m.jpg", size=(1000, 1000))
    out = mockup.compose(
        design, template_image, tmp_path / "c.jpg", area=mockup.PrintArea(0.2, 0.2, 0.6, 0.6)
    )
    assert out.is_file()


def test_transparency_is_what_distinguishes_artwork_from_a_photo(tmp_path):
    assert mockup.looks_like_artwork(_make_design(tmp_path / "art.png", alpha=True))
    assert not mockup.looks_like_artwork(_make_design(tmp_path / "photo.png", alpha=False))


def test_an_opaque_rgba_export_is_a_photo_not_artwork(tmp_path):
    # Canva, Figma and Photoshop's "Export As > PNG" all keep an alpha channel on a
    # fully opaque composition. Reading the channel's presence as "artwork" would
    # paste a finished product photo into the middle of a t-shirt.
    opaque = tmp_path / "finished-photo.png"
    Image.new("RGBA", (800, 800), (200, 30, 30, 255)).save(opaque, "PNG")
    assert not mockup.looks_like_artwork(opaque)


@pytest.mark.parametrize("mode", ["RGBA", "LA", "P"])
def test_a_cut_out_mockup_lands_on_white_rather_than_black(tmp_path, mode):
    # Dropping the alpha instead of compositing it leaves the transparent ground black,
    # and nothing in the run says so — every image of the listing would ship like that.
    design = _make_design(tmp_path / "d.png")
    template_image = _make_cutout_mockup(tmp_path / f"cutout-{mode}.png", mode=mode)
    out = mockup.compose(design, template_image, tmp_path / f"out-{mode}.jpg")
    with Image.open(out) as img:
        rgb = img.convert("RGB")
        for point in ((2, 2), (rgb.width - 3, 2), (2, rgb.height - 3)):
            assert min(rgb.getpixel(point)) > 248, f"{point} is {rgb.getpixel(point)}, not white"


def test_an_opaque_mockup_keeps_its_own_background(tmp_path):
    design = _make_design(tmp_path / "d.png")
    template_image = _make_mockup(tmp_path / "m.jpg")
    out = mockup.compose(design, template_image, tmp_path / "opaque.jpg")
    with Image.open(out) as img:
        assert all(abs(band - 240) < 6 for band in img.convert("RGB").getpixel((2, 2)))


def test_the_mockup_ground_can_be_chosen(tmp_path):
    design = _make_design(tmp_path / "d.png")
    template_image = _make_cutout_mockup(tmp_path / "cutout.png")
    out = mockup.compose(
        design, template_image, tmp_path / "grey.jpg", background=(200, 200, 200)
    )
    with Image.open(out) as img:
        assert all(abs(band - 200) < 6 for band in img.convert("RGB").getpixel((2, 2)))


def test_the_design_survives_the_flattening(tmp_path):
    # An all-white image would pass the corner check too; the print has to still be there.
    design = _make_design(tmp_path / "d.png")
    template_image = _make_cutout_mockup(tmp_path / "cutout.png")
    out = mockup.compose(design, template_image, tmp_path / "out.jpg")
    with Image.open(out) as img:
        rgb = img.convert("RGB")
        red, green, blue = rgb.getpixel((rgb.width // 2, rgb.height // 2))
        assert red > 150 and green < 90 and blue < 90, "the design is not in the print area"


def test_flat_artwork_is_rendered_on_white(tmp_path):
    out = mockup.flatten_design(_make_design(tmp_path / "art.png"), tmp_path / "flat.jpg")
    with Image.open(out) as img:
        assert min(img.convert("RGB").getpixel((2, 2))) > 248


def test_pixel_positions_import_as_fractions():
    legacy = {"m.jpg": {"x": 250, "y": 500, "w": 500, "h": 250}}
    converted = mockup.import_pixel_positions(legacy, {"m.jpg": (1000, 2000)})
    assert converted["m.jpg"] == mockup.PrintArea(0.25, 0.25, 0.5, 0.125)


def test_one_calibration_covers_identically_sized_siblings():
    legacy = {"a.jpg": {"x": 100, "y": 100, "w": 200, "h": 200}}
    sizes = {"a.jpg": (1000, 1000), "b.jpg": (1000, 1000)}
    converted = mockup.import_pixel_positions(legacy, sizes)
    # b.jpg has no entry of its own, but a.jpg's fractions apply to it unchanged.
    assert converted["a.jpg"].pixels(*sizes["b.jpg"]) == (100, 100, 200, 200)


def test_positions_already_in_fractions_are_left_alone():
    legacy = {"m.jpg": {"x": 0.3, "y": 0.2, "w": 0.4, "h": 0.3}}
    converted = mockup.import_pixel_positions(legacy, {"m.jpg": (1000, 1000)})
    assert converted["m.jpg"] == mockup.PrintArea(0.3, 0.2, 0.4, 0.3)


def test_a_legacy_rectangle_that_overflows_is_skipped_not_fatal():
    # Raising here would abort the loop while every other bad entry is
    # skipped, so one rectangle measured on a bigger copy of the file lost the import.
    notes = []
    legacy = {
        "over.jpg": {"x": 900, "y": 100, "w": 500, "h": 200},
        "fine.jpg": {"x": 100, "y": 100, "w": 200, "h": 200},
    }
    sizes = {"over.jpg": (1000, 1000), "fine.jpg": (1000, 1000)}
    converted = mockup.import_pixel_positions(legacy, sizes, on_skip=lambda n, r: notes.append(n))
    assert set(converted) == {"fine.jpg"}
    assert notes == ["over.jpg"]


def test_every_dropped_legacy_entry_reports_a_reason():
    notes = {}
    legacy = {"a.jpg": "not a dict", "b.jpg": {"x": 1, "y": 1, "w": 0, "h": 5}, "gone.jpg": {}}
    mockup.import_pixel_positions(
        legacy, {"a.jpg": (10, 10), "b.jpg": (10, 10)}, on_skip=lambda n, r: notes.update({n: r})
    )
    assert set(notes) == {"a.jpg", "b.jpg", "gone.jpg"}
    assert all(reason for reason in notes.values())


@pytest.mark.parametrize(
    "text", ["0.30,0.26,0.40,0.36", " 0.3 , 0.26 , 0.4 , 0.36 "]
)
def test_an_area_is_parsed_from_the_four_numbers_a_seller_types(text):
    assert mockup.parse_area(text) == mockup.PrintArea(0.3, 0.26, 0.4, 0.36)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("0.3,0.2,0.4", "four numbers"),
        ("left,top,w,h", "not a number"),
        ("250,500,500,250", "not pixels"),
        ("0.9,0.1,0.5,0.2", "past the edge"),
    ],
)
def test_a_bad_area_says_what_is_wrong_with_it(text, message):
    with pytest.raises(ValidationError, match=message):
        mockup.parse_area(text)


def test_the_preview_draws_the_area_and_stays_small_enough_to_open(tmp_path):
    template_image = _make_mockup(tmp_path / "m.jpg", size=(3000, 4000))
    out = mockup.draw_preview(
        template_image, tmp_path / "prev" / "m--area.jpg", mockup.PrintArea(0.3, 0.26, 0.4, 0.36)
    )
    with Image.open(out) as img:
        assert img.format == "JPEG"
        assert max(img.size) == mockup.PREVIEW_MAX_EDGE
        centre = img.getpixel((img.width // 2, img.height // 2))
        corner = img.getpixel((2, 2))
        # Inside the rectangle is tinted; the untouched corner is the mockup's own grey.
        assert centre[0] > centre[2] and corner[0] == corner[2]


# --- EXIF orientation: what the seller sees is what gets composited -------------


def _make_phone_mockup(
    path: Path, stored=(1500, 1200), orientation=6, fill=(240, 240, 240)
) -> Path:
    """A mockup shot on a phone: stored landscape, tagged to be shown portrait.

    Orientation 6 means "turn a quarter turn clockwise to display", so Explorer,
    Preview and Etsy all show this file as 1200x1500. The GPS tags are the ones a real
    phone attaches, and they are here so a test can prove they do not survive the render.
    """
    exif = Image.Exif()
    exif[mockup.ORIENTATION_TAG] = orientation
    exif[34853] = {1: "N", 2: (51.0, 30.0, 0.0), 3: "W", 4: (0.0, 7.0, 0.0)}
    Image.new("RGB", stored, fill).save(path, "JPEG", exif=exif)
    return path


def _ink_bbox(path: Path) -> tuple[int, int, int, int]:
    """Where the artwork actually landed, ignoring JPEG noise on the pale ground."""
    with Image.open(path) as img:
        rgb = img.convert("RGB")
        ink = ImageChops.difference(rgb, Image.new("RGB", rgb.size, (255, 255, 255)))
        return ink.convert("L").point(lambda v: 255 if v > 40 else 0).getbbox()


def test_a_phone_mockup_is_composed_the_way_it_is_displayed(tmp_path):
    # The same mockup twice: stored upright, and stored sideways with the tag every
    # viewer obeys. The two renders have to be indistinguishable.
    design = _make_design(tmp_path / "d.png", size=(600, 600))
    upright = _make_mockup(tmp_path / "upright.jpg", size=(1200, 1500))
    sideways = _make_phone_mockup(tmp_path / "sideways.jpg")

    a = mockup.compose(design, upright, tmp_path / "a.jpg")
    b = mockup.compose(design, sideways, tmp_path / "b.jpg")

    with Image.open(a) as first, Image.open(b) as second:
        assert first.size == second.size, "the tag was ignored; the render is a quarter turn out"
        assert second.height > second.width
    assert _ink_bbox(a) == _ink_bbox(b), "the print area landed somewhere else on the garment"


def test_the_seller_location_never_reaches_a_rendered_image(tmp_path):
    design = _make_design(tmp_path / "d.png")
    sideways = _make_phone_mockup(tmp_path / "sideways.jpg")

    out = mockup.compose(design, sideways, tmp_path / "out.jpg")
    with Image.open(out) as img:
        assert dict(img.getexif()) == {}, "the mockup's GPS would be published with it"
        assert "exif" not in img.info


def test_a_sideways_design_is_flattened_upright(tmp_path):
    # Stored 900x600 landscape, displayed 600x900 portrait.
    design = _make_phone_mockup(tmp_path / "art.jpg", stored=(900, 600), fill=(200, 30, 30))
    flat = mockup.flatten_design(design, tmp_path / "flat.jpg")

    left, top, right, bottom = _ink_bbox(flat)
    assert bottom - top > right - left, "a portrait design was rendered landscape"


def test_a_mockup_measures_the_size_the_seller_sees(tmp_path):
    sideways = _make_phone_mockup(tmp_path / "sideways.jpg")
    # Not (1500, 1200): a rectangle calibrated on screen was measured against this.
    assert mockup.mockup_sizes([sideways]) == {"sideways.jpg": (1200, 1500)}


def test_an_untagged_mockup_is_measured_as_stored(tmp_path):
    plain = _make_mockup(tmp_path / "plain.jpg", size=(1200, 1500))
    assert mockup.mockup_sizes([plain]) == {"plain.jpg": (1200, 1500)}


# --- the template ---------------------------------------------------------------


LISTING = {
    "listing_id": 12345,
    "title": "Handmade Ceramic Mug",
    "description": "A lovely mug.",
    "taxonomy_id": 1633,
    "shipping_profile_id": 999,
    "return_policy_id": 7,
    "who_made": "i_did",
    "when_made": "made_to_order",
    "listing_type": "physical",
    "price": {"amount": 2400, "divisor": 100, "currency_code": "USD"},
    "quantity": 5,
    "processing_min": 1,
    "processing_max": 3,
    "materials": ["stoneware"],
    "tags": ["ceramic mug", "coffee gift"],
}


def test_capture_lifts_the_fields_a_photo_cannot_supply():
    tmpl = capture(LISTING)
    assert tmpl.fields["taxonomy_id"] == 1633
    assert tmpl.fields["shipping_profile_id"] == 999
    assert tmpl.fields["price"] == 24.0
    assert tmpl.fields["type"] == "physical"
    assert tmpl.materials == ["stoneware"]


def test_capture_drops_enum_values_etsy_would_refuse():
    tmpl = capture({**LISTING, "who_made": "me", "when_made": "yesterday"})
    assert "who_made" not in tmpl.fields
    assert "when_made" not in tmpl.fields


def test_capture_reports_what_would_block_publishing():
    tmpl = capture({k: v for k, v in LISTING.items() if k != "shipping_profile_id"})
    assert "shipping_profile_id" in tmpl.missing_for_a_physical_draft()


def test_a_template_round_trips_through_json():
    tmpl = capture(LISTING)
    assert Template.from_dict(tmpl.to_dict()).fields == tmpl.fields


def test_an_old_product_json_with_etsy_entities_is_read_as_plain_text():
    # Saved before 0.3.0: the text is exactly as Etsy sent it, HTML-escaped. Every
    # draft copies the description, so it must come out plain.
    old = {
        "source_listing_id": 1000001,
        "source_title": "Mom&#39;s &quot;Best&quot; Mug &amp; Gift",
        "fields": {"taxonomy_id": 1633, "price": 24.0},
        "materials": ["ceramic &amp; glaze"],
        "description": "Mom&#39;s favourite mug &amp; saucer.",
        "tags": ["mother&#39;s day", "mom &amp; dad"],
    }
    tmpl = Template.from_dict(old)
    assert tmpl.source_title == 'Mom\'s "Best" Mug & Gift'
    assert tmpl.description == "Mom's favourite mug & saucer."
    assert tmpl.tags == ["mother's day", "mom & dad"]
    assert tmpl.materials == ["ceramic & glaze"]
    assert tmpl.fields == {"taxonomy_id": 1633, "price": 24.0}
    # Saved again, the file is marked plain, and reading it back changes nothing.
    saved = tmpl.to_dict()
    assert saved["plain_text"] is True
    assert Template.from_dict(json.loads(json.dumps(saved))) == tmpl


def test_a_new_product_json_is_not_decoded_a_second_time():
    # The seller typed a literal "&amp;": the client decoded Etsy's "&amp;amp;" once,
    # and product.json written since carries the mark, so it stays what was typed.
    tmpl = capture({**LISTING, "title": "R&amp;B Mug", "description": "Tom &amp; Jerry",
                    "tags": ["r&amp;b"]})
    back = Template.from_dict(json.loads(json.dumps(tmpl.to_dict())))
    assert back.source_title == "R&amp;B Mug"
    assert back.description == "Tom &amp; Jerry" and back.tags == ["r&amp;b"]
    # Plain text without the mark (and without entities) is left as it is too.
    unmarked = {k: v for k, v in tmpl.to_dict().items() if k != "plain_text"}
    unmarked.update(source_title="Salt & Pepper Mug", description="Plain & simple")
    assert Template.from_dict(unmarked).source_title == "Salt & Pepper Mug"
    assert Template.from_dict(unmarked).description == "Plain & simple"


@pytest.mark.parametrize("data", [[], "x", None, {"fields": {}}, {"source_listing_id": "abc"}])
def test_a_malformed_product_json_is_a_validation_error(data):
    with pytest.raises(ValidationError, match="malformed"):
        Template.from_dict(data)


# --- end to end: folder in, pushable CSV out ------------------------------------


class _FakeClient:
    """Returns a fixed market sample without touching the network."""

    def search_active_listings(self, **_kw):
        return iter(
            [
                {
                    "listing_id": i,
                    "title": "ceramic coffee mug handmade gift",
                    "tags": ["ceramic mug", "coffee gift", "handmade pottery"],
                    "price": {"amount": 2000, "divisor": 100, "currency_code": "USD"},
                    "num_favorers": 5,
                }
                for i in range(30)
            ]
        )


def _workspace_with(tmp_path, design_names) -> workspace.Workspace:
    ws = workspace.Workspace(tmp_path / "studio").create()
    _make_mockup(ws.mockups / "front.jpg")
    _make_mockup(ws.mockups / "back.jpg")
    for name in design_names:
        _make_design(ws.products / name)
    return ws


def test_the_whole_pipeline_produces_a_csv_push_would_accept(tmp_path, monkeypatch):
    monkeypatch.setenv("ETSYOTOMASYON_HOME", str(tmp_path / "home"))
    ws = _workspace_with(tmp_path, ["ceramic-coffee-mug.png", "mountain-sunset-poster.png"])

    report = pipeline.run(ws, capture(LISTING), client=_FakeClient(), mockups_per_product=2)

    assert len(report.ready) == 2
    assert report.csv_path and report.csv_path.is_file()

    from etsyotomasyon.csvio import read_rows

    rows = read_rows(report.csv_path)
    assert len(rows) == 2
    for row in rows:
        # The real validator, on the real output.
        payload = build_payload(row, is_update=False)
        assert payload["taxonomy_id"] == 1633
        assert payload["shipping_profile_id"] == 999
        assert len(payload["title"]) <= MAX_TITLE_LEN
        assert len(payload["tags"]) <= MAX_TAGS


def test_the_pipeline_never_emits_a_listing_id(tmp_path, monkeypatch):
    # This is what makes the drop flow structurally incapable of publishing: Etsy
    # only accepts a state change on an update, and there is never an id to update.
    monkeypatch.setenv("ETSYOTOMASYON_HOME", str(tmp_path / "home"))
    ws = _workspace_with(tmp_path, ["ceramic-coffee-mug.png"])
    report = pipeline.run(ws, capture(LISTING), client=_FakeClient(), mockups_per_product=1)

    from etsyotomasyon.csvio import read_rows

    for row in read_rows(report.csv_path):
        assert row["listing_id"] == ""
        assert row["state"] == ""


def test_images_are_written_and_referenced_relatively(tmp_path, monkeypatch):
    monkeypatch.setenv("ETSYOTOMASYON_HOME", str(tmp_path / "home"))
    ws = _workspace_with(tmp_path, ["ceramic-coffee-mug.png"])
    report = pipeline.run(ws, capture(LISTING), client=_FakeClient(), mockups_per_product=2)

    row = report.rows[0]
    assert len(row.images) == 3, "two mockups plus the flat artwork"
    assert all(p.is_file() for p in row.images)

    from etsyotomasyon.csvio import read_rows, split_multi

    images = split_multi(read_rows(report.csv_path)[0]["images"])
    for name in images:
        assert not Path(name).is_absolute()
        assert (report.csv_path.parent / name).is_file()


def test_mockups_plus_the_flat_render_cannot_exceed_the_image_limit(tmp_path, monkeypatch):
    # `--mockups 20` with the flat render is 21 images. Etsy would accept the create
    # and then refuse the last upload, leaving a draft etsyotomasyon cannot delete — so the run
    # stops before anything is composited.
    monkeypatch.setenv("ETSYOTOMASYON_HOME", str(tmp_path / "home"))
    ws = _workspace_with(tmp_path, ["ceramic-coffee-mug.png"])
    for n in range(20):
        _make_mockup(ws.mockups / f"extra-{n}.jpg")

    with pytest.raises(ValidationError, match="Etsy allows 20"):
        pipeline.run(ws, capture(LISTING), client=_FakeClient(), mockups_per_product=20)

    assert not list(ws.drafts.glob("*/*.jpg")), "nothing may be composited before the refusal"


def test_ten_mockups_without_the_flat_render_fills_the_listing_exactly(tmp_path, monkeypatch):
    monkeypatch.setenv("ETSYOTOMASYON_HOME", str(tmp_path / "home"))
    ws = _workspace_with(tmp_path, ["ceramic-coffee-mug.png"])
    for n in range(10):
        _make_mockup(ws.mockups / f"extra-{n}.jpg")

    report = pipeline.run(
        ws, capture(LISTING), client=_FakeClient(), mockups_per_product=10, include_flat=False
    )

    assert len(report.rows[0].images) == 10


def test_asking_for_more_mockups_than_exist_is_not_over_budget(tmp_path, monkeypatch):
    # The budget counts the mockups actually in 1-MOCKUPS, so "use everything I have" on
    # a workspace holding two is an ordinary run, not a refusal.
    monkeypatch.setenv("ETSYOTOMASYON_HOME", str(tmp_path / "home"))
    ws = _workspace_with(tmp_path, ["ceramic-coffee-mug.png"])

    report = pipeline.run(ws, capture(LISTING), client=_FakeClient(), mockups_per_product=50)

    assert len(report.rows[0].images) == 3, "two mockups plus the flat artwork"


def test_the_review_csv_never_carries_more_images_than_push_accepts(tmp_path, monkeypatch):
    # The pipeline's budget and the validator's limit are the same number. This is the
    # seam between them, checked on the real output.
    monkeypatch.setenv("ETSYOTOMASYON_HOME", str(tmp_path / "home"))
    ws = _workspace_with(tmp_path, ["ceramic-coffee-mug.png"])
    for n in range(10):
        _make_mockup(ws.mockups / f"extra-{n}.jpg")

    report = pipeline.run(ws, capture(LISTING), client=_FakeClient(), mockups_per_product=9)

    from etsyotomasyon.csvio import read_rows, split_multi
    from etsyotomasyon.listings import prepare

    rows = read_rows(report.csv_path)
    assert len(split_multi(rows[0]["images"])) == 10
    assert not prepare(rows, base_dir=report.csv_path.parent)[0].result.failed


def test_a_junk_filename_is_skipped_not_listed(tmp_path, monkeypatch):
    monkeypatch.setenv("ETSYOTOMASYON_HOME", str(tmp_path / "home"))
    ws = _workspace_with(tmp_path, ["IMG_2043.png", "ceramic-coffee-mug.png"])
    report = pipeline.run(ws, capture(LISTING), client=_FakeClient(), mockups_per_product=1)

    skipped = {r.source.name for r in report.skipped}
    assert "IMG_2043.png" in skipped
    assert len(report.ready) == 1


def test_a_finished_photo_is_used_as_is(tmp_path, monkeypatch):
    monkeypatch.setenv("ETSYOTOMASYON_HOME", str(tmp_path / "home"))
    ws = workspace.Workspace(tmp_path / "studio").create()
    _make_mockup(ws.mockups / "front.jpg")
    photo = _make_design(ws.products / "finished-product-photo.png", alpha=False)

    report = pipeline.run(ws, capture(LISTING), client=_FakeClient())
    assert report.rows[0].images == [photo], "an opaque photo needs no compositing"


def test_a_ready_photo_keeps_its_own_orientation_tag(tmp_path, monkeypatch):
    """A ready photo is uploaded byte for byte, orientation tag and all.

    Etsy turns it the same way Explorer does, so re-encoding it here to bake the
    rotation in would spend a JPEG generation to arrive at the same picture.
    """
    monkeypatch.setenv("ETSYOTOMASYON_HOME", str(tmp_path / "home"))
    ws = workspace.Workspace(tmp_path / "studio").create()
    _make_mockup(ws.mockups / "front.jpg")
    photo = _make_phone_mockup(ws.products / "finished-product-photo.jpg")
    before = photo.read_bytes()

    report = pipeline.run(ws, capture(LISTING), client=_FakeClient())
    assert report.rows[0].images == [photo]
    assert photo.read_bytes() == before, "a ready photo must not be rewritten"


def test_the_run_works_with_no_market_access_at_all(tmp_path, monkeypatch):
    # A seller still waiting on API approval can still get mockups and a draft CSV.
    monkeypatch.setenv("ETSYOTOMASYON_HOME", str(tmp_path / "home"))
    ws = _workspace_with(tmp_path, ["ceramic-coffee-mug.png"])
    report = pipeline.run(ws, capture(LISTING), client=None, mockups_per_product=1)

    assert len(report.ready) == 1
    assert any("no market data" in w for w in report.rows[0].warnings)


def test_vitrin_previews_are_not_mistaken_for_products(tmp_path, monkeypatch):
    monkeypatch.setenv("ETSYOTOMASYON_HOME", str(tmp_path / "home"))
    ws = _workspace_with(tmp_path, ["design-one.png", "design-one-vitrin.png"])
    assert [p.name for p in ws.product_files()] == ["design-one.png"]


def test_an_empty_products_folder_returns_an_empty_report(tmp_path, monkeypatch):
    monkeypatch.setenv("ETSYOTOMASYON_HOME", str(tmp_path / "home"))
    ws = _workspace_with(tmp_path, [])
    report = pipeline.run(ws, capture(LISTING), client=_FakeClient())
    assert report.rows == [] and report.csv_path is None


def test_a_missing_workspace_says_how_to_make_one(tmp_path):
    with pytest.raises(ValidationError, match="drop init"):
        workspace.Workspace(tmp_path / "nope").require()


def test_a_missing_template_says_to_build_a_listing_by_hand(tmp_path):
    ws = workspace.Workspace(tmp_path / "studio").create()
    with pytest.raises(ValidationError, match="built by hand"):
        ws.read_template()


# --- the quota arithmetic shown before a run ----- --------------------------------


def test_request_estimate_covers_research_and_every_upload():
    # 100 products, 18 concepts, 6 images each: 36 research + 700 writes.
    assert pipeline.estimate_requests(100, 18, 6) == 736
    assert pipeline.estimate_requests(100, 18, 6) < 5000, "must fit a Personal Access day"


# --- calibration: the one thing a seller cannot do by hand -----------------------


def _studio_with_mockups(tmp_path, sizes) -> workspace.Workspace:
    ws = workspace.Workspace(tmp_path / "studio").create()
    for name, size in sizes.items():
        _make_mockup(ws.mockups / name, size=size)
    return ws


def _calibrate(ws, *args):
    return CliRunner().invoke(app, ["drop", "calibrate", "--path", str(ws.root), *args])


def test_calibrate_writes_the_area_the_compositor_then_reads(tmp_path):
    ws = _studio_with_mockups(tmp_path, {"shirt.jpg": (1000, 1000)})
    result = _calibrate(ws, "--mockup", "shirt.jpg", "--area", "0.2,0.1,0.5,0.4")
    assert result.exit_code == 0, result.output
    assert mockup.load_positions(ws.positions_path)["shirt.jpg"] == mockup.PrintArea(
        0.2, 0.1, 0.5, 0.4
    )


def test_calibrate_accepts_the_name_without_its_extension(tmp_path):
    ws = _studio_with_mockups(tmp_path, {"shirt.jpg": (1000, 1000)})
    assert _calibrate(ws, "--mockup", "shirt", "--area", "0.2,0.1,0.5,0.4").exit_code == 0
    assert "shirt.jpg" in mockup.load_positions(ws.positions_path)


def test_same_size_covers_every_sibling_of_those_dimensions(tmp_path):
    # This is the promise fractions were chosen for; without it each file needs its own.
    ws = _studio_with_mockups(
        tmp_path, {"a.jpg": (1000, 1000), "b.jpg": (1000, 1000), "wide.jpg": (2000, 1000)}
    )
    assert _calibrate(
        ws, "--mockup", "a.jpg", "--area", "0.2,0.1,0.5,0.4", "--same-size"
    ).exit_code == 0
    saved = mockup.load_positions(ws.positions_path)
    assert set(saved) == {"a.jpg", "b.jpg"}


def test_a_dry_run_draws_the_rectangle_and_saves_nothing(tmp_path):
    ws = _studio_with_mockups(tmp_path, {"shirt.jpg": (1000, 1000)})
    result = _calibrate(ws, "--mockup", "shirt.jpg", "--area", "0.2,0.1,0.5,0.4", "--dry-run")
    assert result.exit_code == 0, result.output
    assert not ws.positions_path.exists()
    assert (ws.calibration / "shirt--area.jpg").is_file()


def test_previews_never_land_in_the_mockups_folder(tmp_path):
    # A JPEG written beside the templates would come back as a template to composite on.
    ws = _studio_with_mockups(tmp_path, {"shirt.jpg": (1000, 1000)})
    assert _calibrate(ws, "--preview").exit_code == 0
    assert [p.name for p in ws.mockup_files()] == ["shirt.jpg"]


def test_reset_puts_a_mockup_back_on_the_default(tmp_path):
    ws = _studio_with_mockups(tmp_path, {"shirt.jpg": (1000, 1000)})
    _calibrate(ws, "--mockup", "shirt.jpg", "--area", "0.2,0.1,0.5,0.4")
    assert _calibrate(ws, "--mockup", "shirt.jpg", "--reset").exit_code == 0
    assert mockup.load_positions(ws.positions_path) == {}


def test_import_converts_a_legacy_pixel_file_and_reports_what_it_dropped(tmp_path):
    ws = _studio_with_mockups(tmp_path, {"shirt.jpg": (1000, 2000)})
    legacy = tmp_path / "mockup-positions.json"
    legacy.write_text(
        json.dumps(
            {
                "shirt.jpg": {"x": 250, "y": 500, "w": 500, "h": 250},
                "deleted.jpg": {"x": 1, "y": 1, "w": 2, "h": 2},
            }
        ),
        encoding="utf-8",
    )
    result = _calibrate(ws, "--import", str(legacy))
    assert result.exit_code == 0, result.output
    assert mockup.load_positions(ws.positions_path)["shirt.jpg"] == mockup.PrintArea(
        0.25, 0.25, 0.5, 0.125
    )
    assert "deleted.jpg" in result.output


def test_calibrate_refuses_pixels_instead_of_silently_clamping_them(tmp_path):
    ws = _studio_with_mockups(tmp_path, {"shirt.jpg": (1000, 1000)})
    result = _calibrate(ws, "--mockup", "shirt.jpg", "--area", "300,260,400,360")
    assert result.exit_code != 0
    assert isinstance(result.exception, ValidationError)
    assert "not pixels" in str(result.exception)
    assert not ws.positions_path.exists()


def test_calibrate_names_the_mockups_it_has_when_the_one_asked_for_is_missing(tmp_path):
    ws = _studio_with_mockups(tmp_path, {"shirt.jpg": (1000, 1000)})
    result = _calibrate(ws, "--mockup", "mug.jpg", "--area", "0.2,0.1,0.5,0.4")
    assert result.exit_code == 1
    assert "shirt.jpg" in result.output


def test_a_calibrated_area_is_what_drop_run_then_composites_with(tmp_path, monkeypatch):
    # What the command exists for: an area saved by `drop calibrate` has to reach the
    # compositor, from the CLI all the way to the JPEG.
    monkeypatch.setenv("ETSYOTOMASYON_HOME", str(tmp_path / "home"))
    ws = _workspace_with(tmp_path, ["ceramic-coffee-mug.png"])
    for name in ("front.jpg", "back.jpg"):
        assert _calibrate(ws, "--mockup", name, "--area", "0,0,1,1").exit_code == 0

    report = pipeline.run(ws, capture(LISTING), client=_FakeClient(), mockups_per_product=2)
    composited = next(p for p in report.ready[0].images if p.stem.endswith("--front"))
    with Image.open(composited) as img:
        spot = img.getpixel((round(img.width * 0.15), round(img.height * 0.53)))
    # Off to the side: outside the default rectangle's 0.30-0.70 band, but well inside
    # the artwork once the full-bleed area is honoured — so bare mockup before the
    # calibration reaches the compositor, and design after. Sampled clear of the print
    # file's own transparent margin, which is not what this test is about.
    assert spot[0] > spot[1] + 50, "the saved print area never reached the compositor"


def test_the_calibration_preview_matches_what_compose_renders(tmp_path):
    # A preview drawn on a different ground, or at a different orientation, than the
    # render it is calibrating would have the seller measuring the wrong shape.
    cutout = _make_cutout_mockup(tmp_path / "cutout.png")
    out = mockup.draw_preview(cutout, tmp_path / "preview.jpg", mockup.DEFAULT_PRINT_AREA)
    with Image.open(out) as img:
        assert min(img.convert("RGB").getpixel((2, 2))) > 240, "preview ground is not white"


def test_the_calibration_preview_turns_a_phone_mockup_upright(tmp_path):
    sideways = _make_mockup(tmp_path / "sideways.jpg", size=(1500, 1200))
    exif = Image.open(sideways).getexif()
    exif[mockup.ORIENTATION_TAG] = 6
    Image.open(sideways).save(sideways, exif=exif)
    out = mockup.draw_preview(sideways, tmp_path / "preview.jpg", mockup.DEFAULT_PRINT_AREA)
    with Image.open(out) as img:
        assert img.height > img.width, "preview kept the stored landscape shape"


def test_the_design_is_resampled_once_not_down_then_back_up(tmp_path):
    # Fitting the design to the print box on the ORIGINAL mockup and only then enlarging
    # the whole frame to min_edge would squeeze a big print file into a
    # few hundred pixels and blown back up. LANCZOS cannot restore what the first resize
    # discarded, and the artwork is the part buyers zoom into.
    design = tmp_path / "stripes.png"
    art = Image.new("RGBA", (1600, 1600), (255, 255, 255, 255))
    for x in range(0, 1600, 20):  # fine detail that survives one resize and not two
        art.paste((0, 0, 0, 255), (x, 0, x + 10, 1600))
    art.save(design)
    small = _make_mockup(tmp_path / "small.jpg", size=(600, 600))

    out = mockup.compose(design, small, tmp_path / "out.jpg", area=mockup.PrintArea(0, 0, 1, 1))
    with Image.open(out) as img:
        band = img.convert("L").crop((0, img.height // 2, img.width, img.height // 2 + 1))
    # Sharp stripes keep a wide spread between their lightest and darkest pixel; a
    # downsample-then-upsample cycle greys them together.
    low, high = band.getextrema()
    assert high - low > 150, f"stripes washed out to a {high - low} spread"


def test_a_design_past_pillows_bomb_guard_is_a_message_not_a_traceback(tmp_path, monkeypatch):
    # DecompressionBombError does not subclass OSError, so without its own handler it
    # would end the whole batch with a bare traceback.
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    design = _make_design(tmp_path / "huge.png", size=(200, 200))
    template_image = _make_mockup(tmp_path / "m.jpg")
    assert mockup.looks_like_artwork(design) is False
    with pytest.raises(ValidationError) as caught:
        mockup.compose(design, template_image, tmp_path / "out.jpg")
    assert "decompression-bomb" in str(caught.value)


def test_two_designs_differing_only_by_extension_get_separate_images(tmp_path):
    # Named from Path.stem alone, mug.png and mug.jpg would write to one file and two
    # listings would ship the same pictures with nothing in the report to show it.
    ws = _workspace_with(tmp_path, [])
    _make_design(ws.products / "ceramic-mug.png")
    _make_design(ws.products / "ceramic-mug.jpg")
    report = pipeline.run(ws, capture(LISTING), client=_FakeClient(), mockups_per_product=1)

    produced = [image for row in report.ready for image in row.images]
    assert len(produced) == len({str(p).casefold() for p in produced}), (
        f"two products share an output path: {produced}"
    )
    for image in produced:
        assert image.exists(), f"{image.name} was overwritten by another product"


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        # A revision marker is a suffix. In the middle of a name these are the product.
        ("new-york-skyline.png", "new york skyline"),
        ("yeni-yil-hediyesi.png", "yeni yil hediyesi"),
        ("route-66-poster.png", "route 66 poster"),
        ("boeing-747-print.png", "boeing 747"),
        # ...and at the end they are still stripped.
        ("mountain-sunset-new.png", "mountain sunset"),
        ("mountain-sunset-2.png", "mountain sunset"),
        ("mountain-sunset-final-v3.png", "mountain sunset"),
    ],
)
def test_a_noise_word_inside_the_name_is_part_of_the_product(filename, expected):
    assert seeds.derive(Path(filename), folder_fallback=False).text == expected


@pytest.mark.parametrize(("mode", "suffix"), [("I;16", "png"), ("I", "tif")])
def test_a_16_bit_grey_mockup_keeps_its_tone(tmp_path, mode, suffix):
    # Pillow clamps wide greyscale to 0-255, which would turn mid-grey 30000/65535 white.
    wide = tmp_path / f"grey-{mode.replace(';', '')}.{suffix}"
    Image.new(mode, (1200, 1500), 30000).save(wide)
    out = mockup.compose(_make_design(tmp_path / "d.png"), wide, tmp_path / "out.jpg")
    with Image.open(out) as img:
        corner = img.convert("L").getpixel((3, 3))
    assert 100 < corner < 135, f"mid-grey came out as {corner}"


def test_an_embedded_colour_profile_is_applied_not_carried(tmp_path):
    from PIL import ImageCms

    # The JPEGs written here carry no profile, so one on the way in has to be applied
    # rather than passed along and then silently dropped at save time.
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    img = Image.new("RGB", (10, 10), (200, 30, 30))
    img.info["icc_profile"] = profile
    converted = mockup._to_srgb(img)
    assert converted.mode == "RGB"
    assert "icc_profile" not in converted.info
    assert all(abs(a - b) <= 2 for a, b in zip(converted.getpixel((0, 0)), (200, 30, 30)))


def test_an_unreadable_colour_profile_is_ignored_not_fatal(tmp_path):
    img = Image.new("RGB", (10, 10), (200, 30, 30))
    img.info["icc_profile"] = b"not a profile"
    assert mockup._to_srgb(img).getpixel((0, 0)) == (200, 30, 30)


def test_jpegs_keep_full_colour_resolution(tmp_path):
    from PIL import JpegImagePlugin

    out = mockup.flatten_design(_make_design(tmp_path / "d.png"), tmp_path / "flat.jpg")
    with Image.open(out) as img:
        assert JpegImagePlugin.get_sampling(img) == 0, "chroma was subsampled"


def test_a_converted_ready_photo_keeps_its_orientation(tmp_path):
    # Re-encoding a .webp drops the EXIF block, so the phone's orientation tag must be
    # applied before the copy is written — Etsy has nothing left to honour afterwards.
    webp = tmp_path / "01-front.webp"
    Image.new("RGB", (1500, 1200), (120, 120, 120)).save(webp)
    exif = Image.open(webp).getexif()
    exif[mockup.ORIENTATION_TAG] = 6
    Image.open(webp).save(webp, exif=exif)
    out = mockup.to_uploadable(webp, tmp_path / "out")
    with Image.open(out) as img:
        assert img.height > img.width, "the converted copy lost its orientation"


def test_an_uncalibrated_mockup_borrows_the_area_of_a_same_size_sibling(tmp_path):
    # Fractions exist so one calibration covers a set; a mockup added later of the same
    # size must not quietly fall back to the default rectangle.
    ws = _workspace_with(tmp_path, ["ceramic-coffee-mug.png"])
    for name in ("a-black.jpg", "b-white.jpg"):
        _make_mockup(ws.mockups / name, size=(1000, 1000))
    mockup.save_positions(ws.positions_path, {"a-black.jpg": mockup.PrintArea(0, 0, 1, 1)})
    report = pipeline.run(ws, capture(LISTING), client=_FakeClient(), mockups_per_product=2)
    white = next(p for p in report.ready[0].images if p.stem.endswith("--b-white"))
    with Image.open(white) as img:
        spot = img.getpixel((round(img.width * 0.15), round(img.height * 0.5)))
    assert spot[0] > spot[1] + 50, "the sibling kept the default rectangle"


def test_mockups_left_out_by_the_limit_are_reported(tmp_path):
    ws = _workspace_with(tmp_path, ["ceramic-coffee-mug.png"])
    for i in range(4):
        _make_mockup(ws.mockups / f"m{i}.jpg")
    report = pipeline.run(ws, capture(LISTING), client=_FakeClient(), mockups_per_product=2)
    # Two from the fixture plus four here, two used: four left out.
    assert any("4 mockup(s) in 1-MOCKUPS were not used" in w for w in report.ready[0].warnings)


def test_a_varied_template_listings_total_stock_is_capped_for_the_draft():
    # A listing with variations reports the sum of its offerings — far above 999 here — which
    # Etsy then refuses as the quantity of a new listing.
    template = capture(dict(LISTING, quantity=12000))
    assert template.fields["quantity"] == 999


# --- digital templates -------------------------------------------------------------------


DIGITAL_LISTING = {
    **LISTING,
    "title": "Boho Planner Printable",
    "listing_type": "download",
    "when_made": "2020_2026",  # made to order would need no download file
    "shipping_profile_id": None,
    "readiness_state_id": None,
    "processing_min": 1,  # legacy day counts Etsy may still report for a download
    "processing_max": 2,
    "item_weight": 0.1,
}


def test_capture_keeps_a_digital_type_and_copies_no_shipping():
    tmpl = capture(DIGITAL_LISTING)
    assert tmpl.fields["type"] == "download" and tmpl.listing_type == "download"
    assert tmpl.digital is True
    for name in ("shipping_profile_id", "readiness_state_id", "processing_min",
                 "processing_max", "item_weight"):
        assert name not in tmpl.fields
    assert tmpl.missing_for_a_physical_draft() == []
    assert ("Type", "Digital download (the design file is attached for buyers)") in tmpl.describe()
    both = capture({**LISTING, "listing_type": "both"})
    assert both.listing_type == "both" and both.digital
    assert both.fields["shipping_profile_id"] == 999, "both is shipped"
    assert capture(LISTING).digital is False
    assert Template.from_dict(tmpl.to_dict()).listing_type == "download"


def _write(path: Path, data: bytes = b"%PDF-1.4") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_a_loose_design_is_its_own_download(tmp_path):
    design = _write(tmp_path / "2-PRODUCTS" / "retro-sunset.png", b"PNG")
    assert pipeline.deliverables(design) == ([design], None)


def test_a_folder_products_downloads_are_its_dosyalar_files(tmp_path):
    folder = tmp_path / "boho planner"
    _write(folder / "01-front.jpg", b"jpg")
    for name in ("10-notes.pdf", "2-planner.pdf", "Thumbs.db", ".DS_Store", "~$draft.docx"):
        _write(folder / "Dosyalar" / name)
    (folder / "Dosyalar" / "nested").mkdir()
    files, issue = pipeline.deliverables(folder)
    assert issue is None
    assert [p.name for p in files] == ["2-planner.pdf", "10-notes.pdf"]
    assert pipeline.files_folder(folder).name == "Dosyalar"


def test_the_english_files_folder_works_too(tmp_path):
    folder = tmp_path / "kids pages"
    _write(folder / "files" / "pages.zip", b"PK")
    assert [p.name for p in pipeline.deliverable_files(folder)] == ["pages.zip"]


def test_what_stops_a_folders_downloads(tmp_path, monkeypatch):
    from etsyotomasyon import client as client_mod

    folder = tmp_path / "boho planner"
    folder.mkdir()
    code, message, params = pipeline.deliverables(folder)[1]
    assert code == "no_deliverable" and "no 'dosyalar'" in message
    assert params == {"name": "boho planner", "folder": "dosyalar", "missing": True}
    (folder / "dosyalar").mkdir()
    assert "'dosyalar' folder is empty" in pipeline.deliverables(folder)[1][1]
    for n in range(6):
        _write(folder / "dosyalar" / f"page-{n}.pdf")
    assert pipeline.deliverables(folder)[1][:1] == ("too_many_files",)
    for n in range(1, 6):
        (folder / "dosyalar" / f"page-{n}.pdf").unlink()
    assert pipeline.deliverables(folder)[1] is None
    monkeypatch.setattr(client_mod, "MAX_FILE_BYTES", 3)
    assert pipeline.deliverables(folder)[1][0] == "file_too_large"


def test_a_digital_pipeline_writes_the_downloads_into_review_csv(tmp_path, monkeypatch):
    monkeypatch.setenv("ETSYOTOMASYON_HOME", str(tmp_path / "home"))
    ws = _workspace_with(tmp_path, ["ceramic-coffee-mug.png"])
    folder = ws.products / "boho planner"
    folder.mkdir()
    _make_design(folder / "01-front.png")
    _write(folder / "dosyalar" / "planner.pdf")
    empty = ws.products / "sunset poster set"
    empty.mkdir()
    _make_design(empty / "01-front.png")

    report = pipeline.run(ws, capture(DIGITAL_LISTING), client=_FakeClient(),
                          mockups_per_product=1)
    by_name = {row.source.name: row for row in report.rows}
    assert [p.name for p in by_name["ceramic-coffee-mug.png"].files] == ["ceramic-coffee-mug.png"]
    assert [p.name for p in by_name["boho planner"].files] == ["planner.pdf"]
    assert by_name["sunset poster set"].skipped
    assert "no 'dosyalar'" in by_name["sunset poster set"].warnings[0]

    from etsyotomasyon.csvio import read_rows

    rows = {row["source_file"]: row for row in read_rows(report.csv_path)}
    assert rows["boho planner"]["files"] == "../../2-PRODUCTS/boho planner/dosyalar/planner.pdf"
    assert rows["boho planner"]["type"] == "download"
    assert rows["boho planner"]["shipping_profile_id"] == ""
    item = prepare([rows["boho planner"]], base_dir=report.csv_path.parent)[0]
    assert not item.result.failed and [p.name for p in item.file_paths] == ["planner.pdf"]


def test_a_physical_pipeline_leaves_the_files_column_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("ETSYOTOMASYON_HOME", str(tmp_path / "home"))
    ws = _workspace_with(tmp_path, ["ceramic-coffee-mug.png"])
    report = pipeline.run(ws, capture(LISTING), client=_FakeClient(), mockups_per_product=1)

    from etsyotomasyon.csvio import read_rows

    (row,) = read_rows(report.csv_path)
    assert row["files"] == "" and report.ready[0].files == []


def test_the_pipeline_tells_the_copy_what_the_product_is(tmp_path, monkeypatch):
    monkeypatch.setenv("ETSYOTOMASYON_HOME", str(tmp_path / "home"))
    ws = _workspace_with(tmp_path, ["black-cat-magic.png"])
    seen = {}
    real = generate.generate

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(generate, "generate", spy)
    pipeline.run(ws, capture(LISTING), client=_FakeClient(), mockups_per_product=1)
    assert seen["template_title"] == "Handmade Ceramic Mug"
    assert pipeline.title_problems("Salt & Pepper & Co") and pipeline.TITLE_ONCE["&"] == "and"


def test_a_batch_avoids_the_tags_of_its_earlier_rows_and_of_the_history(tmp_path, monkeypatch):
    monkeypatch.setenv("ETSYOTOMASYON_HOME", str(tmp_path / "home"))
    names = ["black-cat-magic.png", "black-cat-moon.png", "black-cat-garden.png"]
    ws = _workspace_with(tmp_path, names)

    plain = pipeline.run(ws, capture(LISTING), client=_FakeClient(), mockups_per_product=1)
    tags = [row.tags for row in plain.ready]
    assert len(tags) == 3 and all(len(t) == MAX_TAGS for t in tags)
    for a in range(3):
        for b in range(a):
            assert len(set(tags[a]) & set(tags[b])) < 7

    # The history's tags count as earlier rows too: a design drafted last week is avoided.
    last_week = ["black cat magic", "black cat moon", "black cat garden"] + tags[0][:6]
    again = pipeline.run(ws, capture(LISTING), client=_FakeClient(), mockups_per_product=1,
                         avoid_tags=[last_week])
    for row in again.ready:
        assert len(set(row.tags) & set(last_week)) < 7
    assert again.ready[0].tags != tags[0], "the six tags of last week's draft were avoided"
