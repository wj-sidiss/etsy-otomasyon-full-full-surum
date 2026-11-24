"""Wallpaper scenes: a pattern repeated across a wall, lit to suit it and marked as a repeat.

A wallpaper buyer is buying how a room will look, not one motif. A cover photo that shows
a single design pasted on the wall hides that; what sells is the wall covered with the
pattern, the pattern readable even in Etsy's small search thumbnail, the light kind to it,
and a hint of where it repeats. This is what `mockup.compose` calls for an area with a
`tile` setting (0-100; 0 is off, the design pasted whole as before):

- `tiled` makes the wall's picture: the design repeated `repeats(tile)` times across the
  wall's width, in the wall's own proportions, so it fills the area like any other design
  (and the area's realism, perspective and folds apply to it as usual);
- `light` warms and lifts a dark pattern a little and keeps a light one crisp, only on the wall;
- `daylight` lays soft, warm window light across the wall from the side of the room that is
  brighter in the photo, fading to the far side, so the pattern does not look pasted flat;
- `mark` draws a dashed frame round one repeat with a small label, as the best covers do;
- `check` says whether a cover will do its job in a thumbnail: how much of the picture the
  wall takes, and whether the pattern still shows at that size.
"""

from __future__ import annotations

from collections.abc import Sequence

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageStat

from . import surface

MAX_REPEATS = 12
MAX_EDGE = 3600
DARK, LIGHT = 0.38, 0.62
WARM = (255, 246, 232)
LABEL = "Seamless repeat"
THUMB = 340  # the width Etsy shows a search result at, about
MIN_WALL_SHARE = 0.45
MIN_THUMB_DETAIL = 14.0


def repeats(level: int) -> int:
    """How many times the pattern repeats across the wall at a `tile` level of 1-100."""
    return max(1, min(MAX_REPEATS, round(1 + (MAX_REPEATS - 1) * max(0, min(100, level)) / 100)))


def tiled(design: Image.Image, level: int, aspect: float, *, max_edge: int = MAX_EDGE) -> Image.Image:
    """`design` (RGBA) repeated across a picture `aspect` (width / height) wide, at the
    number of repeats `level` asks for. Transparent parts of a design show white."""
    n = repeats(level)
    target_w = max(1, min(max_edge, design.width * n))
    target_h = max(1, round(target_w / max(aspect, 0.05)))
    cell_w = target_w / n
    cell_h = cell_w * design.height / design.width
    cell = design.resize((max(1, round(cell_w)), max(1, round(cell_h))), Image.LANCZOS)
    out = Image.new("RGBA", (target_w, target_h), (255, 255, 255, 255))
    rows = int(target_h / cell_h) + 1
    for row in range(rows):
        y = round(row * cell_h)
        for col in range(n):
            out.paste(cell, (round(col * cell_w), y), cell)
    return out


def luminance(design: Image.Image) -> float:
    """The design's brightness from 0 (black) to 1 (white), where it is not transparent."""
    small = design.copy()
    small.thumbnail((96, 96))
    rgba = small.convert("RGBA")
    flat = Image.new("RGB", rgba.size, (255, 255, 255))
    flat.paste(rgba, mask=rgba.getchannel("A"))
    return ImageStat.Stat(flat.convert("L")).mean[0] / 255.0


def light(canvas: Image.Image, corners: Sequence[tuple[float, float]], luma: float) -> None:
    """Soften and warm a dark pattern, keep a light one crisp: on the wall only."""
    mask = surface.area_mask(canvas.size, corners).filter(ImageFilter.GaussianBlur(1.2))
    if luma < DARK:
        warm = ImageChops.multiply(canvas, Image.new("RGB", canvas.size, WARM))
        lifted = Image.blend(canvas, warm, 0.7).point(lambda v: min(255, round(v * 1.05 + 2)))
        canvas.paste(lifted, mask=mask)
    elif luma > LIGHT:
        crisp = canvas.filter(ImageFilter.UnsharpMask(radius=2.0, percent=45, threshold=3))
        canvas.paste(crisp, mask=mask)


DAYLIGHT = (255, 236, 205)
DAYLIGHT_STRENGTH = 0.17
FAR_SIDE_SHADE = 0.88


def _side(reference: Image.Image, mask: Image.Image) -> int:
    """+1 when the left half of the wall is the brighter in `reference` (the room photo
    before the pattern went on), -1 when the right is; +1 when they are alike."""
    box = mask.getbbox()
    if box is None:
        return 1
    mid = (box[0] + box[2]) // 2
    gray = reference.convert("L")
    left = ImageStat.Stat(gray.crop((box[0], box[1], mid, box[3])), mask.crop((box[0], box[1], mid, box[3]))).mean[0]
    right = ImageStat.Stat(gray.crop((mid, box[1], box[2], box[3])), mask.crop((mid, box[1], box[2], box[3]))).mean[0]
    return -1 if right > left + 3 else 1


