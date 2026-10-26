"""Print-ready files for an order, made from the listings' print files (drop.printfiles).

An order of a wallpaper says what to print in its variation: "1 Roll 23×160 in",
"Mural 120×140 in", "SAMPLE 15×20 in" (any "W×H" or "WxH", in inches unless it says cm
or ft). From the listing's print file:

- a roll, a sample or any other size is a *pattern*: the design is a tile repeated
  across and down. It is scaled so that a whole number of repeats spans the roll's width
  (`repeats` across), so every roll of the order is the same file and the pattern meets
  itself where two rolls hang side by side. A sample uses the repeat of a roll, so it
  shows the pattern at its real size.
- a mural is one picture: the design covers the whole wall, centred, cut to its shape.

Files are JPEG (quality 95, no chroma subsampling) with their dpi written in; a mural too
large to hold in memory at that dpi is made at a lower one, and says so.
"""

from __future__ import annotations

import math
import re
import unicodedata
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageFilter, ImageOps, ImageStat

from . import printfiles
from .workspace import IMAGE_SUFFIXES, PRINT_DIR

DEFAULT_DPI = 150
MIN_DPI, MAX_DPI = 50, 300
MAX_BLEED_IN = 1.0
# The width of one roll, in inches; also the repeat a sample is shown at.
ROLL_WIDTH_IN = 23.0
# Above this many pixels a file is made at a lower dpi (a 120×140 in mural at 150 dpi is
# 378 million pixels, more than a desktop holds comfortably).
MAX_PIXELS = 120_000_000
# A repeating roll holds more: a 23×160 in roll at 300 dpi is 331 million pixels (about
# 1 GB in memory), the file a printer takes for it.
MAX_PATTERN_PIXELS = 360_000_000
# A design enlarged more than this to fill its file prints soft.
UPSCALE_WARN = 1.5
JPEG_LIMIT = 65_000  # JPEG's own limit is 65535 px a side
# A tile whose own edges differ from its inside this many times over shows a line where
# it repeats (about 1 for a seamless tile).
SEAM_WARN = 1.8
ORDERS_DIR = "orders"
_SHARPEN = ImageFilter.UnsharpMask(radius=1.6, percent=70, threshold=2)

KINDS = ("roll", "mural", "sample", "panel")
_SIZE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(?:x|×|\*|by)\s*(\d+(?:[.,]\d+)?)\s*(cm|ft|feet|in(?:ch(?:es)?)?|\"|″)?",
    re.IGNORECASE,
)
_UNIT = {"cm": 1 / 2.54, "ft": 12.0, "feet": 12.0}


@dataclass
class Size:
    kind: str  # roll | mural | sample | panel
    width_in: float
    height_in: float
    label: str  # the variation's own words

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "width_in": round(self.width_in, 2),
                "height_in": round(self.height_in, 2), "label": self.label}


def parse_size(values: list[str]) -> Size | None:
    """The size an order item is printed at, from its variation values; None if none says."""
    for value in values:
        text = str(value or "")
        found = _SIZE.search(text)
        if not found:
            continue
        unit = (found.group(3) or "").lower()
        factor = _UNIT.get(unit, 1.0)
        width = float(found.group(1).replace(",", ".")) * factor
        height = float(found.group(2).replace(",", ".")) * factor
        if width <= 0 or height <= 0:
            continue
        folded = text.casefold()
        if "mural" in folded or "duvar resmi" in folded:
            kind = "mural"
        elif "sample" in folded or "numune" in folded or "örnek" in folded:
            kind = "sample"
        elif "roll" in folded or "rulo" in folded:
            kind = "roll"
        else:
            kind = "panel"
        return Size(kind, width, height, text.strip())
    return None


def material_of(values: list[str], size: Size | None) -> str:
    """The other variation (Traditional, Peel and Stick, Vinyl), for names and notes."""
    for value in values:
        if size is None or str(value).strip() != size.label:
            return str(value).strip()
    return ""


# --- the tile --------------------------------------------------------------------------


def _diff(a: Image.Image, b: Image.Image) -> float:
    return ImageStat.Stat(ImageChops.difference(a, b)).mean[0]


def seam_ratios(tile: Image.Image) -> tuple[float, float]:
    """(left-right, top-bottom): how much the tile's opposite edges differ, over how much
    neighbouring columns (rows) inside it differ. About 1 when it repeats without a line;
    the larger, the more visible the seam."""
    grey = tile.convert("L")
    if max(grey.size) > 1600:
        grey.thumbnail((1600, 1600))
    w, h = grey.size
    if w < 4 or h < 4:
        return 1.0, 1.0

    def col(x: int) -> Image.Image:
        return grey.crop((x, 0, x + 1, h))

    def row(y: int) -> Image.Image:
        return grey.crop((0, y, w, y + 1))

    spots = [round(i * (w - 2) / 8) for i in range(1, 8)]
    inside_x = sum(_diff(col(x), col(x + 1)) for x in spots) / len(spots)
    spots = [round(i * (h - 2) / 8) for i in range(1, 8)]
    inside_y = sum(_diff(row(y), row(y + 1)) for y in spots) / len(spots)
    across = _diff(col(w - 1), col(0)) / max(inside_x, 0.5)
    down = _diff(row(h - 1), row(0)) / max(inside_y, 0.5)
    return round(across, 2), round(down, 2)


