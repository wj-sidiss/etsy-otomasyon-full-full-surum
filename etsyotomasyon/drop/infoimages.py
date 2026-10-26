"""The shop's info images: the pictures every draft ends with.

A shop's listings often close on the same few cards: the materials, the sizes, how to
install, how to order a sample, how to measure. The seller picks them once, on Şablon
İlan (from the template listing's photos on Etsy, or image files of their own), and
every draft etsyotomasyon makes gets them after its own photos, in the seller's order, each
with its alt text. They are never watermarked or changed.

They are kept in the workspace as full-resolution copies, so a run needs no Etsy call
for them, and `drop run` / `drop auto` (the CLI) use them exactly as the app does:

    <workspace>/info-images/                    the pictures (JPG, PNG or GIF)
    <workspace>/info-images/info-images.json    first to last:
        {"images": [{"file": "etsy-4401.jpg", "alt": "Materials",
                     "listing_id": 1000013, "listing_image_id": 4401}, ...]}

A picture in the folder that the file does not list (one put there by hand) follows the
listed ones, in name order; an entry whose file is gone is ignored. At most
MAX_INFO_IMAGES are used.

Etsy takes MAX_LISTING_IMAGES (20) pictures per listing (createDraftListing's
`image_ids`, "up to 20 images", in the Open API spec). A draft's own photos come first,
so the mockups a design can use shrink by one per info image (`mockup_cap`, which
`catalog.usage` applies), and a product folder whose photos leave too little room gets
only the info images that fit, the first ones (`fitting`); the run says so.
"""

from __future__ import annotations

import io
import json
import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from ..client import MAX_ALT_TEXT, MAX_IMAGE_BYTES, UPLOADABLE_SUFFIXES
from ..config import MAX_LISTING_IMAGES
from ..errors import ValidationError
from .workspace import IMAGE_SUFFIXES, INFO_DIR, Workspace

INDEX_FILE = "info-images.json"
# Ten keeps room for nine mockups plus the flat design on every draft.
MAX_INFO_IMAGES = 10

# Read-modify-write of the index from several request threads.
_LOCK = threading.RLock()


class TooManyInfoImages(ValidationError):
    """More info images than MAX_INFO_IMAGES."""

    def __init__(self, count: int) -> None:
        self.count = count
        super().__init__(
            f"A listing can end with at most {MAX_INFO_IMAGES} info images ({count} given)."
        )


@dataclass
class InfoImage:
    name: str  # the file name in info-images/
    path: Path
    alt: str = ""
    listing_id: int | None = None  # the Etsy listing it was copied from, if any
    listing_image_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "alt": self.alt, "listing_id": self.listing_id,
                "listing_image_id": self.listing_image_id}


# --- the limit ------------------------------------------------------------------------


def mockup_cap(info: int, *, include_flat: bool = True) -> int:
    """How many mockups a design's draft can use beside `info` info images.

    Etsy's MAX_LISTING_IMAGES, minus the flat design, minus the info images.
    """
    return max(0, MAX_LISTING_IMAGES - (1 if include_flat else 0) - max(0, int(info)))


def fitting(photos: int, info: Sequence[Any]) -> list[Any]:
    """The info images that fit after `photos` of the product's own: the first ones."""
    room = max(0, MAX_LISTING_IMAGES - max(0, int(photos)))
    return list(info[:room])


# --- reading --------------------------------------------------------------------------


def folder(ws: Workspace) -> Path:
    return ws.root / INFO_DIR


def index_path(ws: Workspace) -> Path:
    return folder(ws) / INDEX_FILE


def _natural(name: str) -> list:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name.casefold())]


def _files(ws: Workspace) -> dict[str, Path]:
    """The image files in info-images/, by name."""
    where = folder(ws)
    if not where.is_dir():
        return {}
    try:
        return {
            p.name: p for p in where.iterdir()
            if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES and not p.name.startswith(".")
        }
    except OSError:
        return {}


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _read_index(ws: Workspace) -> tuple[list[dict[str, Any]], set[str]]:
    """(the listed entries, the names the seller took out that could not be moved)."""
    try:
        data = json.loads(index_path(ws).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], set()
    if not isinstance(data, dict):
        return [], set()
    entries = data.get("images")
    left_out = data.get("left_out")
    return (
        [e for e in entries or [] if isinstance(e, dict) and isinstance(e.get("file"), str)],
        {n for n in left_out or [] if isinstance(n, str)} if isinstance(left_out, list) else set(),
    )


