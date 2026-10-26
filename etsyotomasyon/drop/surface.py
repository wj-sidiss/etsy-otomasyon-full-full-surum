"""Making a flat design look printed on the product in the photo, with Pillow only.

`mockup.compose` places a design inside a print area. This module is what it calls
when the area asks for more than a flat paste:

* **realism** (0-100): the design takes the mockup's own light, folds and fabric
  texture. A shading map is read from the mockup's brightness inside the print area,
  normalised by the area's median, so a flat area changes nothing; darker folds
  darken the print (multiply) and highlights lift it (screen). Only brightness is
  used, never the garment's colour, so a design keeps its colours on a white or light
  shirt. On fabric the design also moves a little along the folds (displacement).
* **perspective**: a print area can be four corners instead of a rectangle; the
  design is mapped into them with a perspective transform.
* **curve** (0-100): the design wraps round a cylinder (a mug or a tumbler): squeezed
  towards the sides, a little darker there, and bent into a slight arc.

All of it is resolution independent: blur radii and displacements are fractions of
the print area, so the editor's 1400 px preview looks like the 2000+ px render.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from PIL import Image, ImageChops, ImageDraw, ImageFilter

Point = tuple[float, float]

# Transparent pixels round the resized design: the transform interpolates into them,
# which anti-aliases the design's edge instead of cutting it with a hard step.
PAD = 2
# Output pixels per mesh cell. The mesh is bilinear inside a cell, so a cell has to be
# small next to the curve and the folds it follows; 12 px is invisible at 2000 px.
MESH_CELL = 12

# A four-corner area whose smallest corner triangle is less than this share of it is
# refused (corner_share): 0.08 still allows a far side an eighth of the near one.
MIN_CORNER_SHARE = 0.08

# Where the design is squeezed below this share (a steep perspective's far side, a
# cylinder's edges), it is drawn up to SUPERSAMPLE_MAX times larger and reduced.
SUPERSAMPLE_BELOW = 0.6
SUPERSAMPLE_MAX = 4
# The most pixels drawn at once while supersampling (about 100 MB in RGBa): a larger
# canvas is drawn in horizontal bands, so a steep area on a 7000 px mockup needs no
# more memory than one on a 2000 px mockup.
SUPERSAMPLE_PIXELS = 24_000_000

# The widest angle the front of the cylinder covers each side of its middle, at curve 100.
PHI_MAX = math.radians(75)
# The arc a horizontal line on a mug takes when shot from slightly above (edges higher),
# as a fraction of the print area's height at curve 100.
ARC_MAX = 0.06
# How much darker the design gets at the cylinder's edges at curve 100.
EDGE_SHADE_MAX = 0.28

# Brightness changes are measured against the area's median, but never against less than
# this: on a black shirt a fold from 30 to 15 is half the light, yet a print on it does
# not lose half of its own.
DEN_FLOOR = 100
# Shadows may darken the print down to this share of its colour at most.
SHADOW_MIN = 0.15
# Highlights lift the print towards white a little less than shadows darken it.
LIFT_GAIN = 0.9
LIFT_MAX = 0.6
# The design moves along the folds by up to this share of the area's shorter side
# (at realism 100), following the folds at FOLD_BLUR against the light at LIGHT_BLUR.
DISPLACE_MAX = 0.012
DISPLACE_GAIN = 3.0
FOLD_BLUR = 0.008
LIGHT_BLUR = 0.08
# Fabric weave and grain are kept, slightly sharpened, so the print shows the texture.
TEXTURE_PERCENT = 70

UNIT_SQUARE: tuple[Point, Point, Point, Point] = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))


# --- geometry ---------------------------------------------------------------------------------


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    """Solve a small linear system by Gaussian elimination with partial pivoting."""
    size = len(vector)
    rows = [row[:] + [value] for row, value in zip(matrix, vector)]
    for col in range(size):
        pivot = max(range(col, size), key=lambda r: abs(rows[r][col]))
        if abs(rows[pivot][col]) < 1e-12:
            raise ValueError("the four corners do not make a quadrilateral")
        rows[col], rows[pivot] = rows[pivot], rows[col]
        lead = rows[col][col]
        rows[col] = [value / lead for value in rows[col]]
        for r in range(size):
            if r != col and rows[r][col]:
                factor = rows[r][col]
                rows[r] = [a - factor * b for a, b in zip(rows[r], rows[col])]
    return [row[size] for row in rows]


def perspective_coeffs(src: Sequence[Point], dst: Sequence[Point]) -> tuple[float, ...]:
    """The 8 coefficients that take each `src` point to its `dst` point:
    u = (a x + b y + c) / (g x + h y + 1), v = (d x + e y + f) / (g x + h y + 1),
    the form Pillow's PERSPECTIVE transform takes (output -> input)."""
    matrix: list[list[float]] = []
    vector: list[float] = []
    for (x, y), (u, v) in zip(src, dst):
        matrix.append([x, y, 1.0, 0.0, 0.0, 0.0, -u * x, -u * y])
        vector.append(u)
        matrix.append([0.0, 0.0, 0.0, x, y, 1.0, -v * x, -v * y])
        vector.append(v)
    return tuple(_solve(matrix, vector))