def _profile(length: int, band: int, other: int, horizontal: bool) -> Image.Image:
    """A mask of length x other that is 0 at both ends along its length and 1 from `band`
    pixels in (a smooth ramp between)."""
    values = []
    for i in range(length):
        edge = min(i, length - 1 - i)
        t = min(1.0, edge / band)
        values.append(round(255 * t * t * (3 - 2 * t)))
    line = Image.new("L", (length, 1))
    line.putdata(values)
    return line.resize((length, other), Image.NEAREST) if horizontal else \
        line.transpose(Image.TRANSPOSE).resize((other, length), Image.NEAREST)


def make_seamless(tile: Image.Image, band: float = 0.08) -> Image.Image:
    """The tile with its edges made to meet.

    The tile is shifted half its size, wrapping round: its edges are then the neighbours
    of its middle, so they continue each other. The original is faded into that shifted
    copy over `band` of its width and height at the edges, and stays untouched in the
    middle. The pattern's own motifs are kept; only a thin strip along each edge is a
    cross-fade.
    """
    width, height = tile.size
    shifted = ImageChops.offset(tile, width // 2, height // 2)
    bx, by = max(2, int(width * band)), max(2, int(height * band))
    across = _profile(width, bx, height, True)
    down = _profile(height, by, width, False)
    return Image.composite(tile, shifted, ImageChops.multiply(across, down))


def open_design(path: Path) -> Image.Image:
    """The print file as RGB (transparency on white), upright."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", Image.DecompressionBombWarning)
        with Image.open(path) as opened:
            image = ImageOps.exif_transpose(opened)
            image.load()
    if image.mode in ("RGBA", "LA", "PA") or (image.mode == "P" and "transparency" in image.info):
        background = Image.new("RGB", image.size, "white")
        background.paste(image.convert("RGBA"), mask=image.convert("RGBA").getchannel("A"))
        return background
    return image.convert("RGB")


def design_file(root: Path, listing_id: int) -> Path | None:
    """The listing's print file to print from: its first image, by name."""
    for path in printfiles.files(root, listing_id):
        if path.suffix.lower() in IMAGE_SUFFIXES:
            return path
    return None


# --- what an item becomes ----------------------------------------------------------------


@dataclass
class Plan:
    """One file to make: the item, its size, how the design fills it."""

    size: Size
    mode: str  # pattern | cover
    dpi: int
    width_px: int
    height_px: int
    repeats: int = 0  # pattern: how many times the tile spans a roll's width
    span_px: int = 0  # pattern: that width (a roll's), which `repeats` tiles fill exactly
    lowered: bool = False  # a file made at a lower dpi to fit in memory
    bleed_px: int = 0  # extra design added on every side, for the printer to trim
    upscale: float = 1.0  # how much the design is enlarged to fill the file
    notes: list[str] = field(default_factory=list)

    @property
    def file_width_px(self) -> int:
        return self.width_px + 2 * self.bleed_px

    @property
    def file_height_px(self) -> int:
        return self.height_px + 2 * self.bleed_px

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "dpi": self.dpi, "width_px": self.width_px,
                "height_px": self.height_px, "bleed_px": self.bleed_px,
                "bleed_in": round(self.bleed_px / self.dpi, 3),
                "file_width_px": self.file_width_px, "file_height_px": self.file_height_px,
                "repeats": self.repeats,
                "repeat_px": round(self.span_px / self.repeats) if self.repeats else 0,
                "lowered": self.lowered, "upscale": round(self.upscale, 2),
                "soft": self.upscale > UPSCALE_WARN,
                "width_cm": round(self.width_px / self.dpi * 2.54, 1),
                "height_cm": round(self.height_px / self.dpi * 2.54, 1)}


def repeats_for(tile_width: int, roll_px: int, wanted: int | None = None) -> int:
    """How many tiles across a roll: `wanted`, else as near the tile's own size as fits."""
    if wanted:
        return max(1, min(int(wanted), 40))
    return max(1, round(roll_px / max(1, tile_width)))


