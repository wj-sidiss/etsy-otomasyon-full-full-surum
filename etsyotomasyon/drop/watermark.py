"""The seller's watermark, stamped on the photos a draft shows buyers.

A digital product is the file itself, and Etsy shows every listing photo to anyone at
up to 3000 px a side, so sellers of printables mark their previews. The watermark is a
picture of the seller's own (a logo, the shop name) kept at the workspace root beside
product.json, with its settings next to it:

    Etsy Studio/watermark.png    the mark, normalised when it is set: upright, sRGB,
                                 RGBA, its see-through margins trimmed, at most
                                 STORE_MAX_EDGE px on its longer side
    Etsy Studio/watermark.json   {"enabled": true, "scope": "digital",
                                  "position": "center", "opacity": 35, "size": 30,
                                  "tile_size": 15, "name": "logo.png"}

Neither is a mockup (1-MOCKUPS) nor a product (2-PRODUCTS), so the app's runs and the
CLI's `drop run` / `drop auto` read the same two files.

What it goes on: every picture a draft UPLOADS AS A PHOTO (the composited mockups, the
flat render or preview, a folder product's own photos, a finished photo uploaded as it
is), and nothing else. The files a buyer downloads are never touched: a stamped copy is
written beside the batch's other outputs, and the seller's originals stay as they are.

`scope` says which runs stamp: "digital" (the default) only when the template is a
download or both, "all" on every run. `position` is "center", "corner" (bottom right)
or "tiled" (repeated on a diagonal grid). `opacity` is 10-90 %. The size is a share of
the photo's width: `size` for one mark (default 30 %), `tile_size` for the repeated one
(default 15 %), so the mark looks the same on a 1200 px preview and a 3000 px mockup.

A stamped copy keeps the photo's pixel size and format family (a JPEG stays a JPEG at
the app's own quality, anything else becomes a PNG), is turned upright and carries no
EXIF or colour profile (the pixels are converted to sRGB first).
"""

from __future__ import annotations

import io
import json
import math
import os
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from PIL import Image

from ..errors import ValidationError
from . import catalog, mockup
from .workspace import Workspace

WATERMARK_FILE = "watermark.png"
SETTINGS_FILE = "watermark.json"
# Stamped copies land in this one folder of the batch (every product's), so a design's
# name is in the path once; the file keeps its name, which says which mockup it was made
# on (web/api/listings reads it), and a clash gets "-2" (the run's one `taken` set).
STAMPED_DIR = "watermarked"
# Guards a `taken` set shared by a run's worker threads (stamp's name check-and-add).
_TAKEN_LOCK = threading.Lock()

CENTER, CORNER, TILED = "center", "corner", "tiled"
POSITIONS = (CENTER, CORNER, TILED)
SCOPE_DIGITAL, SCOPE_ALL = "digital", "all"
SCOPES = (SCOPE_DIGITAL, SCOPE_ALL)

OPACITY_MIN, OPACITY_MAX, OPACITY_DEFAULT = 10, 90, 35
SIZE_MIN, SIZE_MAX, SIZE_DEFAULT = 5, 60, 30  # one mark, % of the photo's width
TILE_SIZE_MIN, TILE_SIZE_MAX, TILE_SIZE_DEFAULT = 5, 40, 15  # each repeated mark

# What a seller may upload as the mark. PNG keeps transparency, which a mark needs;
# JPG and WebP are taken too (an opaque one shows as a see-through box).
UPLOAD_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MIN_EDGE = 16  # a smaller picture cannot be enlarged into a readable mark
STORE_MAX_EDGE = 3000  # no listing photo is wider than Etsy's 3000 px view

# The corner mark's distance from the edges, as a share of the photo's shorter side.
CORNER_MARGIN = 0.035
# The repeated mark: rows turned this many degrees (rising to the right), a gap of this
# share of the mark's width between marks, and every other row shifted by half a step.
TILE_ANGLE = 30
TILE_GAP = 0.6

_LOCK = threading.RLock()


