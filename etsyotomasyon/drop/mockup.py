"""Compositing a design onto a mockup template with Pillow.

Print areas are stored as **fractions** of the mockup, not pixels. That one choice
buys three things: a sensible default works before anyone calibrates anything, one
calibrated rectangle covers every sibling mockup of the same dimensions, and the
compositor can be tested in CI on generated images with no assets in the repo.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageChops, ImageDraw, ImageOps

try:  # Colour management needs Pillow built with littlecms; the wheels always are.
    from PIL import ImageCms
except ImportError:  # pragma: no cover - a source build without lcms2
    ImageCms = None  # type: ignore[assignment]

from ..client import UPLOADABLE_SUFFIXES
from ..errors import ValidationError
from . import surface

# A chest print on a folded garment shot, as fractions of the mockup. Deliberately
# conservative: too small reads as a design choice, too large reads as a bug.
DEFAULT_AREA = (0.30, 0.26, 0.40, 0.36)

# Etsy recommends the shortest side be at least 2000px so the zoom viewer works.
OUTPUT_MIN_EDGE = 2000
# The flat render of a DIGITAL product's design: it is the file being sold, and Etsy
# serves a listing photo at up to 3000 px a side (url_fullxfull), so the public photo
# of it is a preview, small enough that it cannot stand in for the download.
DIGITAL_PREVIEW_EDGE = 1200
JPEG_QUALITY = 92

# Full-resolution colour (4:4:4). libjpeg's default halves chroma in both directions,
# which bleeds saturated line art and small lettering — the detail a print is sold on.
JPEG_OPTIONS = {"quality": JPEG_QUALITY, "optimize": True, "subsampling": 0}

# A calibration preview is looked at once and thrown away, so it is deliberately small.
# Nobody wants to wait for a 4500px JPEG to open just to see the rectangle sits too low.
PREVIEW_MAX_EDGE = 1400

# EXIF tag 274. A phone does not rotate what its sensor captured; it records which way
# up the photo goes and leaves the turning to whoever displays it. Explorer, Preview and
# Etsy all obey that tag, so a mockup the seller sees as 1200x1500 portrait can be stored
# as 1500x1200 landscape. Pillow hands over the stored pixels, so anything that draws on
# them has to apply the tag first — otherwise the render comes out a quarter turn wrong
# and the print area, a fraction of the shape the seller measured on screen, lands
# somewhere else on the garment.
ORIENTATION_TAG = 274

# Orientations 5-8 are the quarter-turn ones, where stored width is displayed height.
QUARTER_TURNED = frozenset({5, 6, 7, 8})

# Every listing image lands as a JPEG on Etsy's white product page, so white is the
# ground a transparent template has to be flattened against — any other colour reads
# as a rectangle drawn around the product. flatten_design() already made this call.
WHITE = (255, 255, 255)


# How much of the mockup's own light, folds and texture a design takes (0-100), by mockup
# type when the print area does not say: fabric takes most, paper and rigid prints little.
# Without a type (a direct compose() call) it is 0, the flat paste of old.
REALISM_DEFAULTS = {
    "tshirt": 65, "sweatshirt": 65, "hoodie": 65, "tote": 60, "pillow": 50, "mug": 45,
    "canvas": 30, "phone_case": 25, "sticker": 15, "poster": 15, "other": 35,
}
# How far the design wraps round a cylinder (0-100), by type: mugs only.
CURVE_DEFAULTS = {"mug": 55}
# The types the editor offers the curve for: a mug, or a tumbler or bottle filed as "other".
CURVE_TYPES = frozenset({"mug", "other"})
# Rigid surfaces have no folds, so the design is never displaced along them.
RIGID_TYPES = frozenset({"mug", "poster", "canvas", "phone_case", "sticker"})


@dataclass(frozen=True)
class PrintArea:
    """Where the design goes, as fractions of the mockup's width and height.

    `quad`, when set, is four corners (top-left, top-right, bottom-right, bottom-left)
    the design is mapped into with perspective; x/y/w/h are then their bounding box,
    so code that only knows rectangles still sees where the print is. `realism` and
    `curve` (0-100) are the area's own settings; None means "the mockup type's default"
    (REALISM_DEFAULTS, CURVE_DEFAULTS), which `styled()` fills in.
    """

    x: float
    y: float
    w: float
    h: float
    quad: tuple[tuple[float, float], ...] | None = None
    realism: int | None = None
    curve: int | None = None

    def __post_init__(self) -> None:
        if self.quad is not None:
            corners = _corners(self.quad)
            object.__setattr__(self, "quad", corners)
            xs = [x for x, _ in corners]
            ys = [y for _, y in corners]
            # The box rounded like the editor rounds fractions, so JSON stays readable.
            object.__setattr__(self, "x", round(min(xs), 6))
            object.__setattr__(self, "y", round(min(ys), 6))
            object.__setattr__(self, "w", round(max(xs) - min(xs), 6))
            object.__setattr__(self, "h", round(max(ys) - min(ys), 6))
        for name, value in (("x", self.x), ("y", self.y), ("w", self.w), ("h", self.h)):
            if not 0.0 <= value <= 1.0:
                raise ValidationError(f"print area {name}={value} must be between 0 and 1")
        if self.x + self.w > 1.0001 or self.y + self.h > 1.0001:
            raise ValidationError("print area extends past the edge of the mockup")
        if self.w <= 0 or self.h <= 0:
            raise ValidationError("print area has no size")
        for name in ("realism", "curve"):
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
                raise ValidationError(f"print area {name} must be a number from 0 to 100")
            if not 0 <= value <= 100:
                raise ValidationError(f"print area {name}={value} must be between 0 and 100")
            object.__setattr__(self, name, int(round(value)))

    @classmethod
    def from_quad(cls, corners: Any, *, realism: int | None = None,
                  curve: int | None = None) -> PrintArea:
        return cls(0.0, 0.0, 0.0, 0.0, quad=corners, realism=realism, curve=curve)

    def pixels(self, width: int, height: int) -> tuple[int, int, int, int]:
        return (
            round(self.x * width),
            round(self.y * height),
            max(1, round(self.w * width)),
            max(1, round(self.h * height)),
        )

    def corners(self, width: int, height: int) -> list[tuple[float, float]]:
        """The four corners in pixels of a `width` x `height` image (TL, TR, BR, BL).

        A rectangle's corners are its `pixels()` box, so it lands where it always did.
        """
        if self.quad is None:
            x, y, w, h = self.pixels(width, height)
            return surface.rect_corners(x, y, w, h)
        return [(qx * width, qy * height) for qx, qy in self.quad]

    def geometry(self) -> PrintArea:
        """The same area without its own realism and curve (the type defaults apply)."""
        return PrintArea(self.x, self.y, self.w, self.h, quad=self.quad)

    def to_dict(self) -> dict[str, Any]:
        """x/y/w/h, plus quad/realism/curve only when set: an area saved before they
        existed is written back exactly as it was."""
        out: dict[str, Any] = {"x": self.x, "y": self.y, "w": self.w, "h": self.h}
        if self.quad is not None:
            out["quad"] = [[x, y] for x, y in self.quad]
        if self.realism is not None:
            out["realism"] = self.realism
        if self.curve is not None:
            out["curve"] = self.curve
        return out


def _corners(raw: Any) -> tuple[tuple[float, float], ...]:
    """Four (x, y) fractions in order TL, TR, BR, BL that make a convex shape."""
    try:
        if any(len(point) != 2 for point in raw):
            raise ValueError("a corner is not [x, y]")
        points = [(float(point[0]), float(point[1])) for point in raw]
    except (TypeError, ValueError, IndexError, KeyError) as exc:
        raise ValidationError("print area quad must be four [x, y] corners") from exc
    if len(points) != 4:
        raise ValidationError("print area quad must be four [x, y] corners")
    clean = []
    for x, y in points:
        if x != x or y != y or not (-0.0001 <= x <= 1.0001 and -0.0001 <= y <= 1.0001):
            raise ValidationError("print area corners must be between 0 and 1")
        clean.append((min(1.0, max(0.0, x)), min(1.0, max(0.0, y))))
    if not surface.is_well_shaped(clean):
        raise ValidationError(
            "print area corners must go top-left, top-right, bottom-right, bottom-left "
            "and make a shape without a dent, a twist or a corner flattened into a side"
        )
    return tuple(clean)


def styled(area: PrintArea, kind: str | None) -> PrintArea:
    """`area` with its realism and curve filled in: its own, else the `kind` (mockup
    type) default, else 0 when there is no type."""
    realism = area.realism
    if realism is None:
        realism = REALISM_DEFAULTS.get(kind, 0) if kind else 0
    curve = area.curve
    if curve is None:
        curve = CURVE_DEFAULTS.get(kind, 0) if kind else 0
    return replace(area, realism=realism, curve=curve)


DEFAULT_PRINT_AREA = PrintArea(*DEFAULT_AREA)


def parse_area(text: str) -> PrintArea:
    """Read an `x,y,w,h` string typed on the command line.

    Fractions are what get stored, so fractions are what the flag takes. Whole numbers
    are the predictable mistake — they are what a ruler in an image editor shows — and
    they get one message that says so, rather than four separate range errors.
    """
    parts = [part.strip() for part in str(text).split(",")]
    if len(parts) != 4:
        raise ValidationError(
            f"--area needs four numbers, x,y,w,h — got {text!r}. "
            f"Example: --area {','.join(format(value, 'g') for value in DEFAULT_AREA)}"
        )
    try:
        values = [float(part) for part in parts]
    except ValueError as exc:
        raise ValidationError(f"--area has a value that is not a number: {text!r}") from exc
    if any(value > 1.0 for value in values):
        raise ValidationError(
            f"--area takes fractions of the mockup between 0 and 1, not pixels: {text!r}. "
            "Divide each measurement by the mockup's width or height — or, if you already "
            "have a file of pixel rectangles, bring it over with --import."
        )
    return PrintArea(*values)


def load_positions(path: Path) -> dict[str, PrintArea]:
    """Read positions.json. A missing file is normal — defaults cover it."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{path} is not valid JSON: {exc}") from exc
    out: dict[str, PrintArea] = {}
    for name, value in (raw or {}).items():
        if not isinstance(value, dict):
            continue
        try:
            box = (float(value["x"]), float(value["y"]), float(value["w"]), float(value["h"]))
            realism, curve = _level(value.get("realism")), _level(value.get("curve"))
            try:
                out[name] = PrintArea(*box, quad=value.get("quad"), realism=realism, curve=curve)
            except ValidationError:
                if value.get("quad") is None:
                    raise
                # Corners a hand edit (or a later, stricter rule) broke: the rectangle
                # they were saved with still says where the print goes.
                out[name] = PrintArea(*box, realism=realism, curve=curve)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError(f"{path}: entry {name!r} is malformed ({exc})") from exc
    return out


