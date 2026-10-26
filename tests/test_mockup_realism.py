"""Realistic compositing: four corners (perspective), realism (shading, folds), curve."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps
from test_drop_stream import studio  # noqa: F401

from etsyotomasyon.drop import catalog, mockup, surface
from etsyotomasyon.errors import ValidationError

ROOT = Path(__file__).resolve().parents[1]
WHITE = (255, 255, 255)


def solid(size, colour=(200, 40, 40)) -> Image.Image:
    return Image.new("RGBA", size, (*colour, 255))


def placed(canvas, design, area, *, kind=None) -> Image.Image:
    canvas = canvas.copy()
    mockup.place(canvas, design, area, kind=kind)
    return canvas


def diff_stats(a: Image.Image, b: Image.Image) -> tuple[float, int]:
    """(mean, max) absolute difference over all channels."""
    hist = ImageChops.difference(a.convert("RGB"), b.convert("RGB")).convert("L").histogram()
    total = sum(hist)
    mean = sum(value * count for value, count in enumerate(hist)) / total
    top = max(value for value, count in enumerate(hist) if count)
    return mean, top


def is_red(pixel) -> bool:
    r, g, b = pixel[:3]
    return r > 150 and g < 90 and b < 90


# --- the print area itself ---------------------------------------------------------------------


def test_a_quad_area_keeps_its_corners_and_its_bounding_box():
    area = mockup.PrintArea.from_quad([[0.2, 0.25], [0.8, 0.2], [0.75, 0.8], [0.25, 0.85]],
                                      realism=40, curve=0)
    assert (area.x, area.y, area.w, area.h) == (0.2, 0.2, 0.6, 0.65)
    assert area.quad == ((0.2, 0.25), (0.8, 0.2), (0.75, 0.8), (0.25, 0.85))
    assert area.to_dict() == {"x": 0.2, "y": 0.2, "w": 0.6, "h": 0.65,
                              "quad": [[0.2, 0.25], [0.8, 0.2], [0.75, 0.8], [0.25, 0.85]],
                              "realism": 40, "curve": 0}
    assert area.corners(1000, 500)[1] == (800.0, 100.0)


def test_an_old_rectangle_is_written_exactly_as_before():
    assert mockup.PrintArea(0.3, 0.26, 0.4, 0.36).to_dict() == {"x": 0.3, "y": 0.26, "w": 0.4, "h": 0.36}


@pytest.mark.parametrize("corners", [
    [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8]],  # three corners
    [[0.8, 0.2], [0.2, 0.2], [0.2, 0.8], [0.8, 0.8]],  # mirrored (counter-clockwise)
    [[0.2, 0.2], [0.8, 0.8], [0.8, 0.2], [0.2, 0.8]],  # twisted
    [[0.2, 0.2], [0.8, 0.2], [0.4, 0.4], [0.2, 0.8]],  # a dent
    [[0.2, 0.2], [0.5, 0.5], [0.8, 0.8], [0.2, 0.8]],  # a corner flattened into a side
    [[0.2, 0.2], [1.2, 0.2], [0.8, 0.8], [0.2, 0.8]],  # off the mockup
    [[0.2, 0.2], ["a", 0.2], [0.8, 0.8], [0.2, 0.8]],
    "nope",
])
def test_bad_corners_are_refused(corners):
    with pytest.raises(ValidationError):
        mockup.PrintArea.from_quad(corners)


@pytest.mark.parametrize("field, value", [("realism", 101), ("realism", -1), ("curve", True),
                                          ("curve", "50")])
def test_realism_and_curve_are_0_to_100(field, value):
    with pytest.raises(ValidationError):
        mockup.PrintArea(0.1, 0.1, 0.5, 0.5, **{field: value})


def test_type_defaults_fill_in_what_the_area_does_not_say():
    plain = mockup.PrintArea(0.3, 0.26, 0.4, 0.36)
    assert mockup.styled(plain, "tshirt").realism == mockup.REALISM_DEFAULTS["tshirt"]
    assert mockup.styled(plain, "tshirt").curve == 0
    assert mockup.styled(plain, "mug").curve == mockup.CURVE_DEFAULTS["mug"] > 0
    assert mockup.REALISM_DEFAULTS["poster"] < mockup.REALISM_DEFAULTS["tshirt"]
    # Without a type, nothing changes: the flat paste of old.
    assert (mockup.styled(plain, None).realism, mockup.styled(plain, None).curve) == (0, 0)
    own = mockup.PrintArea(0.3, 0.26, 0.4, 0.36, realism=0, curve=10)
    assert (mockup.styled(own, "mug").realism, mockup.styled(own, "mug").curve) == (0, 10)


# --- geometry ----------------------------------------------------------------------------------


def test_corners_land_where_they_are_set():
    canvas = Image.new("RGB", (1000, 1000), WHITE)
    # An isosceles trapezoid: mean width 500, mean height ~608, filled by a design of that shape.
    corners = [[0.3, 0.2], [0.7, 0.2], [0.8, 0.8], [0.2, 0.8]]
    area = mockup.PrintArea.from_quad(corners, realism=0, curve=0)
    out = placed(canvas, solid((500, 608)), area)
    for x, y in ((310, 212), (690, 212), (788, 790), (212, 790), (500, 500)):
        assert is_red(out.getpixel((x, y))), (x, y)
    for x, y in ((290, 190), (710, 190), (812, 812), (188, 812), (240, 300), (760, 300)):
        assert out.getpixel((x, y)) == WHITE, (x, y)


def test_the_edges_of_a_perspective_design_are_anti_aliased():
    canvas = Image.new("RGB", (600, 600), WHITE)
    area = mockup.PrintArea.from_quad([[0.1, 0.1], [0.9, 0.2], [0.85, 0.9], [0.15, 0.85]],
                                      realism=0, curve=0)
    out = placed(canvas, solid((500, 500)), area)
    # The slanted edges have pixels between white and red (about one per edge pixel), not
    # only the two colours a hard cut would leave.
    between = sum(out.convert("RGB").getchannel("G").point(lambda g: 255 if 70 < g < 225 else 0)
                  .histogram()[255:])
    assert between > 900


def test_a_rectangle_with_nothing_on_is_the_old_paste_pixel_for_pixel():
    canvas = Image.new("RGB", (900, 700), (90, 120, 160))
    design = Image.new("RGBA", (300, 200), (0, 0, 0, 0))
    ImageDraw.Draw(design).ellipse((10, 10, 290, 190), fill=(250, 200, 30, 255))
    area = mockup.PrintArea(0.2, 0.25, 0.5, 0.4)
    # What compose() did before: fit, LANCZOS, centre, paste.
    old = canvas.copy()
    bx, by, bw, bh = area.pixels(*old.size)
    scale = min(bw / design.width, bh / design.height)
    size = (max(1, round(design.width * scale)), max(1, round(design.height * scale)))
    resized = design.resize(size, Image.LANCZOS)
    old.paste(resized, (bx + (bw - size[0]) // 2, by + (bh - size[1]) // 2), resized)
    assert ImageChops.difference(placed(canvas, design, area), old).getbbox() is None
    off = mockup.PrintArea(0.2, 0.25, 0.5, 0.4, realism=0, curve=0)
    assert ImageChops.difference(placed(canvas, design, off, kind="tshirt"), old).getbbox() is None


def test_four_corners_on_a_rectangle_match_the_rectangle():
    canvas = Image.new("RGB", (800, 800), (70, 70, 70))
    design = Image.new("RGBA", (400, 300), (0, 0, 0, 0))
    ImageDraw.Draw(design).rectangle((40, 40, 360, 260), fill=(20, 200, 120, 255))
    ImageDraw.Draw(design).line((0, 150, 400, 150), fill=(255, 255, 255, 255), width=6)
    rect = mockup.PrintArea(0.25, 0.25, 0.5, 0.4)
    quad = mockup.PrintArea.from_quad(
        [[0.25, 0.25], [0.75, 0.25], [0.75, 0.65], [0.25, 0.65]], realism=0, curve=0)
    mean, _top = diff_stats(placed(canvas, design, rect), placed(canvas, design, quad))
    assert mean < 1.0


# --- realism -----------------------------------------------------------------------------------


def test_realism_changes_nothing_on_a_flat_area():
    canvas = Image.new("RGB", (800, 800), (182, 182, 182))
    design = solid((300, 300), (30, 90, 200))
    flat = placed(canvas, design, mockup.PrintArea(0.2, 0.2, 0.5, 0.5, realism=0))
    rigid = placed(canvas, design, mockup.PrintArea(0.2, 0.2, 0.5, 0.5, realism=100), kind="poster")
    assert ImageChops.difference(flat, rigid).getbbox() is None
    # On fabric the design also follows folds; a flat shirt has none to follow.
    fabric = placed(canvas, design, mockup.PrintArea(0.2, 0.2, 0.5, 0.5, realism=100), kind="tshirt")
    mean, _top = diff_stats(flat, fabric)
    assert mean < 1.0


def fold_canvas(size=(800, 800)) -> Image.Image:
    """A white shirt with one dark vertical fold through the middle of the area."""
    canvas = Image.new("RGB", size, (236, 236, 236))
    ImageDraw.Draw(canvas).rectangle((380, 0, 420, size[1]), fill=(150, 150, 150))
    return canvas


def test_a_fold_darkens_the_print_and_the_colours_stay_elsewhere():
    canvas = fold_canvas()
    design = solid((400, 400), (240, 160, 60))
    area = mockup.PrintArea(0.25, 0.25, 0.5, 0.5, realism=80)
    out = placed(canvas, design, area, kind="poster")
    flat = placed(canvas, design, mockup.PrintArea(0.25, 0.25, 0.5, 0.5, realism=0))
    in_fold, beside = out.getpixel((400, 400)), out.getpixel((300, 400))
    assert sum(in_fold) < sum(flat.getpixel((400, 400))) - 60
    # The garment is white: away from the fold the design keeps its own colour.
    assert all(abs(a - b) <= 3 for a, b in zip(beside, (240, 160, 60)))


def test_highlights_lift_the_print():
    canvas = Image.new("RGB", (800, 800), (120, 120, 120))
    ImageDraw.Draw(canvas).rectangle((380, 0, 420, 800), fill=(210, 210, 210))
    out = placed(canvas, solid((400, 400), (40, 40, 160)),
                 mockup.PrintArea(0.25, 0.25, 0.5, 0.5, realism=80), kind="poster")
    assert sum(out.getpixel((400, 400))) > sum(out.getpixel((300, 400))) + 30


def test_on_fabric_the_design_follows_the_fold():
    canvas = fold_canvas()
    design = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
    for x in range(0, 400, 20):  # a grid of thin lines shows any displacement
        ImageDraw.Draw(design).line((x, 0, x, 400), fill=(0, 0, 0, 255), width=2)
        ImageDraw.Draw(design).line((0, x, 400, x), fill=(0, 0, 0, 255), width=2)
    area = mockup.PrintArea(0.25, 0.25, 0.5, 0.5, realism=100)
    rigid = placed(canvas, design, area, kind="poster")
    fabric = placed(canvas, design, area, kind="tshirt")
    near = (360, 200, 440, 600)
    assert ImageChops.difference(rigid.crop(near), fabric.crop(near)).getbbox() is not None
    far = (210, 210, 300, 300)  # away from the fold the lines stay put
    mean, _top = diff_stats(rigid.crop(far), fabric.crop(far))
    assert mean < 3


# --- curve -------------------------------------------------------------------------------------


def stripes(size=(400, 400), every=50) -> Image.Image:
    """Dark lines on red, mirror-symmetric: pixel i mirrors to width - 1 - i."""
    design = Image.new("RGBA", size, (230, 60, 50, 255))
    draw = ImageDraw.Draw(design)
    for x in range(every // 2, size[0], every):
        draw.rectangle((x - 3, 0, x + 2, size[1]), fill=(20, 20, 20, 255))
    return design


def test_the_curve_is_symmetric():
    canvas = Image.new("RGB", (800, 800), WHITE)
    area = mockup.PrintArea(0.25, 0.2, 0.5, 0.6, realism=0, curve=70)
    out = placed(canvas, stripes(), area, kind="mug")
    mean, _top = diff_stats(out, ImageOps.mirror(out))
    assert mean < 1.0
    assert ImageChops.difference(out, placed(canvas, stripes(), mockup.PrintArea(
        0.25, 0.2, 0.5, 0.6, realism=0, curve=0))).getbbox() is not None


def line_centres(row: list[tuple[int, int, int]]) -> list[float]:
    centres, run = [], []
    for x, pixel in enumerate(row):
        if sum(pixel) < 200:
            run.append(x)
        elif run:
            centres.append(sum(run) / len(run))
            run = []
    return centres


def test_the_curve_squeezes_the_sides_and_shades_them():
    canvas = Image.new("RGB", (800, 800), WHITE)
    area = mockup.PrintArea(0.2, 0.3, 0.6, 0.4, realism=0, curve=80)
    out = placed(canvas, stripes((800, 300), every=100), area, kind="mug")
    gaps = [b - a for a, b in zip(*(lambda c: (c, c[1:]))(line_centres(
        [out.getpixel((x, 400)) for x in range(800)])))]
    # Evenly spaced on the design; round the cylinder the gaps shrink towards the sides.
    assert len(gaps) == 7
    assert gaps[3] > gaps[1] + 8 and gaps[3] > gaps[5] + 8 and gaps[0] < gaps[2]
    # Darker towards the edges than in the middle (both on the red, between lines).
    red = [x for x in range(160, 640) if out.getpixel((x, 400))[0] > 100]
    assert sum(out.getpixel((red[0] + 2, 400))) < sum(out.getpixel((400, 400))) - 25


def test_mugs_are_curved_by_default_and_shirts_are_not():
    canvas = Image.new("RGB", (800, 800), WHITE)
    area = mockup.PrintArea(0.25, 0.2, 0.5, 0.6)
    flat = placed(canvas, stripes(), area)
    assert diff_stats(placed(canvas, stripes(), area, kind="mug"), flat)[0] > 5
    # A shirt's default realism changes nothing on a flat white area.
    assert diff_stats(placed(canvas, stripes(), area, kind="tshirt"), flat)[0] < 1


def test_the_curve_keeps_a_tall_design_inside_its_area():
    # The arc bends the design's middle down and its edges up; the fit leaves room for it.
    canvas = Image.new("RGB", (1250, 1000), WHITE)
    area = mockup.PrintArea(0.36, 0.34, 0.22, 0.30, realism=0, curve=100)
    out = placed(canvas, solid((400, 800)), area, kind="mug")
    box_x, box_y, box_w, box_h = area.pixels(*canvas.size)
    left, top, right, bottom = ImageChops.difference(out, canvas).getbbox()
    # Inside, but for the one row of anti-aliasing an edge that fits exactly has
    # (before, the arc pushed the middle 19 px past the bottom).
    assert box_x <= left and right <= box_x + box_w
    assert box_y <= top and bottom <= box_y + box_h + 1
    assert sum(out.getpixel(((left + right) // 2, box_y + box_h))) > 3 * 225
    assert bottom > box_y + box_h - 4  # still filled to the bottom


def highlight_canvas(size=(1000, 1000)) -> Image.Image:
    """Grey with soft horizontal highlight bands, as on a steel tumbler."""
    canvas = Image.new("RGB", size, (120, 120, 120))
    draw = ImageDraw.Draw(canvas)
    for y in range(0, size[1], 90):
        draw.rectangle((0, y, size[0], y + 18), fill=(235, 235, 235))
    return canvas.filter(ImageFilter.GaussianBlur(6))


def test_a_curved_other_product_is_rigid_and_its_edges_stay_straight():
    canvas = highlight_canvas()
    design = Image.new("RGBA", (400, 400), (40, 60, 200, 255))
    ImageDraw.Draw(design).rectangle((0, 0, 199, 400), fill=(220, 30, 30, 255))
    area = mockup.PrintArea(0.25, 0.25, 0.5, 0.5, realism=35, curve=55)
    other = placed(canvas, design, area, kind="other")
    # The same as a mug (rigid), and the red/blue boundary is one straight column.
    assert ImageChops.difference(other, placed(canvas, design, area, kind="mug")).getbbox() is None
    edges = set()
    for y in range(300, 700, 7):
        row = [other.getpixel((x, y)) for x in range(420, 580)]
        edges.add(next(i for i, p in enumerate(row) if p[2] > p[0]))
    assert max(edges) - min(edges) <= 1
    # A shirt with the same realism and no curve still follows its folds.
    flat = mockup.PrintArea(0.25, 0.25, 0.5, 0.5, realism=35, curve=0)
    assert ImageChops.difference(placed(canvas, design, flat, kind="tshirt"),
                                 placed(canvas, design, flat, kind="poster")).getbbox() is not None


# --- memory -------------------------------------------------------------------------------------

STEEP = [[0.42, 0.15], [0.58, 0.15], [0.95, 0.85], [0.05, 0.85]]


def record_transforms(monkeypatch) -> list[tuple[int, int]]:
    sizes = []
    original = Image.Image.transform

    def transform(self, size, *args, **kwargs):
        sizes.append(tuple(size))
        return original(self, size, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "transform", transform)
    return sizes


def test_supersampling_a_steep_area_on_a_large_mockup_stays_within_budget(monkeypatch):
    sizes = record_transforms(monkeypatch)
    canvas = Image.new("RGB", (4000, 4000), WHITE)
    area = mockup.PrintArea.from_quad(STEEP, realism=0, curve=0)
    assert surface.is_well_shaped(area.corners(1, 1))
    out = placed(canvas, solid((800, 600)), area, kind="poster")
    assert len(sizes) > 1  # supersampled, in bands
    assert all(w * h <= surface.SUPERSAMPLE_PIXELS for w, h in sizes)
    assert sum(w * h for w, h in sizes) > 2 * 3600 * 2800  # at more than the output size
    # The design's middle lands where the diagonals cross, near the far (narrow) side.
    assert is_red(out.getpixel((2000, 1100))) and out.getpixel((2000, 3000)) == WHITE


@pytest.mark.parametrize("realism, curve, kind", [(0, 0, "poster"), (60, 0, "tshirt"), (0, 70, "mug")])
def test_bands_draw_exactly_what_one_pass_draws(monkeypatch, realism, curve, kind):
    canvas = fold_canvas((600, 600))
    area = mockup.PrintArea.from_quad(STEEP, realism=realism, curve=curve)
    one = placed(canvas, stripes((300, 240), every=30), area, kind=kind)
    sizes = record_transforms(monkeypatch)
    monkeypatch.setattr(surface, "SUPERSAMPLE_PIXELS", 300_000)
    banded = placed(canvas, stripes((300, 240), every=30), area, kind=kind)
    assert len(sizes) > 3 and all(w * h <= 300_000 for w, h in sizes)
    mean, top = diff_stats(one, banded)
    assert mean < 0.01 and top <= 2


# --- compose, positions.json, the catalog ------------------------------------------------------------


def test_compose_takes_the_type_and_still_writes_a_jpeg(tmp_path):
    mock = tmp_path / "mug.png"
    Image.new("RGB", (600, 400), (240, 240, 240)).save(mock)
    art = tmp_path / "art.png"
    stripes((200, 200)).save(art)
    out = mockup.compose(art, mock, tmp_path / "out.jpg", kind="mug",
                         area=mockup.PrintArea.from_quad([[0.3, 0.2], [0.7, 0.25], [0.7, 0.8], [0.3, 0.85]]))
    with Image.open(out) as image:
        assert image.format == "JPEG" and min(image.size) == mockup.OUTPUT_MIN_EDGE


def test_an_old_positions_file_loads_and_is_written_back_unchanged(tmp_path):
    path = tmp_path / "positions.json"
    old = {"a.png": {"x": 0.3, "y": 0.26, "w": 0.4, "h": 0.36},
           "b.png": {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5}}
    path.write_text(json.dumps(old, indent=2), encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    loaded = mockup.load_positions(path)
    assert loaded["a.png"] == mockup.PrintArea(0.3, 0.26, 0.4, 0.36)
    assert (loaded["a.png"].quad, loaded["a.png"].realism, loaded["a.png"].curve) == (None, None, None)
    mockup.save_positions(path, loaded)
    assert path.read_text(encoding="utf-8") == before


def test_new_settings_round_trip_and_a_broken_quad_falls_back_to_its_rectangle(tmp_path):
    path = tmp_path / "positions.json"
    area = mockup.PrintArea.from_quad([[0.2, 0.25], [0.8, 0.2], [0.75, 0.8], [0.25, 0.85]],
                                      realism=70, curve=20)
    mockup.save_positions(path, {"a.png": area})
    assert mockup.load_positions(path)["a.png"] == area
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["a.png"]["quad"][1] = [0.4, 0.6]  # a dent, by hand
    raw["a.png"]["realism"] = 140  # an overshoot, by hand
    path.write_text(json.dumps(raw), encoding="utf-8")
    back = mockup.load_positions(path)["a.png"]
    assert back.quad is None and (back.x, back.y, back.w, back.h) == (area.x, area.y, area.w, area.h)
    assert back.realism == 100 and back.curve == 20
    raw["a.png"]["curve"] = "lots"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValidationError):
        mockup.load_positions(path)


@pytest.fixture
def ws(tmp_path):
    from etsyotomasyon.drop.workspace import Workspace

    workspace = Workspace(tmp_path / "Etsy Studio")
    workspace.mockups.mkdir(parents=True)
    workspace.products.mkdir(parents=True)
    return workspace


def test_same_size_save_copies_the_corners_and_settings_and_the_catalog_is_untouched(ws):
    for name in ("a-tshirt.png", "b-tshirt.png", "c-mug.png"):
        Image.new("RGB", (300, 300), WHITE).save(ws.mockups / name)
    catalog.update(ws, "a-tshirt.png", color="Beyaz")
    before = catalog.catalog_path(ws).read_text(encoding="utf-8")
    area = mockup.PrintArea.from_quad([[0.2, 0.25], [0.8, 0.2], [0.75, 0.8], [0.25, 0.85]],
                                      realism=30)
    assert catalog.save_area(ws, "a-tshirt.png", area, same_size=True) == [
        "a-tshirt.png", "b-tshirt.png", "c-mug.png"]
    stored = mockup.load_positions(ws.positions_path)
    assert stored["b-tshirt.png"] == area and stored["c-mug.png"].quad == area.quad
    assert catalog.catalog_path(ws).read_text(encoding="utf-8") == before
    # The mug follows its own type for what the area leaves open (the curve).
    infos = catalog.load(ws)
    mug = mockup.styled(stored["c-mug.png"], catalog.kind_of(infos, "c-mug.png"))
    assert (mug.realism, mug.curve) == (30, mockup.CURVE_DEFAULTS["mug"])


def test_a_same_size_save_keeps_a_siblings_own_realism_and_curve_when_left_on_default(ws):
    for name in ("a-tshirt.png", "b-tote.png", "c-mug.png"):
        Image.new("RGB", (300, 300), WHITE).save(ws.mockups / name)
    catalog.save_area(ws, "b-tote.png", mockup.PrintArea(0.1, 0.1, 0.5, 0.5, realism=20))
    catalog.save_area(ws, "c-mug.png", mockup.PrintArea(0.1, 0.1, 0.5, 0.5, realism=5, curve=90))
    area = mockup.PrintArea(0.3, 0.3, 0.4, 0.4)  # the editor's sliders left untouched
    assert catalog.save_area(ws, "a-tshirt.png", area, same_size=True) == [
        "a-tshirt.png", "b-tote.png", "c-mug.png"]
    stored = mockup.load_positions(ws.positions_path)
    assert stored["a-tshirt.png"] == area
    assert stored["b-tote.png"] == mockup.PrintArea(0.3, 0.3, 0.4, 0.4, realism=20)
    assert stored["c-mug.png"] == mockup.PrintArea(0.3, 0.3, 0.4, 0.4, realism=5, curve=90)
    # A value the seller did set goes to every mockup of the size; the other one stays.
    catalog.save_area(ws, "a-tshirt.png", mockup.PrintArea(0.3, 0.3, 0.4, 0.4, realism=70),
                      same_size=True)
    stored = mockup.load_positions(ws.positions_path)
    assert (stored["b-tote.png"].realism, stored["b-tote.png"].curve) == (70, None)
    assert (stored["c-mug.png"].realism, stored["c-mug.png"].curve) == (70, 90)
    # The mockup being edited takes the area as sent: None puts it back on its default.
    catalog.save_area(ws, "c-mug.png", mockup.PrintArea(0.3, 0.3, 0.4, 0.4))
    assert mockup.load_positions(ws.positions_path)["c-mug.png"] == mockup.PrintArea(0.3, 0.3, 0.4, 0.4)


def test_the_cli_area_keeps_realism_and_curve(ws):
    Image.new("RGB", (300, 300), WHITE).save(ws.mockups / "mug.png")
    catalog.save_area(ws, "mug.png", mockup.PrintArea(0.2, 0.2, 0.5, 0.5, realism=10, curve=90))
    catalog.save_area(ws, "mug.png", mockup.PrintArea(0.3, 0.3, 0.4, 0.4), keep_style=True)
    assert mockup.load_positions(ws.positions_path)["mug.png"] == mockup.PrintArea(
        0.3, 0.3, 0.4, 0.4, realism=10, curve=90)


def test_kind_of_reads_the_catalog_then_the_file_name(ws):
    Image.new("RGB", (50, 50), WHITE).save(ws.mockups / "kupa-beyaz.png")
    Image.new("RGB", (50, 50), WHITE).save(ws.mockups / "photo.png")
    catalog.update(ws, "photo.png", type="tote")
    infos = catalog.load(ws)
    assert catalog.kind_of(infos, "kupa-beyaz.png") == "mug"
    assert catalog.kind_of(infos, "photo.png") == "tote"
    assert catalog.kind_of(None, "tshirt-white.jpg") == "tshirt"


# --- the editor's strings --------------------------------------------------------------------------


def test_every_editor_string_the_page_uses_exists_in_both_languages():
    strings = json.loads((ROOT / "etsyotomasyon/web/static/i18n/mockups.json").read_text(encoding="utf-8"))
    source = (ROOT / "etsyotomasyon/web/static/js/pages/mockups.js").read_text(encoding="utf-8")
    used = set(re.findall(r'"(editor\.[a-z_]+)"', source))
    used |= {f"editor.corner_{c}" for c in ("tl", "tr", "br", "bl")}
    assert {"editor.realism", "editor.curve", "editor.mode_quad", "editor.corners_hint"} <= used
    for lang in ("tr", "en"):
        missing = used - set(strings[lang])
        assert not missing, (lang, missing)
    assert strings["tr"]["editor.realism"] == "Gerçekçilik" and strings["tr"]["editor.curve"] == "Kavis"
    assert strings["tr"]["editor.mode_quad"] == "4 köşe"


def test_the_surface_math_maps_corners_exactly():
    src = [(10.0, 20.0), (300.0, 40.0), (280.0, 260.0), (30.0, 240.0)]
    coeffs = surface.perspective_coeffs(src, surface.UNIT_SQUARE)
    for point, target in zip(src, surface.UNIT_SQUARE):
        u, v = surface.apply(coeffs, *point)
        assert u == pytest.approx(target[0], abs=1e-9) and v == pytest.approx(target[1], abs=1e-9)
    assert surface.corner_share(surface.rect_corners(0, 0, 4, 2)) == pytest.approx(0.5)


# --- every run composites through the same door ----------------------------------------------------


def test_the_app_run_composites_with_each_mockups_type_and_area(studio, monkeypatch):  # noqa: F811
    from test_drop_stream import Client, _artwork, _run

    ws, template = studio
    catalog.save_area(ws, "mug-white.jpg", mockup.PrintArea(0.3, 0.3, 0.4, 0.4, curve=70))
    _artwork(ws.products / "lemon.png")
    calls = []
    small = mockup.compose  # the fixture's quick stand-in

    def spy(design, template_image, out, **kw):
        calls.append((template_image.name, kw.get("kind"), kw.get("area")))
        return small(design, template_image, out, **kw)

    monkeypatch.setattr(mockup, "compose", spy)
    _run(ws, template, Client(ws), dry_run=True)
    assert ("mug-white.jpg", "mug", mockup.PrintArea(0.3, 0.3, 0.4, 0.4, curve=70)) in calls
    # The same-size shirt borrows where the print goes, not the mug's curve.
    assert ("tshirt-white.jpg", "tshirt", mockup.PrintArea(0.3, 0.3, 0.4, 0.4)) in calls


def test_the_cli_pipeline_composites_with_each_mockups_type_and_area(tmp_path, monkeypatch):
    from test_drop import LISTING, _FakeClient, _workspace_with, capture

    from etsyotomasyon.drop import pipeline

    monkeypatch.setenv("ETSYOTOMASYON_HOME", str(tmp_path / "home"))
    ws = _workspace_with(tmp_path, ["ceramic-coffee-mug.png"])
    quad = mockup.PrintArea.from_quad([[0.2, 0.25], [0.8, 0.2], [0.75, 0.8], [0.25, 0.85]],
                                      realism=40)
    catalog.save_area(ws, "front.jpg", quad)
    catalog.update(ws, "back.jpg", type="mug")
    calls = []
    real = mockup.compose

    def spy(design, template_image, out, **kw):
        calls.append((template_image.name, kw.get("kind"), kw.get("area")))
        return real(design, template_image, out, **kw)

    monkeypatch.setattr(mockup, "compose", spy)
    report = pipeline.run(ws, capture(LISTING), client=_FakeClient(), mockups_per_product=2)
    assert len(report.ready) == 1
    assert sorted(calls) == [("back.jpg", "mug", quad.geometry()), ("front.jpg", "other", quad)]
