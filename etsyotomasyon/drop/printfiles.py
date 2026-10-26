"""Print files: the design behind every listing, kept under its Etsy listing id.

A listing made from mockups sells a product that cannot be printed once its flat design
is lost: the mockups are all Etsy has. So every draft etsyotomasyon makes keeps a copy of what
it was made from in print-files/<listing id>/, and the seller adds the file of any other
listing on its İlanlar page (or copies it into the folder). The folder is the record: a
listing has a print file when its folder holds a file, whoever put it there.

What a product's print file is: a loose design, the file itself, byte for byte; a product
folder, the files in its `dosyalar` / `files` subfolder (pipeline.deliverable_files). A
folder of ready photos without one has none: its photos are mockups too.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import warnings
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..errors import ValidationError
from . import catalog
from .pipeline import _seller_file, deliverable_files
from .workspace import ARCHIVE_DIR, IMAGE_SUFFIXES, PRINT_DIR, PRODUCTS_DIR

log = logging.getLogger("etsyotomasyon.drop")

# What a seller may add by hand: images, and the formats printers and design tools hand
# over. A program or a script is never a print file.
PRINT_SUFFIXES = frozenset(IMAGE_SUFFIXES | {".pdf", ".svg", ".psd", ".ai", ".eps", ".zip"})
# A copy is written beside its target under this hidden name, then moved into place, so
# a half-written file is never taken for the listing's print file.
PART_PREFIX = ".part-"

_ID = re.compile(r"[0-9]+")
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def folder(root: Path, listing_id: int) -> Path:
    """print-files/<listing id> in the workspace at `root` (not created)."""
    return root / PRINT_DIR / str(int(listing_id))


def files_in(path: Path) -> list[Path]:
    """The finished files directly in `path` (no hidden, system or half-written ones)."""
    try:
        entries = list(path.iterdir())
    except OSError:
        return []
    return sorted(
        (p for p in entries if p.is_file() and _seller_file(p.name)
         and not p.name.startswith(PART_PREFIX)),
        key=lambda p: p.name.casefold(),
    )


def files(root: Path, listing_id: int) -> list[Path]:
    """The listing's print files, by name; [] when it has none."""
    return files_in(folder(root, listing_id))


def listing_ids(root: Path) -> set[int]:
    """The listings that have a print file: a folder named after the id that holds one."""
    try:
        entries = list((root / PRINT_DIR).iterdir())
    except OSError:
        return set()
    return {int(e.name) for e in entries
            if _ID.fullmatch(e.name) and e.is_dir() and files_in(e)}


def _copy_into(target: Path, name: str, write) -> Path:
    """Write a file called `name` in `target` through `write(temporary)`, atomically.

    A file of that name already there is kept: the new one gets "name (2).ext".
    """
    target.mkdir(parents=True, exist_ok=True)
    dest = target / name
    stem, suffix = Path(name).stem, Path(name).suffix
    n = 2
    while dest.exists():
        dest = target / f"{stem} ({n}){suffix}"
        n += 1
    temporary = target / f"{PART_PREFIX}{dest.name}"
    try:
        write(temporary)
        os.replace(temporary, dest)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return dest


def _same_size(path: Path, size: int) -> bool:
    try:
        return path.is_file() and path.stat().st_size == size
    except OSError:
        return False


def keep(root: Path, listing_id: int, source: Path) -> list[Path]:
    """Copy the print file of the product at `source` into the listing's folder.

    A file of the same name and size already there is the same file and is not copied
    again. Never raises: the draft exists whatever happens here, so a copy that fails
    is logged and the listing simply shows no print file yet.
    """
    kept: list[Path] = []
    try:
        wanted = deliverable_files(source)
        target = folder(root, listing_id)
        for path in wanted:
            size = path.stat().st_size
            existing = target / path.name
            if _same_size(existing, size):
                kept.append(existing)
                continue
            kept.append(_copy_into(target, path.name,
                                   lambda tmp, src=path: shutil.copy2(src, tmp)))
    except OSError as exc:
        log.warning("could not keep the print file of listing %s: %s", listing_id, exc)
    return kept


def backfill(root: Path, sources: Mapping[int, str]) -> int:
    """Keep the print file of every listing etsyotomasyon made that has none yet, when its
    design is still in 2-PRODUCTS or archive/. `sources`: listing id -> the product's
    name (upload-history.json). How many listings got one."""
    done = 0
    have = listing_ids(root)
    for listing_id, name in sources.items():
        if listing_id in have or not name or Path(name).name != name:
            continue
        for base in (PRODUCTS_DIR, ARCHIVE_DIR):
            source = root / base / name
            if source.exists():
                if keep(root, listing_id, source):
                    done += 1
                break
    return done


def clean_name(name: str) -> str:
    """A file name safe in any folder: no path, no characters Windows refuses."""
    base = _UNSAFE.sub("_", Path(str(name or "").replace("\\", "/")).name).strip(" .")
    return base[:150]


def add(root: Path, listing_id: int, name: str, data: bytes) -> tuple[Path, bool]:
    """Save a file the seller added as the listing's print file.

    (path, duplicate): the same bytes under the same name are already there.
    """
    safe = clean_name(name)
    suffix = Path(safe).suffix.lower()
    if not safe or suffix not in PRINT_SUFFIXES:
        raise ValidationError(
            f"{safe or 'This file'} is not a print file: use an image, PDF, SVG, PSD, AI, "
            "EPS or ZIP file."
        )
    target = folder(root, listing_id)
    existing = target / safe
    if _same_size(existing, len(data)):
        try:
            if existing.read_bytes() == data:
                return existing, True
        except OSError:
            pass

    def write(tmp: Path) -> None:
        with tmp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

    return _copy_into(target, safe, write), False


def describe(root: Path, path: Path) -> dict[str, Any]:
    """One print file for the page: name, size, pixels when it is an image."""
    try:
        size = path.stat().st_size
        mtime = int(path.stat().st_mtime)
    except OSError:
        size, mtime = 0, 0
    width = height = None
    image = path.suffix.lower() in IMAGE_SUFFIXES
    if image:
        from PIL import Image

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", Image.DecompressionBombWarning)
                with Image.open(path) as opened:
                    width, height = opened.size
        except Image.DecompressionBombError as exc:
            width, height = catalog.bomb_size(exc)
        except (OSError, ValueError, SyntaxError):
            image = False
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        rel = path.name
    return {
        "name": path.name,
        "rel": rel,
        "size": size,
        "mtime": mtime,
        "width": width,
        "height": height,
        "image": image,
        # The page asks for a thumbnail only of what the thumbnailer will decode.
        "thumb": bool(image and width and height
                      and not catalog.too_many_pixels((width, height))),
    }