def _level(value: Any) -> int | None:
    """A stored 0-100 setting: None when absent, clamped when a hand edit overshot."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        raise ValueError(f"{value!r} is not a number from 0 to 100")
    return int(round(min(100.0, max(0.0, float(value)))))


def save_positions(path: Path, positions: dict[str, PrintArea]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {name: area.to_dict() for name, area in sorted(positions.items())}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_legacy_positions(path: Path) -> dict[str, Any]:
    """Read an older pixel-based mockup-positions.json, ready for conversion."""
    if not path.exists():
        raise ValidationError(f"No such file: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{path} is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise ValidationError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValidationError(f"{path} should be an object keyed by mockup filename.")
    return raw


def import_pixel_positions(
    legacy: dict[str, Any],
    mockups: dict[str, tuple[int, int]],
    *,
    on_skip: Callable[[str, str], None] | None = None,
) -> dict[str, PrintArea]:
    """Convert an older pixel-based mockup-positions.json into fractions.

    Pixel rectangles only describe the one file they were measured on. Converting to
    fractions makes a single calibration cover every sibling mockup of the same
    dimensions, which is usually most of a set.

    Every entry that cannot be converted is dropped rather than raised, including a
    rectangle that overflows the mockup it names — a legacy file is nearly always part
    rubbish, and one stale measurement must not cost the seller the twenty good ones
    beside it. `on_skip` is handed the name and the reason so the caller can say out
    loud what was left behind instead of losing it silently.
    """

    def skip(name: str, reason: str) -> None:
        if on_skip:
            on_skip(name, reason)

    out: dict[str, PrintArea] = {}
    for name, value in (legacy or {}).items():
        if not isinstance(value, dict):
            skip(name, "not a rectangle")
            continue
        if name not in mockups:
            skip(name, "no mockup with that filename")
            continue
        width, height = mockups[name]
        if not width or not height:
            skip(name, "the mockup has no readable size")
            continue
        try:
            x = float(value.get("x", value.get("left", 0)))
            y = float(value.get("y", value.get("top", 0)))
            w = float(value.get("w", value.get("width", 0)))
            h = float(value.get("h", value.get("height", 0)))
        except (TypeError, ValueError):
            skip(name, "x/y/w/h are not numbers")
            continue
        if w <= 0 or h <= 0:
            skip(name, "the rectangle has no size")
            continue
        try:
            # Values already in 0..1 were fractions to begin with.
            if max(x + w, y + h) <= 1.0:
                out[name] = PrintArea(x, y, w, h)
            else:
                out[name] = PrintArea(x / width, y / height, w / width, h / height)
        except ValidationError as exc:
            skip(name, str(exc))
    return out


def _upright(image: Image.Image) -> Image.Image:
    """Return the image the way a viewer shows it, with its EXIF block left behind.

    Pillow writes only the EXIF a caller hands to ``save(exif=...)``, so the renders
    already come out clean — but a mockup shot on a phone carries GPS, and an image
    object still holding that block is one ``exif=`` away from publishing the seller's
    home address. Dropping it at the door makes that leak impossible, not unlikely.
    """
    upright = ImageOps.exif_transpose(image)
    upright.info.pop("exif", None)
    return upright


# Integer and float greyscale modes. Pillow clamps them to 0-255 on conversion rather
# than rescaling, so a 16-bit grey mockup at mid-grey comes out as a blank white frame.
_WIDE_GREY = ("I", "I;16", "I;16B", "I;16L", "I;16N", "F")


def _to_8bit(image: Image.Image) -> Image.Image:
    """Rescale 16/32-bit or float greyscale into 8 bits on its own scale, not clip it.

    The scale is the format's, not the image's brightest pixel: stretching to the
    maximum would turn a flat mid-grey backdrop white just as surely as clipping did.
    A wide file whose values already sit inside 0-255 is taken as 8-bit data.
    """
    if image.mode not in _WIDE_GREY:
        return image
    wide = image.convert("F") if image.mode == "F" else image.convert("I").convert("F")
    high = wide.getextrema()[1]
    if image.mode == "F" and high <= 1.0:
        scale = 255.0
    elif high <= 255:
        scale = 1.0
    elif high <= 65535:
        scale = 255 / 65535
    else:
        scale = 255 / high
    return wide.point(lambda v: v * scale).convert("L")


_SRGB = ImageCms.createProfile("sRGB") if ImageCms is not None else None


def _to_srgb(image: Image.Image) -> Image.Image:
    """Convert an image carrying an ICC profile into sRGB, then drop the profile.

    The JPEGs written here carry no profile, which every browser reads as sRGB. A
    Display P3 photo from an iPhone or a CMYK print file taken at face value therefore
    comes out dull or shifted. A profile that cannot be read is ignored rather than
    fatal — the image is still usable, just unmanaged, as it was before.
    """
    icc = image.info.get("icc_profile")
    if ImageCms is None or not icc or image.mode not in ("RGB", "RGBA", "CMYK", "L"):
        return image
    out_mode = "RGBA" if image.mode == "RGBA" else "RGB"
    try:
        source = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        converted = ImageCms.profileToProfile(image, source, _SRGB, outputMode=out_mode)
    except (ImageCms.PyCMSError, OSError, ValueError):
        return image
    if converted is None:
        return image
    converted.info.pop("icc_profile", None)
    return converted


def _as_displayed(image: Image.Image) -> Image.Image:
    """The image a viewer would show: turned upright, in 8 bits, in sRGB."""
    return _to_srgb(_to_8bit(_upright(image)))


def _display_size(image: Image.Image) -> tuple[int, int]:
    """The size a viewer reports, which is what a calibrated rectangle was measured on.

    Reading the tag is enough here. Decoding the pixels only to learn their shape would
    cost a full load per file, in a folder that can hold dozens of mockups.
    """
    if image.getexif().get(ORIENTATION_TAG) in QUARTER_TURNED:
        return image.height, image.width
    return image.size


def mockup_sizes(paths: list[Path]) -> dict[str, tuple[int, int]]:
    sizes: dict[str, tuple[int, int]] = {}
    for path in paths:
        try:
            with Image.open(path) as img:
                sizes[path.name] = _display_size(img)
        except (OSError, Image.DecompressionBombError):
            continue
    return sizes


def _why(exc: Exception) -> str:
    """Pillow's bomb guard says only that a limit was passed, never which one or how."""
    if isinstance(exc, Image.DecompressionBombError):
        return (
            f"{exc}. This is Pillow's decompression-bomb guard, not a corrupt file — a "
            f"very large print file can trip it. Downscale the design, or raise "
            f"PIL.Image.MAX_IMAGE_PIXELS if you trust the source"
        )
    return str(exc)