def apply(coeffs: Sequence[float], x: float, y: float) -> Point:
    a, b, c, d, e, f, g, h = coeffs
    den = g * x + h * y + 1.0
    return (a * x + b * y + c) / den, (d * x + e * y + f) / den


def _dist(p: Point, q: Point) -> float:
    return math.hypot(p[0] - q[0], p[1] - q[1])


def quad_size(corners: Sequence[Point]) -> tuple[float, float]:
    """(width, height) of a top-left, top-right, bottom-right, bottom-left quad: the mean
    of its opposite sides, which is what a design is fitted into."""
    tl, tr, br, bl = corners
    return (_dist(tl, tr) + _dist(bl, br)) / 2, (_dist(tl, bl) + _dist(tr, br)) / 2


def _triangle(p: Point, q: Point, r: Point) -> float:
    return abs((q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])) / 2


def corner_share(corners: Sequence[Point]) -> float:
    """The smallest triangle a corner makes with its two neighbours, over the quad's area:
    0.5 for a rectangle, 0 for a corner flattened into a side. A ratio of areas, so the
    mockup's aspect ratio does not change it (fractions and pixels agree)."""
    tl, tr, br, bl = corners
    whole = _triangle(tl, tr, br) + _triangle(tl, br, bl)
    if whole <= 0:
        return 0.0
    return min(_triangle(corners[i - 1], corners[i], corners[(i + 1) % 4]) for i in range(4)) / whole


def is_well_shaped(corners: Sequence[Point]) -> bool:
    """Convex, corners in order, and no corner flattened into a side: a shape a design can
    be mapped into without the map coming apart (MIN_CORNER_SHARE)."""
    return is_convex(corners) and corner_share(corners) >= MIN_CORNER_SHARE


def is_convex(corners: Sequence[Point]) -> bool:
    """Whether the corners, in order, turn the same way at every corner (clockwise on
    screen, y down) and enclose some area."""
    turns = []
    for i in range(4):
        (x0, y0), (x1, y1), (x2, y2) = corners[i], corners[(i + 1) % 4], corners[(i + 2) % 4]
        turns.append((x1 - x0) * (y2 - y1) - (y1 - y0) * (x2 - x1))
    return all(turn > 1e-9 for turn in turns)


def _side_ratio(corners: Sequence[Point]) -> float:
    """The shorter of each pair of opposite sides over the longer, the smaller pair: 1 for
    a rectangle, small for a steep perspective."""
    tl, tr, br, bl = corners
    top, bottom, left, right = _dist(tl, tr), _dist(bl, br), _dist(tl, bl), _dist(tr, br)
    return min(min(top, bottom) / max(top, bottom, 1e-9), min(left, right) / max(left, right, 1e-9))


def rect_corners(x: float, y: float, w: float, h: float) -> list[Point]:
    return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]


# --- the curve --------------------------------------------------------------------------------


def _phi(curve: float) -> float:
    return PHI_MAX * max(0.0, min(100.0, curve)) / 100.0


def _unrolled(s: float, phi: float) -> tuple[float, float]:
    """For a point at `s` (0..1) across the visible front of a cylinder whose front spans
    +-phi: (u, the matching position across the unrolled design, 0..1; its angle)."""
    z = (2.0 * s - 1.0) * math.sin(phi)
    if -1.0 <= z <= 1.0:
        theta = math.asin(z)
        return (theta / phi + 1.0) / 2.0, theta
    # Outside the cylinder (only reached in the margin round the area): keep going
    # straight, so the map stays monotonic and the design simply is not there.
    theta = math.copysign(math.pi / 2, z)
    return (theta / phi + 1.0) / 2.0 + (abs(z) - 1.0) * math.copysign(1.0, z), theta


def _arc_lift(theta: float, phi: float) -> float:
    """0 in the middle of the front, 1 at its edges: how far round the cylinder."""
    if phi <= 1e-9:
        return 0.0
    return min(1.0, (1.0 - math.cos(theta)) / (1.0 - math.cos(phi)))