class WatermarkError(ValidationError):
    """A watermark that cannot be used: a code the app translates, plus its params."""

    def __init__(self, code: str, message: str, **params: Any) -> None:
        super().__init__(message)
        self.code = code
        self.params = params


# --- settings ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    enabled: bool = True
    scope: str = SCOPE_DIGITAL
    position: str = CENTER
    opacity: int = OPACITY_DEFAULT
    size: int = SIZE_DEFAULT
    tile_size: int = TILE_SIZE_DEFAULT
    name: str = ""  # the uploaded file's own name, for the screen

    @property
    def mark_size(self) -> int:
        """The size the position uses: tile_size for the repeated mark, else size."""
        return self.tile_size if self.position == TILED else self.size

    def applies_to(self, listing_type: str | None) -> bool:
        """Whether a run with this template type stamps (enabled aside)."""
        if self.scope == SCOPE_ALL:
            return True
        return str(listing_type or "physical") in ("download", "both")

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "scope": self.scope, "position": self.position,
                "opacity": self.opacity, "size": self.size, "tile_size": self.tile_size,
                "name": self.name}


DEFAULTS = Settings()

# field -> (low, high) for the whole-number settings.
_RANGES = {
    "opacity": (OPACITY_MIN, OPACITY_MAX),
    "size": (SIZE_MIN, SIZE_MAX),
    "tile_size": (TILE_SIZE_MIN, TILE_SIZE_MAX),
}


def watermark_path(ws: Workspace) -> Path:
    return ws.root / WATERMARK_FILE


def settings_path(ws: Workspace) -> Path:
    return ws.root / SETTINGS_FILE