def _has_transparency(image: Image.Image) -> bool:
    """Alpha lives in a band for RGBA/LA/PA, and in info for palette and greyscale files."""
    return image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info


def _is_actually_transparent(image: Image.Image) -> bool:
    """Whether any pixel is see-through, not merely whether a channel exists.

    Canva, Figma and Photoshop's "Export As > PNG" all write an alpha channel on a
    fully opaque composition, so the channel's presence classifies a finished product
    photo as artwork and pastes the whole photo into a t-shirt. What the caller means
    by "artwork" is a file with something to see through, so that is what gets checked.
    """
    if not _has_transparency(image):
        return False
    if image.mode in ("RGBA", "LA", "PA"):
        return image.getchannel("A").getextrema()[0] < 255
    # A palette or greyscale file declares one index transparent; it counts only if the
    # image actually uses it.
    index = image.info.get("transparency")
    if isinstance(index, bytes):
        return any(alpha < 255 for alpha in index)
    if isinstance(index, int):
        low, high = image.convert("P").getextrema() if image.mode != "P" else image.getextrema()
        return low <= index <= high
    return True


def flatten_onto(image: Image.Image, background: tuple[int, int, int]) -> Image.Image:
    """Return `image` as RGB, compositing whatever transparency it has onto `background`.

    Pillow's RGBA->RGB conversion drops the alpha channel and keeps the colour hiding
    underneath it, which in a cut-out template is the black the exporter left there.
    Converting such a mockup straight to RGB therefore puts every image of the listing
    on a black background with nothing to show for it, so the transparency has to be
    composited away before the mode changes rather than by changing the mode.
    """
    # A photograph has no transparency to composite, and converting it is enough.
    if _has_transparency(image):
        ground = Image.new("RGBA", image.size, (*background, 255))
        return Image.alpha_composite(ground, image.convert("RGBA")).convert("RGB")
    return image.convert("RGB")