def plan(size: Size, tile_size: tuple[int, int], *, dpi: int = DEFAULT_DPI,
         repeats: int | None = None, mode: str | None = None,
         roll_width_in: float = ROLL_WIDTH_IN, bleed_in: float = 0.0) -> Plan:
    """What the file of an item of `size` is, from a design of `tile_size` pixels.

    `bleed_in`: inches of design added on every side beyond the trimmed size, so a printer
    that cuts a little off an edge does not leave a white line (the pattern simply
    continues; a picture is enlarged to cover it).
    """
    dpi = max(MIN_DPI, min(MAX_DPI, int(dpi)))
    bleed_in = max(0.0, min(MAX_BLEED_IN, float(bleed_in or 0)))
    if mode is None:
        # A mural of a near-square design is that design repeated (the listing sells the
        # same tile as rolls and as murals); a wide or tall picture covers the wall.
        ratio = tile_size[0] / max(1, tile_size[1])
        mode = "cover" if size.kind == "mural" and not 0.8 <= ratio <= 1.25 else "pattern"
    notes: list[str] = []
    lowered = False
    limit = MAX_PATTERN_PIXELS if mode == "pattern" else MAX_PIXELS
    full_w, full_h = size.width_in + 2 * bleed_in, size.height_in + 2 * bleed_in
    if full_w * full_h * dpi * dpi > limit:
        dpi = max(MIN_DPI, int(math.sqrt(limit / (full_w * full_h))))
        lowered = True
    bleed_px = round(bleed_in * dpi)
    width_px = min(JPEG_LIMIT - 2 * bleed_px, round(size.width_in * dpi))
    height_px = min(JPEG_LIMIT - 2 * bleed_px, round(size.height_in * dpi))
    result = Plan(size, mode, dpi, width_px, height_px, lowered=lowered, notes=notes,
                  bleed_px=bleed_px)
    if mode == "pattern":
        # A roll's repeat spans its own width; anything else is shown at a roll's repeat.
        roll_px = width_px if size.kind == "roll" else round(roll_width_in * dpi)
        # The repeat count is the same at every dpi (chosen at the default one), so the
        # pattern looks the same size on the wall at 150 and at 300 dpi.
        count = repeats_for(tile_size[0], round(roll_width_in * DEFAULT_DPI), repeats)
        result.repeats = count
        result.span_px = roll_px
        result.upscale = (roll_px / count) / max(1, tile_size[0])
    else:
        result.upscale = max(width_px / max(1, tile_size[0]), height_px / max(1, tile_size[1]))
    return result


def render(design: Image.Image, item: Plan) -> Image.Image:
    """The file of one item. A design enlarged to fill it is sharpened a little: Lanczos
    keeps edges clean but leaves them soft, and a printer shows that."""
    soft = item.upscale > 1.2
    if item.mode == "cover":
        fitted = ImageOps.fit(design, (item.file_width_px, item.file_height_px), Image.LANCZOS,
                              centering=(0.5, 0.5))
        return fitted.filter(_SHARPEN) if soft else fitted
    # One roll's width of `repeats` tiles, made exactly that wide: the next roll starts
    # where the pattern starts again, so the two meet without a step.
    count, span = item.repeats, item.span_px
    tile_w = math.ceil(span / count)
    tile_h = max(1, round(design.height * span / count / design.width))
    tile = design.resize((tile_w, tile_h), Image.LANCZOS)
    if soft:
        tile = tile.filter(_SHARPEN)
    strip = Image.new("RGB", (tile_w * count, tile_h), "white")
    for i in range(count):
        strip.paste(tile, (i * tile_w, 0))
    if strip.width != span:
        strip = strip.resize((span, tile_h), Image.LANCZOS)
    # The pattern starts at the trimmed edge: the bleed is the same pattern running on past it.
    bleed = item.bleed_px
    canvas = Image.new("RGB", (item.file_width_px, item.file_height_px), "white")
    x0 = bleed - span * math.ceil(bleed / span)
    y0 = bleed - tile_h * math.ceil(bleed / tile_h)
    for y in range(y0, item.file_height_px, tile_h):
        for x in range(x0, item.file_width_px, span):
            canvas.paste(strip, (x, y))
    return canvas


def _slug(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.replace("ı", "i")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")[:40]


def file_name(index: int, item: Plan, material: str, quantity: int) -> str:
    """"1-roll-23x160in-traditional-x13.jpg": what, how big, which paper, how many."""
    dims = f"{item.size.width_in:g}x{item.size.height_in:g}in"
    parts = [str(index), item.size.kind, dims]
    if material:
        parts.append(_slug(material))
    if quantity > 1:
        parts.append(f"x{quantity}")
    if item.bleed_px:
        parts.append(f"bleed{item.bleed_px / item.dpi:g}in")
    return "-".join(p for p in parts if p) + ".jpg"


def order_folder(root: Path, receipt_id: int) -> Path:
    return root / PRINT_DIR / ORDERS_DIR / str(int(receipt_id))


def save(image: Image.Image, path: Path, dpi: int) -> Path:
    """Write a JPEG for the printer, atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{printfiles.PART_PREFIX}{path.name}")
    try:
        image.save(temporary, format="JPEG", quality=95, subsampling=0, dpi=(dpi, dpi),
                   optimize=False)
        temporary.replace(path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return path