def _edge_shade(design: Image.Image, u0: float, uspan: float, phi: float, strength: float) -> Image.Image:
    """Darken the design's columns towards the cylinder's edges (the unrolled design)."""
    width, height = design.size
    if strength <= 0 or phi <= 1e-9 or width < 1:
        return design
    row = []
    for i in range(width):
        u = u0 + (i + 0.5) / width * uspan
        lift = _arc_lift((2.0 * u - 1.0) * phi, phi)
        row.append(max(0, min(255, round(255 * (1.0 - strength * lift ** 1.5)))))
    gradient = Image.new("L", (width, 1))
    gradient.putdata(row)
    gradient = gradient.resize((width, height), Image.NEAREST)
    bands = list(design.split())
    for index in range(3):
        bands[index] = ImageChops.multiply(bands[index], gradient)
    return Image.merge(design.mode, bands)


# --- light and folds --------------------------------------------------------------------------


def _median(histogram: list[int]) -> int | None:
    total = sum(histogram)
    if not total:
        return None
    seen = 0
    for value, count in enumerate(histogram):
        seen += count
        if seen * 2 >= total:
            return value
    return 255


def area_mask(size: tuple[int, int], corners: Sequence[Point]) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).polygon([(float(x), float(y)) for x, y in corners], fill=255)
    return mask


def reference(base: Image.Image, corners: Sequence[Point]) -> int | None:
    """The print area's typical brightness: the median of its luminance, the level a
    flat area sits at and the shading map's 1.0."""
    luminance = base.convert("L")
    return _median(luminance.histogram(area_mask(base.size, corners)))


def _radius(fraction: float, short_side: float) -> float:
    return max(0.6, fraction * short_side)


def shade(layer: Image.Image, base: Image.Image, ref: int, realism: float, short_side: float) -> Image.Image:
    """`layer` (RGBA, the design placed over `base`, same size) darkened by the mockup's
    folds and shadows and lifted by its highlights, by `realism` (0-100)."""
    k = max(0.0, min(100.0, realism)) / 100.0
    if k <= 0:
        return layer
    luminance = base.convert("L")
    if TEXTURE_PERCENT:
        # The weave a little crisper, so the print shows it; radius in area terms.
        luminance = luminance.filter(ImageFilter.UnsharpMask(
            radius=_radius(0.002, short_side), percent=round(TEXTURE_PERCENT * k), threshold=1))
    den = float(max(ref, DEN_FLOOR))
    shadow_lut = []
    lift_lut = []
    for value in range(256):
        dev = (value - ref) / den
        shadow_lut.append(round(255 * max(SHADOW_MIN, min(1.0, 1.0 + k * dev))))
        lift_lut.append(round(255 * max(0.0, min(LIFT_MAX, LIFT_GAIN * k * dev))))
    shadow = luminance.point(shadow_lut)
    lift = luminance.point(lift_lut)
    red, green, blue, alpha = layer.split()
    bands = [ImageChops.screen(ImageChops.multiply(band, shadow), lift) for band in (red, green, blue)]
    return Image.merge("RGBA", (*bands, alpha))


def _fold_map(base: Image.Image, ref: int, short_side: float) -> Image.Image:
    """An "L" map centred on 128: how much brighter (above) or darker (below) each spot
    is than the light around it — the folds, without the photo's overall gradient."""
    luminance = base.convert("L")
    folds = luminance.filter(ImageFilter.GaussianBlur(_radius(FOLD_BLUR, short_side)))
    # The broad light is blurred on a small copy: the same result, a fraction of the work.
    small_scale = min(1.0, 64.0 / max(1.0, max(luminance.size)))
    small = luminance.resize((max(1, round(luminance.width * small_scale)),
                              max(1, round(luminance.height * small_scale))), Image.BOX)
    light = small.filter(ImageFilter.GaussianBlur(max(0.6, LIGHT_BLUR * short_side * small_scale)))
    light = light.resize(luminance.size, Image.BILINEAR)
    # 128 + (folds - light) * gain, clipped: a fold of 1/DISPLACE_GAIN of the light (in
    # DEN_FLOOR terms) is the full displacement.
    gain = 128.0 / float(max(ref, DEN_FLOOR)) * DISPLACE_GAIN
    return ImageChops.subtract(folds, light, 1.0 / gain, 128)


# --- placing the design -----------------------------------------------------------------------