def compose(
    design_path: Path,
    mockup_path: Path,
    out_path: Path,
    *,
    area: PrintArea | None = None,
    min_edge: int = OUTPUT_MIN_EDGE,
    background: tuple[int, int, int] = WHITE,
    kind: str | None = None,
) -> Path:
    """Place one design inside one mockup's print area and write a JPEG.

    The design keeps its aspect ratio and is centred in the rectangle, so a square
    print area and a wide design produce letterboxing rather than distortion. A
    template with a transparent ground — which is most cut-out shirt and mug shots —
    is flattened onto `background` first, because a JPEG cannot carry the alpha and
    dropping it would leave the product sitting on black.

    `kind` is the mockup's type (catalog): it decides the realism and curve an area
    without its own uses (`styled`) and whether the design follows folds. Without it,
    and with neither set on the area, this is the flat paste it always was.
    """
    area = area or DEFAULT_PRINT_AREA
    try:
        with Image.open(mockup_path) as raw_mockup:
            mockup = flatten_onto(_as_displayed(raw_mockup), background)
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValidationError(f"Cannot open mockup {mockup_path.name}: {_why(exc)}") from exc

    try:
        with Image.open(design_path) as raw_design:
            design = _as_displayed(raw_design).convert("RGBA")
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValidationError(
            f"Cannot open design {design_path.name}: {_why(exc)}. "
            "If this is an iPhone HEIC photo, install the extra: pip install etsyotomasyon[heic]"
        ) from exc

    # Enlarge the mockup BEFORE the print box is measured, so the design is resampled
    # once, straight from the source, into final output coordinates. Compositing first
    # and enlarging afterwards would run a 4500px design down to the print box on a
    # small template and then blow the result back up — the second resize cannot
    # recreate detail the first one discarded, and the artwork is what buyers zoom into.
    if min_edge and min(mockup.size) < min_edge:
        factor = min_edge / min(mockup.size)
        mockup = mockup.resize(
            (round(mockup.width * factor), round(mockup.height * factor)), Image.LANCZOS
        )

    place(mockup, design, area, kind=kind)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    mockup.save(out_path, "JPEG", **JPEG_OPTIONS)
    return out_path