def _all(ws: Workspace) -> list[InfoImage]:
    files = _files(ws)
    entries, left_out = _read_index(ws)
    out: list[InfoImage] = []
    seen: set[str] = set(left_out)
    for entry in entries:
        name = entry["file"]
        path = files.get(name)
        if path is None or name in seen:
            continue
        seen.add(name)
        alt = entry.get("alt")
        out.append(InfoImage(
            name=name, path=path, alt=str(alt).strip()[:MAX_ALT_TEXT] if isinstance(alt, str) else "",
            listing_id=_int(entry.get("listing_id")),
            listing_image_id=_int(entry.get("listing_image_id")),
        ))
    for name in sorted((n for n in files if n not in seen), key=_natural):
        out.append(InfoImage(name=name, path=files[name]))
    return out


def load(ws: Workspace) -> list[InfoImage]:
    """The info images every draft ends with, first to last (at most MAX_INFO_IMAGES)."""
    with _LOCK:
        return _all(ws)[:MAX_INFO_IMAGES]


def unused(ws: Workspace) -> list[InfoImage]:
    """Pictures in the folder beyond MAX_INFO_IMAGES: never sent (the CLI says so)."""
    with _LOCK:
        return _all(ws)[MAX_INFO_IMAGES:]


def count(ws: Workspace) -> int:
    return len(load(ws))


# --- writing --------------------------------------------------------------------------


def _write_index(ws: Workspace, images: Sequence[InfoImage],
                 left_out: Sequence[str] = ()) -> None:
    where = folder(ws)
    where.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {"images": [
        {"file": image.name, "alt": image.alt, "listing_id": image.listing_id,
         "listing_image_id": image.listing_image_id}
        for image in images
    ]}
    if left_out:
        data["left_out"] = sorted(left_out)
    target = index_path(ws)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)


_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(10)),
             *(f"lpt{i}" for i in range(10))}


def safe_name(filename: str) -> str:
    """A file name that is safe on Windows and macOS, keeping what can be kept."""
    base = re.split(r"[\\/]", str(filename or ""))[-1]
    base = _UNSAFE.sub("", base).strip().strip(".").strip()
    stem, suffix = Path(base).stem.strip(), Path(base).suffix.lower()
    stem = stem[:80].rstrip(". ") or "info"
    if stem.lower() in _RESERVED or stem.lower() == Path(INDEX_FILE).stem:
        stem = f"info-{stem}"
    return f"{stem}{suffix}"


def _uploadable(name: str, data: bytes) -> tuple[str, bytes]:
    """(name, bytes) of a picture Etsy takes as it is: JPG, PNG or GIF, at most 20 MB.

    A JPG, PNG or GIF of at most 20 MB is kept byte for byte, whatever its colour mode
    (a CMYK JPEG, a 16-bit PNG), as finished product photos are. Anything else Pillow
    reads (WEBP, BMP, TIFF, ...) is saved as a JPEG on white, full size; a file over
    20 MB is saved again smaller in quality, then in size. Refuses what is not an image
    (ValidationError) or is too large to decode (catalog.TooManyPixels).
    """
    from . import catalog, mockup

    if not data:
        raise ValidationError(f"{name} is empty.")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            size, fmt = probe.size, probe.format
            probe.verify()
    except Image.DecompressionBombError as exc:
        raise catalog.TooManyPixels(name, *catalog.bomb_size(exc)) from exc
    except (OSError, ValueError, SyntaxError) as exc:
        raise ValidationError(f"{name} is not an image that can be read ({exc}).") from exc
    catalog.check_pixels(name, size)
    suffix = Path(name).suffix.lower()
    as_is = {"JPEG": (".jpg", ".jpeg"), "PNG": (".png",), "GIF": (".gif",)}.get(fmt or "", ())
    if as_is and len(data) <= MAX_IMAGE_BYTES:
        # Kept byte for byte; a name that says another type (a PNG saved as .jpg, an
        # Etsy address that does not say) gets the type the file really is.
        if suffix in as_is and suffix in UPLOADABLE_SUFFIXES:
            return name, data
        return f"{Path(name).stem}{as_is[0]}", data
    with Image.open(io.BytesIO(data)) as opened:
        picture = mockup.flatten_onto(mockup._as_displayed(opened), mockup.WHITE)
    quality = 92
    for _attempt in range(10):
        out = io.BytesIO()
        picture.save(out, "JPEG", quality=quality, optimize=True)
        if out.tell() <= MAX_IMAGE_BYTES:
            return f"{Path(name).stem}.jpg", out.getvalue()
        if quality > 80:
            quality = 80
        else:
            width, height = picture.size
            picture = picture.resize(
                (max(1, round(width * 0.8)), max(1, round(height * 0.8))), Image.LANCZOS
            )
    raise ValidationError(f"{name} could not be made smaller than Etsy's 20 MB limit.")