def render(
    canvas: Image.Image,
    design: Image.Image,
    corners: Sequence[Point],
    *,
    realism: float = 0,
    curve: float = 0,
    displace: bool = True,
) -> None:
    """Draw `design` (RGBA) onto `canvas` (RGB, changed in place) inside `corners`.

    `corners` are top-left, top-right, bottom-right, bottom-left in canvas pixels
    (continuous coordinates: a rectangle's right edge is x + width). The design keeps
    its aspect ratio and is centred, as a flat paste is. `realism` and `curve` are the
    print area's 0-100 settings; `displace` is False on rigid products.
    """
    corners = [(float(x), float(y)) for x, y in corners]
    width, height = canvas.size
    qw, qh = quad_size(corners)
    if qw < 1 or qh < 1 or design.width < 1 or design.height < 1:
        return
    phi = _phi(curve)
    unroll = phi / math.sin(phi) if phi > 1e-6 else 1.0
    # The arc moves the design's middle down and its edges up by half the arc each, so
    # that much of the height is kept free: the bent design stays inside the area.
    arc_frac = ARC_MAX * max(0.0, min(100.0, curve)) / 100.0 if phi > 1e-6 else 0.0
    scale = min(qw * unroll / design.width, qh * (1.0 - arc_frac) / design.height)
    uspan = min(1.0, design.width * scale / (qw * unroll))
    vspan = min(1.0, design.height * scale / qh)
    u0, v0 = (1.0 - uspan) / 2.0, (1.0 - vspan) / 2.0
    short_side = min(qw, qh)
    k = max(0.0, min(100.0, realism)) / 100.0
    amp = DISPLACE_MAX * k * short_side if displace else 0.0
    arc = arc_frac * qh

    margin = math.ceil(amp + arc / 2 + 2)
    xs = [x for x, _ in corners]
    ys = [y for _, y in corners]
    x0 = max(0, math.floor(min(xs)) - margin)
    y0 = max(0, math.floor(min(ys)) - margin)
    x1 = min(width, math.ceil(max(xs)) + margin)
    y1 = min(height, math.ceil(max(ys)) + margin)
    if x1 <= x0 or y1 <= y0:
        return
    box_w, box_h = x1 - x0, y1 - y0
    rel = [(x - x0, y - y0) for x, y in corners]
    to_unit = perspective_coeffs(rel, UNIT_SQUARE)

    # Resized once, with a proper filter, to the size it is shown at — or a little more
    # where perspective makes one side longer — so the transform below only nudges it.
    tl, tr, br, bl = rel
    stretch = max(_dist(tl, tr), _dist(bl, br)) / max(qw, 1e-6)
    stretch = max(stretch, max(_dist(tl, bl), _dist(tr, br)) / max(qh, 1e-6))
    stretch = min(2.0, max(1.0, stretch))
    if phi > 1e-6:
        stretch = min(2.0, stretch * unroll)  # the middle of a cylinder is magnified
    src_w = max(1, round(design.width * scale * stretch))
    src_h = max(1, round(design.height * scale * stretch))
    # reducing_gap: a large print file is box-reduced most of the way first, then LANCZOS
    # does the rest — the same look, a fraction of the time on a 4500 px design.
    resized = design.convert("RGBA").resize((src_w, src_h), Image.LANCZOS,
                                            reducing_gap=3.0).convert("RGBa")
    if phi > 1e-6:
        resized = _edge_shade(resized, u0, uspan, phi, EDGE_SHADE_MAX * curve / 100.0)
    source = Image.new("RGBa", (src_w + 2 * PAD, src_h + 2 * PAD), (0, 0, 0, 0))
    source.paste(resized, (PAD, PAD))

    kx, ky = src_w / uspan, src_h / vspan
    bx, by = PAD - u0 * kx, PAD - v0 * ky
    base = canvas.crop((x0, y0, x1, y1))
    ref = reference(base, rel) if k > 0 else None

    # Pillow's transform filters do not average, so where the design is squeezed (the far
    # side of a steep perspective, the edges of a cylinder) it would sparkle. It is then
    # drawn `factor` times larger and box-reduced, which averages what was squeezed.
    squeeze = min(_side_ratio(rel), math.cos(phi))
    factor = 1 if squeeze >= SUPERSAMPLE_BELOW else min(SUPERSAMPLE_MAX, math.ceil(SUPERSAMPLE_BELOW / max(squeeze, 1e-6)))
    # Drawn larger and averaged down, bilinear is as smooth as bicubic and much quicker.
    resample = Image.BICUBIC if factor == 1 else Image.BILINEAR
    # Rows of the box drawn per pass, a whole number of mesh cells so the bands join
    # exactly as one pass would (SUPERSAMPLE_PIXELS).
    rows = SUPERSAMPLE_PIXELS // (box_w * factor * factor) // MESH_CELL * MESH_CELL
    rows = box_h if rows >= box_h else max(MESH_CELL, rows)
    if phi <= 1e-6 and amp <= 0:
        a, b, c, d, e, f, g, h = to_unit
        coeffs = (kx * a + bx * g, kx * b + bx * h, kx * c + bx,
                  ky * d + by * g, ky * e + by * h, ky * f + by, g, h)
        coeffs = tuple(value / factor if index not in (2, 5) else value
                       for index, value in enumerate(coeffs))

        def band(top: int, size: tuple[int, int]) -> Image.Image:
            return source.transform(size, Image.PERSPECTIVE, _shifted(coeffs, top), resample,
                                    fillcolor=(0, 0, 0, 0))
    else:
        offsets = _fold_map(base, ref if ref is not None else 128, short_side).load() if amp > 0 else None

        def source_point(px: float, py: float) -> Point:
            px, py = px / factor, py / factor
            if offsets is not None:
                sample = offsets[min(box_w - 1, int(px)), min(box_h - 1, int(py))]
                shift = amp * (sample - 128) / 127.0
                px, py = px - shift, py - shift
            s, t = apply(to_unit, px, py)
            if phi > 1e-6:
                u, theta = _unrolled(s, phi)
                v = t + arc / qh * (_arc_lift(theta, phi) - 0.5) if arc else t
            else:
                u, v = s, t
            return bx + u * kx, by + v * ky

        def band(top: int, size: tuple[int, int]) -> Image.Image:
            return _mesh(source, size, lambda px, py: source_point(px, py + top),
                         cell=MESH_CELL * factor, resample=resample)

    if rows >= box_h:
        layer = band(0, (box_w * factor, box_h * factor))
        if factor > 1:
            layer = layer.reduce(factor)
    else:
        layer = Image.new("RGBa", (box_w, box_h), (0, 0, 0, 0))
        for top in range(0, box_h, rows):
            height = min(rows, box_h - top)
            strip = band(top * factor, (box_w * factor, height * factor))
            layer.paste(strip.reduce(factor) if factor > 1 else strip, (0, top))
    layer = layer.convert("RGBA")
    if ref is not None:
        layer = shade(layer, base, ref, realism, short_side)
    canvas.paste(layer, (x0, y0), layer)