def place(canvas: Image.Image, design: Image.Image, area: PrintArea, *,
          kind: str | None = None) -> None:
    """Draw `design` (RGBA) into `area` on `canvas` (RGB, changed in place).

    The one compositing step every render goes through (compose(), so the app's runs,
    the CLI and the editor's preview). A rectangle with no curve and nothing to follow
    is the flat paste of old, pixel for pixel, with the shading on top when it has
    realism; four corners, a curve or folds go through `surface.render`.
    """
    area = styled(area, kind)
    realism, curve = area.realism or 0, area.curve or 0
    # Rigid products and anything curved (a tumbler or a bottle typed 'other') have no
    # folds: their highlight bands would only make the print wobble.
    displace = realism > 0 and kind not in RIGID_TYPES and curve == 0
    if area.quad is not None or curve > 0 or displace:
        surface.render(canvas, design, area.corners(*canvas.size),
                       realism=realism, curve=curve, displace=displace)
        return

    box_x, box_y, box_w, box_h = area.pixels(*canvas.size)
    scale = min(box_w / design.width, box_h / design.height)
    new_size = (max(1, round(design.width * scale)), max(1, round(design.height * scale)))
    design = design.resize(new_size, Image.LANCZOS)

    offset = (box_x + (box_w - new_size[0]) // 2, box_y + (box_h - new_size[1]) // 2)
    if realism > 0:
        ref = surface.reference(canvas.crop((box_x, box_y, box_x + box_w, box_y + box_h)),
                                surface.rect_corners(0, 0, box_w, box_h))
        if ref is not None:
            under = canvas.crop((offset[0], offset[1], offset[0] + new_size[0],
                                 offset[1] + new_size[1]))
            design = surface.shade(design, under, ref, realism, min(box_w, box_h))
    canvas.paste(design, offset, design)


def draw_preview(
    mockup_path: Path,
    out_path: Path,
    area: PrintArea,
    *,
    max_edge: int = PREVIEW_MAX_EDGE,
    background: tuple[int, int, int] = WHITE,
) -> Path:
    """Write a copy of the mockup with the print area drawn on it.

    This is the calibration interface. There is no GUI here, and four fractions tell
    nobody where a design will actually land on a photograph of a shirt — so the seller
    looks at this file, and the numbers only reach positions.json once it looks right.

    It therefore has to go through the same door compose() does. Turning the mockup
    upright and flattening it onto the same ground is not cosmetic here: a rectangle
    calibrated against a preview that disagreed with the render would be measured on
    the wrong shape, which is the one way this command could make things worse.
    """
    try:
        with Image.open(mockup_path) as raw:
            preview = flatten_onto(_as_displayed(raw), background).convert("RGBA")
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValidationError(f"Cannot open mockup {mockup_path.name}: {_why(exc)}") from exc

    box_x, box_y, box_w, box_h = area.pixels(*preview.size)
    box = (box_x, box_y, box_x + box_w - 1, box_y + box_h - 1)

    # The outline is drawn twice, a dark band immediately inside a bright one, because a
    # single colour vanishes on the mockup that happens to match it. The tint fills the
    # area itself, so a rectangle that landed off the garment is obvious at a glance.
    overlay = Image.new("RGBA", preview.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    edge = max(2, round(min(preview.size) / 250))
    if area.quad is not None:
        # Four corners: the shape itself, outlined the same two ways.
        points = area.corners(*preview.size)
        ring = [*points, points[0]]
        draw.polygon(points, fill=(255, 64, 64, 56))
        draw.line(ring, fill=(0, 0, 0, 220), width=edge * 3, joint="curve")
        draw.line(ring, fill=(255, 64, 64, 255), width=edge, joint="curve")
    else:
        draw.rectangle(box, fill=(255, 64, 64, 56))
        draw.rectangle(box, outline=(0, 0, 0, 220), width=edge * 3)
        draw.rectangle(box, outline=(255, 64, 64, 255), width=edge)
    preview = Image.alpha_composite(preview, overlay).convert("RGB")

    if max_edge and max(preview.size) > max_edge:
        factor = max_edge / max(preview.size)
        preview = preview.resize(
            (max(1, round(preview.width * factor)), max(1, round(preview.height * factor))),
            Image.LANCZOS,
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    preview.save(out_path, "JPEG", **JPEG_OPTIONS)
    return out_path


def flatten_design(
    design_path: Path,
    out_path: Path,
    *,
    edge: int = OUTPUT_MIN_EDGE,
    background: tuple[int, int, int] = WHITE,
) -> Path:
    """Render the artwork itself on white — the image print-on-demand buyers look for.

    Transparent artwork on Etsy's white page is invisible, so it gets a background, and
    `background` is the same knob compose() takes for shops that stage on another colour.
    """
    try:
        with Image.open(design_path) as raw:
            design = _as_displayed(raw).convert("RGBA")
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValidationError(f"Cannot open design {design_path.name}: {_why(exc)}") from exc

    canvas = Image.new("RGB", (edge, edge), background)
    scale = min(edge * 0.86 / design.width, edge * 0.86 / design.height)
    size = (max(1, round(design.width * scale)), max(1, round(design.height * scale)))
    design = design.resize(size, Image.LANCZOS)
    canvas.paste(design, ((edge - size[0]) // 2, (edge - size[1]) // 2), design)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, "JPEG", **JPEG_OPTIONS)
    return out_path


def to_uploadable(photo_path: Path, out_dir: Path) -> Path:
    """Re-encode a finished photo Etsy would refuse; return anything it accepts as is.

    Ready photos are uploaded byte for byte, which is the point of that route — but
    Etsy takes only JPG, PNG and GIF, and a seller exporting from Canva or a phone
    ends up with .webp without ever choosing it. Refusing the run would send them back
    to re-export forty files; converting without saying so would hide a lossless-to-
    lossy step they never asked for. So this converts and the caller warns. The copy
    lands in the batch folder beside the composites, where it is reviewed like any
    other output before anything is pushed.
    """
    if photo_path.suffix.lower() in UPLOADABLE_SUFFIXES:
        return photo_path

    try:
        with Image.open(photo_path) as raw:
            # Re-encoding drops the EXIF block, and with it the orientation tag Etsy
            # would otherwise have honoured — so the turn has to be applied here.
            photo = _as_displayed(raw).convert("RGBA")
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValidationError(f"Cannot open photo {photo_path.name}: {_why(exc)}") from exc

    # JPEG carries no alpha, and Etsy's product page is white — so a transparent ready
    # photo goes on white, which is what the buyer would have seen from it anyway.
    canvas = Image.new("RGB", photo.size, (255, 255, 255))
    canvas.paste(photo, (0, 0), photo)

    # The original suffix stays in the name: two files in one product folder can differ
    # only by extension, and the stem is kept first so natural filename order survives.
    out_path = out_dir / f"{photo_path.stem}-{photo_path.suffix.lstrip('.').lower()}.jpg"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, "JPEG", **JPEG_OPTIONS)
    return out_path


def looks_like_artwork(path: Path) -> bool:
    """Artwork needs compositing; a finished product photo does not.

    Transparency is the signal that actually means something: a photograph has none,
    and a print file almost always does. Everything else is guesswork, so when there
    is no alpha channel we treat it as a finished photo and let the seller override.

    It is the see-through pixels that decide, not the channel: an opaque RGBA export
    is a photo, however it was saved.
    """
    try:
        with Image.open(path) as img:
            return _is_actually_transparent(img)
    except (OSError, Image.DecompressionBombError):
        return False


# --- a design saved on a solid background ---------------------------------------------------

# Two border pixels are "the same colour" when no channel differs by more than this: a
# JPEG's white is 250-255, and its compression ringing stays well inside 24.
GROUND_TOLERANCE = 24
# A border is one flat colour when this share of its pixels is that colour: a few stray
# pixels (a corner mark, a signature) do not make a photo of it.
GROUND_SHARE = 0.985
# The border strip read on each side, in pixels.
GROUND_STRIP = 2
# The largest side the flood fill works on. The fill is Pillow's own pure-Python one, so
# it runs on a small copy; the full-size mask keeps the exact outline.
GROUND_FILL_EDGE = 480
# A design enlarged more than this onto a mockup or its flat render looks soft.
MAX_UPSCALE = 2.0


def _max_difference(image: Image.Image, colour: tuple[int, int, int]) -> Image.Image:
    """An "L" image: each pixel's largest channel difference from `colour`."""
    diff = ImageChops.difference(image, Image.new("RGB", image.size, colour))
    red, green, blue = diff.split()
    return ImageChops.lighter(ImageChops.lighter(red, green), blue)


def _median(histogram: list[int]) -> int:
    """The median value of one band, from its 256-bin histogram."""
    half = sum(histogram) / 2
    seen = 0
    for value, count in enumerate(histogram):
        seen += count
        if seen >= half:
            return value
    return 255


def ground_colour(path: Path) -> tuple[int, int, int] | None:
    """The one colour all four borders of an opaque design share, else None.

    A design exported on white (or any solid colour) has a flat border on every side; a
    finished product photo almost never does. A file with see-through pixels is artwork
    already and has no ground to find (None).
    """
    return design_ground(path)[1]


def design_ground(path: Path) -> tuple[str, tuple[int, int, int] | None]:
    """What a loose design's background is, from one read of the file:

    ("transparent", None) artwork with see-through pixels; ("flat", colour) an opaque
    design whose four borders share one colour; ("photo", None) anything else opaque;
    ("", None) a file that cannot be read.
    """
    try:
        with Image.open(path) as raw:
            if _is_actually_transparent(raw):
                return "transparent", None
            if raw.format == "JPEG":  # a border needs no full-size decode
                raw.draft("RGB", (max(64, raw.width // 4), max(64, raw.height // 4)))
            image = _as_displayed(raw).convert("RGB")
    except (OSError, ValueError, Image.DecompressionBombError):
        return "", None
    colour = _flat_border(image)
    return ("flat", colour) if colour is not None else ("photo", None)


def _flat_border(image: Image.Image) -> tuple[int, int, int] | None:
    width, height = image.size
    if width < 8 or height < 8:
        return None
    strip = GROUND_STRIP
    sides = [
        image.crop((0, 0, width, strip)),
        image.crop((0, height - strip, width, height)),
        image.crop((0, 0, strip, height)).transpose(Image.Transpose.ROTATE_90),
        image.crop((width - strip, 0, width, height)).transpose(Image.Transpose.ROTATE_90),
    ]
    border = Image.new("RGB", (sum(side.width for side in sides), strip))
    x = 0
    for side in sides:
        border.paste(side, (x, 0))
        x += side.width
    # The median colour, then the share of the border within the tolerance of it.
    colour = tuple(_median(band.histogram()) for band in border.split())
    near = _max_difference(border, colour).histogram()[: GROUND_TOLERANCE + 1]
    if sum(near) < GROUND_SHARE * border.width * border.height:
        return None
    return colour[0], colour[1], colour[2]


def remove_ground(design_path: Path, out_path: Path, colour: tuple[int, int, int], *,
                  tolerance: int = GROUND_TOLERANCE) -> Path:
    """Write the design as a PNG with its solid background made see-through.

    Only the background joined to the edges goes: a flood fill from the border, so white
    inside the design (the eyes of a face, the counter of an "o") stays. The fill runs on
    a small copy; the full-size colour mask keeps the outline sharp.
    """
    try:
        with Image.open(design_path) as raw:
            image = _as_displayed(raw).convert("RGB")
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValidationError(f"Cannot open design {design_path.name}: {_why(exc)}") from exc
    near = _max_difference(image, colour).point(lambda v: 255 if v <= tolerance else 0)
    scale = min(1.0, GROUND_FILL_EDGE / max(image.size))
    small_size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    small = near.resize(small_size, Image.NEAREST)
    # A frame of "ground" around it joins every border pixel, so one fill reaches them all.
    framed = ImageOps.expand(small, border=1, fill=255)
    ImageDraw.floodfill(framed, (0, 0), 128, thresh=0)
    joined = framed.crop((1, 1, framed.width - 1, framed.height - 1)).point(
        lambda v: 255 if v == 128 else 0)
    # Back at full size, a pixel is background when it is the colour AND joined to the edge.
    ground = ImageChops.darker(near, joined.resize(image.size, Image.NEAREST))
    out = image.convert("RGBA")
    out.putalpha(ImageChops.invert(ground))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.save(out_path, "PNG")
    return out_path


def upscale_factor(design_size: tuple[int, int], mockup_size: tuple[int, int] | None,
                   area: PrintArea | None = None, *, min_edge: int = OUTPUT_MIN_EDGE) -> float:
    """How much compose() enlarges a design of `design_size` on a mockup of `mockup_size`
    (flatten_design()'s canvas when `mockup_size` is None, with `min_edge` its edge)."""
    width, height = design_size
    if width <= 0 or height <= 0:
        return 1.0
    if mockup_size is None:
        return min(min_edge * 0.86 / width, min_edge * 0.86 / height)
    mock_w, mock_h = mockup_size
    if min_edge and min(mockup_size) < min_edge:
        factor = min_edge / min(mockup_size)
        mock_w, mock_h = round(mock_w * factor), round(mock_h * factor)
    _x, _y, box_w, box_h = (area or DEFAULT_PRINT_AREA).pixels(mock_w, mock_h)
    return min(box_w / width, box_h / height)


def display_size(path: Path) -> tuple[int, int] | None:
    """The size a viewer reports for an image file (upright), from its header only."""
    try:
        with Image.open(path) as img:
            return _display_size(img)
    except (OSError, ValueError, Image.DecompressionBombError):
        return None