def _whole(value: Any) -> int | None:
    """An int from a JSON number (a whole float too); None for anything else."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and (not math.isfinite(value) or value != int(value)):
        return None
    return int(value)


def _from_raw(raw: dict[str, Any]) -> Settings:
    """Settings from a stored file, forgiving: what cannot be read takes its default."""
    values: dict[str, Any] = {}
    if isinstance(raw.get("enabled"), bool):
        values["enabled"] = raw["enabled"]
    if raw.get("scope") in SCOPES:
        values["scope"] = raw["scope"]
    if raw.get("position") in POSITIONS:
        values["position"] = raw["position"]
    for key, (low, high) in _RANGES.items():
        number = _whole(raw.get(key))
        if number is not None:
            values[key] = min(high, max(low, number))
    if isinstance(raw.get("name"), str):
        values["name"] = raw["name"][:120]
    return replace(DEFAULTS, **values)


def load_settings(ws: Workspace) -> Settings:
    """The saved settings; the defaults (switched on) when there is no file yet."""
    try:
        raw = json.loads(settings_path(ws).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULTS
    return _from_raw(raw) if isinstance(raw, dict) else DEFAULTS


def _write_settings(ws: Workspace, settings: Settings) -> None:
    path = settings_path(ws)
    data = {"version": 1, **settings.to_dict(), "updated_at": int(time.time())}
    tmp = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def validate(changes: dict[str, Any]) -> dict[str, Any]:
    """The changes as Settings fields; WatermarkError("invalid", field=...) when one is bad.

    enabled: true/false; scope: digital | all; position: center | corner | tiled;
    opacity 10-90, size 5-60, tile_size 5-40 (whole numbers). Unknown fields are refused.
    """
    out: dict[str, Any] = {}
    for key, value in changes.items():
        if key == "enabled":
            if not isinstance(value, bool):
                raise WatermarkError("invalid", "enabled must be true or false", field=key)
            out[key] = value
        elif key == "scope":
            if value not in SCOPES:
                raise WatermarkError("invalid", f"scope must be one of {', '.join(SCOPES)}",
                                     field=key)
            out[key] = value
        elif key == "position":
            if value not in POSITIONS:
                raise WatermarkError(
                    "invalid", f"position must be one of {', '.join(POSITIONS)}", field=key)
            out[key] = value
        elif key in _RANGES:
            low, high = _RANGES[key]
            number = _whole(value)
            if number is None or not low <= number <= high:
                raise WatermarkError("invalid", f"{key} must be a whole number from {low} to "
                                     f"{high}", field=key, min=low, max=high)
            out[key] = number
        else:
            raise WatermarkError("invalid", f"Unknown watermark setting {key!r}.", field=key)
    return out


def update_settings(ws: Workspace, changes: dict[str, Any] | None = None, /,
                    **more: Any) -> Settings:
    """Validate and save some settings (a dict, or keywords); returns the new ones.

    Switching the watermark on needs its file (WatermarkError "watermark_missing").
    """
    fields = validate({**(changes or {}), **more})
    with _LOCK:
        if fields.get("enabled") and not watermark_path(ws).is_file():
            raise WatermarkError("watermark_missing", "Add a watermark picture first.")
        settings = replace(load_settings(ws), **fields)
        _write_settings(ws, settings)
    return settings


# --- the file -------------------------------------------------------------------------------


def _version(path: Path) -> str:
    st = path.stat()
    return f"{st.st_mtime_ns:x}-{st.st_size:x}"


def file_info(ws: Workspace) -> dict[str, Any] | None:
    """The mark's facts for a screen (from its header and borders); None without one.

    ground: "transparent" (it has see-through pixels), "flat" (opaque on one solid
    colour, which remove_ground can make see-through) or "photo"; "" if unreadable.
    """
    path = watermark_path(ws)
    if not path.is_file():
        return None
    try:
        with Image.open(path) as image:
            width, height = image.size
        version = _version(path)
    except (OSError, ValueError, Image.DecompressionBombError):
        return {"name": load_settings(ws).name or WATERMARK_FILE, "path": WATERMARK_FILE,
                "width": None, "height": None, "version": "0", "ground": "",
                "readable": False}
    ground, _colour = mockup.design_ground(path)
    return {"name": load_settings(ws).name or WATERMARK_FILE, "path": WATERMARK_FILE,
            "width": width, "height": height, "version": version, "ground": ground,
            "readable": True}


def _normalise(image: Image.Image) -> Image.Image:
    """Upright, 8-bit sRGB RGBA, see-through margins trimmed, at most STORE_MAX_EDGE."""
    mark = mockup._as_displayed(image).convert("RGBA")
    box = mark.getchannel("A").getbbox()
    if box is None:
        raise WatermarkError("watermark_blank", "The picture has nothing visible in it.")
    if box != (0, 0, mark.width, mark.height):
        mark = mark.crop(box)
    if max(mark.size) > STORE_MAX_EDGE:
        factor = STORE_MAX_EDGE / max(mark.size)
        mark = _resize(mark, (max(1, round(mark.width * factor)),
                              max(1, round(mark.height * factor))))
    return mark


def _save_mark(ws: Workspace, mark: Image.Image) -> Path:
    path = watermark_path(ws)
    ws.root.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.stem}.{threading.get_ident()}.tmp")
    mark.info = {}  # the uploaded file's EXIF, profile and text chunks stay behind
    mark.save(tmp, "PNG", optimize=False)
    os.replace(tmp, path)
    return path


def read_upload(filename: str, data: bytes) -> Image.Image:
    """The uploaded picture, checked and normalised (see _normalise).

    WatermarkError codes: watermark_type (not PNG/JPG/WebP by name or content),
    watermark_image (empty or unreadable), watermark_too_large (over 10 MB),
    too_many_pixels (catalog's limits), watermark_too_small (a side under 16 px),
    watermark_blank (fully see-through).
    """
    name = catalog.safe_name(filename or "")
    suffix = Path(name).suffix.lower()
    if suffix not in UPLOAD_SUFFIXES:
        raise WatermarkError("watermark_type", f"{name}: a watermark can be a PNG, JPG or "
                             "WebP picture.", name=name)
    if not data:
        raise WatermarkError("watermark_image", f"{name} is empty.", name=name)
    if len(data) > MAX_UPLOAD_BYTES:
        raise WatermarkError(
            "watermark_too_large",
            f"{name} is {len(data) / 1024 / 1024:.1f} MB; a watermark can be at most "
            f"{MAX_UPLOAD_BYTES // 1024 // 1024} MB.",
            name=name, max_mb=MAX_UPLOAD_BYTES // 1024 // 1024,
        )
    try:
        with Image.open(io.BytesIO(data)) as opened:
            if opened.format not in ("PNG", "JPEG", "WEBP", "MPO"):
                raise WatermarkError("watermark_type", f"{name} is not a PNG, JPG or WebP "
                                     "picture.", name=name)
            size = opened.size
            if catalog.too_many_pixels(size):
                raise catalog.TooManyPixels(name, *size)
            if min(size) < MIN_EDGE:
                raise WatermarkError(
                    "watermark_too_small",
                    f"{name} is {size[0]}x{size[1]} px; a watermark needs at least "
                    f"{MIN_EDGE} px on each side.",
                    name=name, width=size[0], height=size[1], min=MIN_EDGE,
                )
            opened.load()
            return _normalise(opened)
    except Image.DecompressionBombError as exc:
        raise catalog.TooManyPixels(name, *catalog.bomb_size(exc)) from exc
    except (OSError, ValueError, SyntaxError) as exc:
        raise WatermarkError("watermark_image", f"{name} is not a picture that can be "
                             f"read ({exc}).", name=name) from exc


def save_upload(ws: Workspace, filename: str, data: bytes) -> Settings:
    """Make an uploaded picture the watermark (replacing any); returns the settings.

    The first watermark of a workspace is switched on; replacing one keeps its settings.
    """
    mark = read_upload(filename, data)
    with _LOCK:
        had = watermark_path(ws).is_file()
        _save_mark(ws, mark)
        settings = load_settings(ws)
        settings = replace(settings, name=catalog.safe_name(filename)[:120],
                           enabled=True if not had else settings.enabled)
        _write_settings(ws, settings)
    return settings


def remove(ws: Workspace) -> bool:
    """Delete the watermark picture (its settings stay for the next one). False if none."""
    with _LOCK:
        try:
            watermark_path(ws).unlink()
        except FileNotFoundError:
            return False
        return True


def remove_ground(ws: Workspace) -> Settings:
    """Make an opaque mark's solid background see-through (mockup.remove_ground).

    WatermarkError "watermark_missing" without a mark, "watermark_no_ground" when it has
    no single-colour background to take away.
    """
    path = watermark_path(ws)
    with _LOCK:
        if not path.is_file():
            raise WatermarkError("watermark_missing", "Add a watermark picture first.")
        kind, colour = mockup.design_ground(path)
        if kind != "flat" or colour is None:
            raise WatermarkError("watermark_no_ground",
                                 "The watermark has no single-colour background to remove.")
        tmp = path.with_name(f"{path.stem}.{threading.get_ident()}.ground.png")
        try:
            mockup.remove_ground(path, tmp, colour)
            with Image.open(tmp) as keyed:
                keyed.load()
                mark = _normalise(keyed)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        _save_mark(ws, mark)
        return load_settings(ws)


# --- stamping ----------------------------------------------------------------------------------


def _resize(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """An RGBA resize on premultiplied colour, so see-through pixels leave no dark fringe."""
    if image.size == size:
        return image
    return image.convert("RGBa").resize(size, Image.LANCZOS).convert("RGBA")


def _faded(mark: Image.Image, opacity: int) -> Image.Image:
    factor = max(0, min(100, opacity)) / 100
    alpha = mark.getchannel("A").point([round(v * factor) for v in range(256)])
    out = mark.copy()
    out.putalpha(alpha)
    return out


def _composite(base: Image.Image, tile: Image.Image, x: int, y: int) -> None:
    """alpha_composite `tile` at (x, y), clipped to `base` (x or y may be negative)."""
    left, top = max(0, x), max(0, y)
    right, bottom = min(base.width, x + tile.width), min(base.height, y + tile.height)
    if right <= left or bottom <= top:
        return
    base.alpha_composite(tile, dest=(left, top),
                         source=(left - x, top - y, right - x, bottom - y))


def _scaled(mark: Image.Image, width: int, box: tuple[int, int]) -> Image.Image:
    """The mark `width` px wide, never larger than `box`, its shape kept."""
    scale = min(width / mark.width, box[0] / mark.width, box[1] / mark.height)
    size = (max(1, round(mark.width * scale)), max(1, round(mark.height * scale)))
    return _resize(mark, size)


def apply(image: Image.Image, mark: Image.Image, settings: Settings) -> Image.Image:
    """`image` (as displayed: mockup._as_displayed) with the mark on it, same pixel size.

    An image with see-through pixels comes back RGBA, any other RGB.
    """
    keep_alpha = image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info
    base = image.convert("RGBA")
    width, height = base.size
    mark = mark.convert("RGBA")
    share = settings.mark_size / 100
    if settings.position == TILED:
        tile = _faded(_scaled(mark, max(4, round(width * share)), (width, height)),
                      settings.opacity)
        _tile(base, tile)
    else:
        margin = max(2, round(min(width, height) * CORNER_MARGIN))
        room = (max(1, width - 2 * margin), max(1, height - 2 * margin))
        one = _faded(_scaled(mark, max(1, round(width * share)), room), settings.opacity)
        if settings.position == CORNER:
            x, y = width - margin - one.width, height - margin - one.height
        else:
            x, y = (width - one.width) // 2, (height - one.height) // 2
        _composite(base, one, x, y)
    return base if keep_alpha else base.convert("RGB")


def _tile(base: Image.Image, tile: Image.Image) -> None:
    """Repeat `tile` over `base` on a grid turned TILE_ANGLE degrees, rows staggered."""
    width, height = base.size
    turned = tile.convert("RGBa").rotate(TILE_ANGLE, resample=Image.BICUBIC,
                                          expand=True).convert("RGBA")
    step_x = tile.width + max(1, round(tile.width * TILE_GAP))
    step_y = tile.height + max(1, round(tile.width * TILE_GAP))
    angle = math.radians(TILE_ANGLE)
    cos, sin = math.cos(angle), math.sin(angle)
    reach = math.hypot(width, height) / 2 + max(turned.size)
    cols = int(reach // step_x) + 2
    rows = int(reach // step_y) + 2
    cx, cy = width / 2, height / 2
    for row in range(-rows, rows + 1):
        shift = step_x / 2 if row % 2 else 0.0
        for col in range(-cols, cols + 1):
            # A point of the unturned grid, turned with the marks (y grows downwards).
            gx, gy = col * step_x + shift, row * step_y
            px = cx + gx * cos + gy * sin
            py = cy - gx * sin + gy * cos
            _composite(base, turned, round(px - turned.width / 2),
                       round(py - turned.height / 2))


@dataclass(frozen=True)
class Watermark:
    """A loaded mark and the settings to stamp it with."""

    mark: Image.Image
    settings: Settings

    def apply(self, image: Image.Image) -> Image.Image:
        return apply(image, self.mark, self.settings)

    def stamp(self, source: Path, out_dir: Path, taken: set[str] | None = None) -> Path:
        """A stamped copy of the photo `source` in `out_dir`; `source` is never changed.

        Same name, except a picture that is not a JPEG becomes a PNG (a GIF included);
        `taken` (casefolded names already written in `out_dir` by this run, which its
        threads may share) gets a "-2" for a clash. Upright, sRGB, no EXIF, same pixel
        size as displayed.
        """
        try:
            with Image.open(source) as raw:
                jpeg = raw.format in ("JPEG", "MPO")
                picture = mockup._as_displayed(raw)
                picture.load()
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            raise ValidationError(f"Cannot open {source.name}: {exc}") from exc
        stamped = self.apply(picture)
        # Nothing of the source's metadata travels: no EXIF, colour profile, comment or
        # text chunk (Pillow writes some of these from `info` when it is left there).
        stamped.info = {}
        suffix = ".jpg" if jpeg else ".png"
        name = f"{source.stem}{suffix}"
        if taken is not None:
            with _TAKEN_LOCK:
                number = 1
                while name.casefold() in taken:
                    number += 1
                    name = f"{source.stem}-{number}{suffix}"
                taken.add(name.casefold())
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / name
        if jpeg:
            stamped.convert("RGB").save(out, "JPEG", **mockup.JPEG_OPTIONS)
        else:
            stamped.save(out, "PNG")
        return out


def load(ws: Workspace, settings: Settings | None = None) -> Watermark | None:
    """The mark with its settings (switched on or not); None when there is no mark.

    Raises WatermarkError("watermark_unreadable") when the file cannot be read.
    """
    path = watermark_path(ws)
    if not path.is_file():
        return None
    try:
        with Image.open(path) as opened:
            if catalog.too_many_pixels(opened.size):
                raise catalog.TooManyPixels(path.name, *opened.size)
            mark = mockup._as_displayed(opened).convert("RGBA")
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError,
            catalog.TooManyPixels) as exc:
        raise WatermarkError(
            "watermark_unreadable",
            f"The watermark {path} cannot be read ({exc}). Set it again in the app's "
            "Mockups page, or remove the file.",
        ) from exc
    return Watermark(mark, settings if settings is not None else load_settings(ws))


def for_run(ws: Workspace, listing_type: str | None) -> Watermark | None:
    """The watermark a run with this template type stamps; None when it stamps nothing.

    Nothing without a mark, when it is switched off, or when it is only for digital
    products and the template is physical. A mark that is on but cannot be read raises
    WatermarkError("watermark_unreadable"): the run must not upload unmarked photos.
    """
    settings = load_settings(ws)
    if not settings.enabled or not settings.applies_to(listing_type):
        return None
    return load(ws, settings)


def describe(ws: Workspace, listing_type: str | None = None) -> dict[str, Any]:
    """What a screen or the CLI says about the watermark (no pixels decoded).

    {"file": bool, "name", "enabled", "on" (enabled with a file), "scope", "position",
     "opacity", "size" (the position's), "applies" (a run with `listing_type` stamps;
     None when the type is unknown), "problem": None | "unreadable"}
    """
    settings = load_settings(ws)
    path = watermark_path(ws)
    has_file = path.is_file()
    problem = None
    if has_file:
        try:
            with Image.open(path):  # the header is enough to know it opens
                pass
        except (OSError, ValueError, Image.DecompressionBombError):
            problem = "unreadable"
    on = has_file and settings.enabled
    return {
        "file": has_file,
        "name": settings.name or (WATERMARK_FILE if has_file else ""),
        "enabled": settings.enabled,
        "on": on,
        "scope": settings.scope,
        "position": settings.position,
        "opacity": settings.opacity,
        "size": settings.mark_size,
        "applies": None if listing_type is None else bool(on and settings.applies_to(
            listing_type)),
        "problem": problem,
    }


def summary(info: dict[str, Any]) -> str:
    """describe() in one English line for the CLI ("" when there is no watermark)."""
    if not info["file"]:
        return ""
    if not info["on"]:
        return "Watermark: off."
    where = {"center": "centred", "corner": "bottom-right corner",
             "tiled": "repeated diagonally"}[info["position"]]
    scope = "digital products only" if info["scope"] == SCOPE_DIGITAL else "every listing"
    line = (f"Watermark: on · {scope} · {where}, {info['opacity']}% opacity, "
            f"{info['size']}% of the photo width.")
    if info["applies"] is False:
        line += " This template is physical, so none is added."
    return line


__all__ = [
    "CENTER", "CORNER", "DEFAULTS", "POSITIONS", "SCOPES", "SCOPE_ALL", "SCOPE_DIGITAL",
    "STAMPED_DIR", "Settings", "TILED", "Watermark", "WatermarkError", "apply", "describe",
    "file_info", "for_run", "load", "load_settings", "read_upload", "remove",
    "remove_ground", "save_upload", "summary", "update_settings",
]