def _shifted(coeffs: tuple[float, ...], top: float) -> tuple[float, ...]:
    """PERSPECTIVE coefficients for output drawn from row `top` down: the same map with
    y + top in place of y, scaled back to a denominator constant of 1 (a projective map
    is unchanged by a common factor)."""
    if not top:
        return coeffs
    a, b, c, d, e, f, g, h = coeffs
    den = h * top + 1.0
    if abs(den) < 1e-12:  # the band starts on the horizon, which is never in the area
        den = math.copysign(1e-12, den)
    return (a / den, b / den, (b * top + c) / den, d / den, e / den, (e * top + f) / den,
            g / den, h / den)


def _mesh(source: Image.Image, size: tuple[int, int], source_point, *, cell: int = MESH_CELL,
          resample: int = Image.BICUBIC) -> Image.Image:
    """`source` warped onto an image of `size` by Pillow's MESH transform, the map given
    by `source_point(x, y)` at the corners of `cell`-sized cells."""
    box_w, box_h = size
    xs = list(range(0, box_w, cell)) + [box_w]
    ys = list(range(0, box_h, cell)) + [box_h]
    grid = [[source_point(x, y) for y in ys] for x in xs]
    limit_x, limit_y = source.width, source.height
    data = []
    for i in range(len(xs) - 1):
        left, right = grid[i], grid[i + 1]
        for j in range(len(ys) - 1):
            nw, sw, se, ne = left[j], left[j + 1], right[j + 1], right[j]
            pts_x = (nw[0], sw[0], se[0], ne[0])
            pts_y = (nw[1], sw[1], se[1], ne[1])
            # A cell whose corners all fall on one side of the design stays transparent.
            if max(pts_x) < 0 or min(pts_x) > limit_x or max(pts_y) < 0 or min(pts_y) > limit_y:
                continue
            data.append(((xs[i], ys[j], xs[i + 1], ys[j + 1]), (*nw, *sw, *se, *ne)))
    if not data:
        return Image.new(source.mode, size, (0, 0, 0, 0))
    return source.transform(size, Image.MESH, data, resample, fillcolor=(0, 0, 0, 0))
