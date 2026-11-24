"""Wallpaper scenes: the pattern repeated across a wall, lit and marked (drop.wall)."""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw, ImageOps

from etsyotomasyon.drop import mockup, surface, wall
from etsyotomasyon.errors import ValidationError


def pattern(size=(60, 40), dark=False):
    base, ink = ((30, 30, 40), (90, 80, 70)) if dark else ((240, 235, 225), (150, 150, 140))
    image = Image.new("RGBA", size, base + (255,))
    ImageDraw.Draw(image).ellipse((10, 8, 40, 32), fill=ink + (255,))
    return image


def test_the_repeats_follow_the_level_and_stay_in_range():
    assert wall.repeats(0) == 1 and wall.repeats(100) == wall.MAX_REPEATS
    assert [wall.repeats(v) for v in (10, 40, 70)] == sorted(wall.repeats(v) for v in (10, 40, 70))
    assert wall.repeats(40) == 5 and wall.repeats(-5) == 1 and wall.repeats(500) == wall.MAX_REPEATS


def test_a_wall_picture_has_the_walls_proportions_and_the_designs_repeats():
    design = pattern((60, 40))
    out = wall.tiled(design, 40, aspect=2.0)  # 5 repeats across
    assert out.size[0] == 60 * 5 and out.size[1] == round(out.size[0] / 2.0)
    # the third column repeats the first (the pattern, not one design stretched)
    cell = out.size[0] // 5
    assert out.getpixel((cell // 2, 20)) == out.getpixel((cell * 2 + cell // 2, 20))
    assert wall.tiled(design, 100, 1.0, max_edge=500).size[0] == 500  # capped


def test_dark_patterns_are_warmed_light_ones_sharpened_and_only_on_the_wall():
    corners = surface.rect_corners(0, 0, 100, 60)
    dark = Image.new("RGB", (100, 100), (40, 40, 40))
    wall.light(dark, corners, wall.luminance(pattern(dark=True)))
    inside, outside = dark.getpixel((50, 30)), dark.getpixel((50, 90))
    assert inside[0] > 40 and inside[0] > inside[2]  # lifted and warmer
    assert outside == (40, 40, 40)
    light = Image.new("RGB", (100, 100), (200, 200, 200))
    before = light.copy()
    wall.light(light, corners, 0.5)  # a mid-tone pattern is left as it is
    assert light.tobytes() == before.tobytes()
    assert wall.luminance(pattern(dark=True)) < wall.DARK < wall.LIGHT < wall.luminance(pattern())


def test_daylight_comes_from_the_brighter_side_and_only_touches_the_wall():
    corners = surface.rect_corners(0, 0, 200, 120)
    room = Image.new("RGB", (200, 200), (200, 200, 200))
    ImageDraw.Draw(room).rectangle((100, 0, 200, 120), fill=(120, 120, 120))  # the right is darker
    wall_pic = Image.new("RGB", (200, 200), (150, 150, 150))
    before = wall_pic.copy()
    assert wall.daylight(wall_pic, corners, room) == 1  # the light is on the left
    assert wall_pic.getpixel((10, 60))[0] > wall_pic.getpixel((190, 60))[0]  # the left is brighter
    assert wall_pic.getpixel((10, 60))[0] > 150 > wall_pic.getpixel((190, 60))[0] - 1
    assert wall_pic.getpixel((10, 60))[0] > wall_pic.getpixel((10, 60))[2]  # warm
    assert wall_pic.crop((0, 150, 200, 200)).tobytes() == before.crop((0, 150, 200, 200)).tobytes()
    flipped = ImageOps.mirror(room)
    other = before.copy()
    assert wall.daylight(other, corners, flipped) == -1
    assert other.getpixel((190, 60))[0] > other.getpixel((10, 60))[0]


def test_the_mark_draws_a_frame_and_a_label_on_the_wall():
    canvas = Image.new("RGB", (800, 600), (230, 225, 215))
    before = canvas.copy()
    wall.mark(canvas, surface.rect_corners(0, 0, 800, 360), 40, (60, 40), 800 / 360)
    assert canvas.tobytes() != before.tobytes()
    # nothing is drawn below the wall's bottom edge plus the label's room
    assert canvas.crop((0, 500, 800, 600)).tobytes() == before.crop((0, 500, 800, 600)).tobytes()


def test_a_repeat_too_small_to_frame_is_left_alone():
    canvas = Image.new("RGB", (800, 600), (230, 225, 215))
    before = canvas.copy()
    wall.mark(canvas, surface.rect_corners(0, 0, 800, 360), 100, (60, 6), 800 / 360)
    assert canvas.tobytes() == before.tobytes()


def test_the_cover_check_wants_a_big_wall_and_a_pattern_that_shows_small():
    corners_big, corners_small = surface.rect_corners(0, 0, 400, 300), surface.rect_corners(0, 0, 400, 60)
    busy = wall.tiled(pattern(), 80, 400 / 300).convert("RGB").resize((400, 300))
    busy = ImageOps.pad(busy, (400, 400), color=(230, 225, 215), centering=(0, 0))
    result = wall.check(busy, corners_big)
    assert result["wall_share"] == pytest.approx(0.75, abs=0.02) and "small_wall" not in result["problems"]
    assert "small_wall" in wall.check(busy, corners_small)["problems"]
    flat = Image.new("RGB", (400, 400), (225, 220, 210))
    assert "flat_pattern" in wall.check(flat, corners_big)["problems"]


def test_compose_without_a_tile_is_the_old_paste_and_with_one_the_wall_is_covered(tmp_path):
    room = Image.new("RGB", (400, 400), (230, 225, 215))
    room.save(tmp_path / "room.png")
    pattern().save(tmp_path / "p.png")
    area = mockup.PrintArea(0.0, 0.0, 1.0, 0.5)
    plain = mockup.compose(tmp_path / "p.png", tmp_path / "room.png", tmp_path / "a.jpg",
                           area=area, min_edge=0)
    tiled = mockup.compose(tmp_path / "p.png", tmp_path / "room.png", tmp_path / "b.jpg",
                           area=mockup.PrintArea(0.0, 0.0, 1.0, 0.5, tile=50), min_edge=0)
    a, b = Image.open(plain).convert("RGB"), Image.open(tiled).convert("RGB")
    assert a.getpixel((5, 5)) == pytest.approx((230, 225, 215), abs=6)  # a 3:2 design letterboxed
    assert b.getpixel((5, 5)) != pytest.approx((230, 225, 215), abs=6)  # the wall corner has pattern
    assert mockup.PrintArea(0, 0, 1, 1, tile=40).to_dict()["tile"] == 40
    assert "tile" not in mockup.PrintArea(0, 0, 1, 1).to_dict()
    with pytest.raises(ValidationError):
        mockup.PrintArea(0, 0, 1, 1, tile=101)