def daylight(canvas: Image.Image, corners: Sequence[tuple[float, float]], reference: Image.Image,
             strength: float = DAYLIGHT_STRENGTH) -> int:
    """Daylight on the wall: warm light from the brighter side of the room, a little more
    from above, fading to the far side, which is shaded a touch. Only on the wall.
    Returns the side the light came from (+1 left, -1 right)."""
    mask = surface.area_mask(canvas.size, corners).filter(ImageFilter.GaussianBlur(1.2))
    box = mask.getbbox()
    side = _side(reference, mask)
    if box is None:
        return side
    width, height = box[2] - box[0], box[3] - box[1]
    across = Image.linear_gradient("L").transpose(Image.ROTATE_90)  # white at the right
    if side > 0:
        across = ImageOps.mirror(across)  # the light is on the left: white there
    down = ImageOps.flip(Image.linear_gradient("L"))  # white at the top
    across = across.resize((width, height), Image.BILINEAR)
    down = down.resize((width, height), Image.BILINEAR)
    field = Image.new("L", canvas.size, 0)
    field.paste(ImageChops.add(across.point(lambda v: round(v * 0.72)),
                               down.point(lambda v: round(v * 0.28))), (box[0], box[1]))
    glow = field.point(lambda v: round(v * strength))
    warm = ImageChops.multiply(Image.new("RGB", canvas.size, DAYLIGHT), Image.merge("RGB", (glow, glow, glow)))
    shade = field.point(lambda v: round(255 * (FAR_SIDE_SHADE + (1 - FAR_SIDE_SHADE) * v / 255)))
    lit = ImageChops.multiply(ImageChops.screen(canvas, warm), Image.merge("RGB", (shade, shade, shade)))
    canvas.paste(lit, mask=mask)
    return side


def _font(size: int) -> ImageFont.ImageFont:
    for name in ("arialbd.ttf", "Arial Bold.ttf", "DejaVuSans-Bold.ttf", "Helvetica.ttc"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:  # Pillow older than 10.1: the small bitmap font is all there is
        return ImageFont.load_default()


def _dashed(draw: ImageDraw.ImageDraw, points: list[tuple[float, float]], colour, width: int,
            dash: int) -> None:
    for a, b in zip(points, points[1:] + points[:1]):
        length = max(1.0, ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5)
        steps = int(length // dash)
        for i in range(0, steps, 2):
            s, e = i / steps, min(1.0, (i + 1) / steps)
            draw.line([(a[0] + (b[0] - a[0]) * s, a[1] + (b[1] - a[1]) * s),
                       (a[0] + (b[0] - a[0]) * e, a[1] + (b[1] - a[1]) * e)], fill=colour, width=width)


def mark(canvas: Image.Image, corners: Sequence[tuple[float, float]], level: int,
         design_size: tuple[int, int], aspect: float, text: str = LABEL) -> None:
    """A dashed frame round one repeat in the middle of the wall, with a small label."""
    n = repeats(level)
    cell_w = 1.0 / n
    cell_h = min(1.0, cell_w * (design_size[1] / design_size[0]) * aspect)
    col, row = (n - 1) // 2, int((1.0 / cell_h) // 2)
    u0, v0 = col * cell_w, min(row * cell_h, 1.0 - cell_h)
    if cell_w < 0.08 or cell_h < 0.08:
        return  # a repeat too small to frame
    src = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    coeffs = surface.perspective_coeffs(src, list(corners))
    frame = [surface.apply(coeffs, u, v) for u, v in
             ((u0, v0), (u0 + cell_w, v0), (u0 + cell_w, v0 + cell_h), (u0, v0 + cell_h))]
    short = min(canvas.size)
    width = max(2, round(short / 400))
    draw = ImageDraw.Draw(canvas, "RGBA")
    _dashed(draw, frame, (255, 255, 255, 235), width + 2, max(8, short // 70))
    _dashed(draw, frame, (40, 40, 40, 235), width, max(8, short // 70))
    font = _font(max(12, short // 48))
    pad = max(5, short // 150)
    box = draw.textbbox((0, 0), text, font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    x = min(max(frame[3][0] + pad * 1.5, pad), canvas.width - tw - 3 * pad)
    y = min(max(frame[3][1] - th - 3.5 * pad, pad), canvas.height - th - 3 * pad)
    draw.rounded_rectangle((x, y, x + tw + 2 * pad, y + th + 2 * pad), radius=pad, fill=(255, 255, 255, 235))
    draw.text((x + pad - box[0], y + pad - box[1]), text, font=font, fill=(35, 35, 35, 255))


def check(cover: Image.Image, corners: Sequence[tuple[float, float]]) -> dict[str, object]:
    """Whether a cover picture does its job in a search thumbnail.

    `wall_share`: the share of the picture the wall takes; `detail`: how much the pattern
    varies on the wall at the size Etsy shows it (the spread of its brightness, 0-128).
    `ok` is False, with `problems` naming why, when the wall is small or the pattern washes out.
    """
    mask = surface.area_mask(cover.size, corners)
    wall_share = (ImageStat.Stat(mask).mean[0] / 255.0)
    scale = THUMB / cover.width if cover.width > THUMB else 1.0
    small = cover.convert("L").resize((max(1, round(cover.width * scale)), max(1, round(cover.height * scale))),
                                      Image.LANCZOS)
    small_mask = ImageOps.fit(mask, small.size) if small.size != mask.size else mask
    detail = ImageStat.Stat(small, small_mask).stddev[0] if small_mask.getbbox() else 0.0
    problems = []
    if wall_share < MIN_WALL_SHARE:
        problems.append("small_wall")
    if detail < MIN_THUMB_DETAIL:
        problems.append("flat_pattern")
    return {"ok": not problems, "wall_share": round(wall_share, 2), "detail": round(detail, 1),
            "problems": problems}