def _store(ws: Workspace, filename: str, data: bytes) -> str:
    """Save the picture in info-images/ under a name no other file has; its name."""
    name = safe_name(filename)
    if Path(name).suffix.lower() not in IMAGE_SUFFIXES:
        raise ValidationError(
            f"{name}: info images can be "
            f"{', '.join(sorted(s.lstrip('.') for s in IMAGE_SUFFIXES))} pictures."
        )
    name, data = _uploadable(name, data)
    where = folder(ws)
    where.mkdir(parents=True, exist_ok=True)
    stem, suffix = Path(name).stem, Path(name).suffix
    number = 1
    while True:
        candidate = name if number == 1 else f"{stem}-{number}{suffix}"
        try:
            with (where / candidate).open("xb") as handle:
                handle.write(data)
        except FileExistsError:
            number += 1
            continue
        return candidate


def add(ws: Workspace, filename: str, data: bytes, *, alt: str = "",
        listing_id: int | None = None, listing_image_id: int | None = None) -> InfoImage:
    """Add a picture at the end. Never overwrites: a second `size.jpg` is `size-2.jpg`.

    TooManyInfoImages when MAX_INFO_IMAGES are there already; ValidationError for a
    file that is not a picture.
    """
    with _LOCK:
        current = _all(ws)
        if len(current) >= MAX_INFO_IMAGES:
            raise TooManyInfoImages(len(current) + 1)
        name = _store(ws, filename, data)
        image = InfoImage(name=name, path=folder(ws) / name, alt=str(alt or "").strip()[:MAX_ALT_TEXT],
                          listing_id=_int(listing_id), listing_image_id=_int(listing_image_id))
        files = _files(ws)
        _write_index(ws, [*current, image],
                     left_out=[n for n in _read_index(ws)[1] if n in files])
        return image


def arrange(ws: Workspace, images: Sequence[InfoImage]) -> list[InfoImage]:
    """Keep exactly `images`, in this order, with their alt texts: the seller's choice.

    Every name must be a picture in info-images/ (FileNotFoundError otherwise, nothing
    changed); a name listed twice is a ValidationError. A picture left out goes: a copy
    of an Etsy photo is deleted (the listing keeps its own), any other one (the seller's
    file) is moved to archive/info-images/. Returns the new `load(ws)`: pictures past
    MAX_INFO_IMAGES (put in the folder by hand) may stay listed, and are never sent.
    """
    names = [image.name for image in images]
    if len(set(names)) != len(names):
        raise ValidationError("An info image is listed twice.")
    with _LOCK:
        files = _files(ws)
        for name in names:
            if name not in files:
                raise FileNotFoundError(name)
        from_etsy = {image.name for image in _all(ws) if image.listing_image_id}
        kept = [
            InfoImage(name=image.name, path=files[image.name],
                      alt=str(image.alt or "").strip()[:MAX_ALT_TEXT],
                      listing_id=_int(image.listing_id),
                      listing_image_id=_int(image.listing_image_id))
            for image in images
        ]
        stuck: list[str] = []
        for name, path in files.items():
            if name in names:
                continue
            try:
                if name in from_etsy:
                    path.unlink()
                else:
                    _archive(ws, path)
            except OSError:
                stuck.append(name)  # open elsewhere: left out all the same (below)
        _write_index(ws, kept, left_out=stuck)
        return _all(ws)[:MAX_INFO_IMAGES]


def _archive(ws: Workspace, path: Path) -> Path:
    """Move a seller's picture to archive/info-images/ (never over another file)."""
    target_dir = ws.archive / INFO_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / path.name
    number = 1
    while target.exists():
        number += 1
        target = target_dir / f"{path.stem}-{number}{path.suffix}"
    path.replace(target)
    return target


def remove(ws: Workspace, name: str) -> list[InfoImage]:
    """Take one picture out (and delete etsyotomasyon's copy). FileNotFoundError if unknown."""
    with _LOCK:
        current = _all(ws)
        if name not in {image.name for image in current}:
            raise FileNotFoundError(name)
        return arrange(ws, [image for image in current if image.name != name])


def set_order(ws: Workspace, names: Sequence[str]) -> list[InfoImage]:
    """Keep exactly the pictures named, in this order, each with what it had (its alt
    text, the Etsy image it came from). The others are taken out, as by `arrange`.

    FileNotFoundError for a name that is not one of them (nothing changed then).
    """
    with _LOCK:
        known = {image.name: image for image in _all(ws)}
        for name in names:
            if name not in known:
                raise FileNotFoundError(name)
        return arrange(ws, [known[name] for name in names])


def find_etsy(ws: Workspace, listing_image_id: int) -> InfoImage | None:
    """The info image copied from this Etsy picture, if there is one."""
    for image in load(ws):
        if image.listing_image_id == listing_image_id:
            return image
    return None


__all__ = [
    "INDEX_FILE", "MAX_INFO_IMAGES", "InfoImage", "TooManyInfoImages", "add", "arrange",
    "count", "find_etsy", "fitting", "folder", "index_path", "load", "mockup_cap", "remove",
    "safe_name", "set_order", "unused",
]
