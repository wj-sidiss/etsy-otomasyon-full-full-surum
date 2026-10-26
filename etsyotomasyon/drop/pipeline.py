"""Folder of designs in, validated listing CSV out.

The pipeline stops at the CSV on purpose. `etsyotomasyon listings push` takes it from
there, and that code already has tests behind it — so everything here sits *before*
the tested boundary rather than inside it, and a seller who would rather work in a
spreadsheet can edit the file and get an identical result.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from PIL import Image

from .. import csvio
from ..client import MAX_IMAGE_BYTES, MAX_LISTING_FILES, EtsyClient, file_issue
from ..config import MAX_LISTING_IMAGES, MAX_TITLE_LEN
from ..errors import ValidationError
from ..listings import (
    DIGITAL_TYPES,
    FILES_COLUMN,
    LISTING_COLUMNS,
    TITLE_ONCE,
    TITLE_SYMBOLS,  # noqa: F401 - re-exported: pipeline.TITLE_SYMBOLS
    title_char_ok,
    title_problems,  # noqa: F401 - re-exported: pipeline.title_problems
)
from ..seo import MarketReport, research
from . import cache, catalog, generate, infoimages, mockup, seeds
from . import watermark as watermark_mod
from .template import Template
from .workspace import FILES_DIRS, INFO_DIR, Workspace

log = logging.getLogger(__name__)

REVIEW_FILE = "review.csv"
# Beside review.csv: the info images' alt texts ({"info-images/<file>": alt}), so a later
# `etsyotomasyon listings push` of the batch sends them as drop auto and the app do.
INFO_ALTS_FILE = "info-alts.json"

# Columns the review file carries beyond what `listings push` reads. push() ignores
# extras, so the same file serves both the seller's eye and the writer.
REVIEW_EXTRA_COLUMNS = ["source_file", "concept", "evidence", "warnings"]
# review.csv: every push column (the digital downloads, FILES_COLUMN, among them: empty
# for a physical template), then the extras.
REVIEW_COLUMNS = [*LISTING_COLUMNS, *REVIEW_EXTRA_COLUMNS]

# A folder product of a digital template keeps the files a buyer downloads in a
# `dosyalar` / `files` subfolder (FILES_DIRS, from workspace); its photos stay in the
# folder itself. A loose design is its own download: the original file, not a render.
# Its listing photos are then always the mockups and a small preview
# (mockup.DIGITAL_PREVIEW_EDGE), never the design itself: an opaque printable is not a
# "finished photo" when it is the thing being sold.

# Written by Windows and macOS into folders a person opens; never a deliverable.
_SYSTEM_FILES = {"thumbs.db", "desktop.ini", "icon\r"}
# Folders an archiver or a synced folder leaves inside `dosyalar`; never the seller's.
_SYSTEM_DIRS = {"__macosx"}

# Etsy's title rule (createDraftListing in the Open API spec: letters, numbers,
# punctuation, math symbols, spaces, ™ © ®; %, :, & and + once each) lives in `listings`,
# where a plain CSV push is checked with it too; TITLE_SYMBOLS, TITLE_ONCE and
# title_problems are re-exported from here. A title built from a file name can break it
# ("salt & pepper & co.png", an emoji, a "$"), and Etsy would then refuse the draft at
# step 6, counting toward the stop after repeated failures; clean_title() mends it first.
_title_char_ok = title_char_ok


@dataclass
class DropRow:
    source: Path
    seed: seeds.Seed
    title: str = ""
    tags: list[str] = field(default_factory=list)
    description: str = ""
    images: list[Path] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: bool = False
    files: list[Path] = field(default_factory=list)  # a digital product's downloads
    stamped: list[Path] = field(default_factory=list)  # images carrying the watermark

    @property
    def ok(self) -> bool:
        return not self.skipped and bool(self.title) and bool(self.images)


@dataclass
class DropReport:
    batch: str
    out_dir: Path
    csv_path: Path | None = None
    rows: list[DropRow] = field(default_factory=list)
    concepts: int = 0
    researched: int = 0
    cached: int = 0
    # The shop's info images every row ends with (as sent: converted when they had to be).
    info_images: list[infoimages.InfoImage] = field(default_factory=list)

    @property
    def ready(self) -> list[DropRow]:
        return [r for r in self.rows if r.ok]

    @property
    def skipped(self) -> list[DropRow]:
        return [r for r in self.rows if not r.ok]

    @property
    def images_made(self) -> int:
        return sum(len(r.images) for r in self.rows)


def estimate_requests(
    products: int,
    concepts: int,
    images_per_product: int = 0,
    *,
    images: int | None = None,
    has_variations: bool = False,
    template_inventory: bool = False,
    files: int = 0,
) -> int:
    """What a run will cost against the daily allowance, before it starts.

    A Personal Access app gets 5,000 requests a day. Research pages at 100 listings
    each, then every product costs one create plus one upload per image — `images` in
    all when the caller knows each product's real count, else `images_per_product`
    each — plus one upload per download file of a digital product (`files` in all).
    A template with variations adds one inventory update per draft, and reading the
    template's inventory costs one request per run.
    """
    research_calls = concepts * 2  # a 200-listing sample is two pages of 100
    image_calls = images if images is not None else products * images_per_product
    write_calls = products + image_calls + files + (products if has_variations else 0)
    return research_calls + write_calls + (1 if template_inventory and products else 0)


# --- a digital product's downloads -----------------------------------------------------


def files_folder(product: Path) -> Path | None:
    """The `dosyalar` / `files` subfolder of a folder product, ignoring case; else None."""
    if not product.is_dir():
        return None
    try:
        entries = sorted(product.iterdir(), key=lambda p: p.name.casefold())
    except OSError:
        return None
    for wanted in FILES_DIRS:
        for entry in entries:
            if entry.name.casefold() == wanted and entry.is_dir() and not entry.is_symlink():
                return entry
    return None


def _natural(path: Path) -> list:
    return [int(part) if part.isdigit() else part
            for part in re.split(r"(\d+)", path.name.casefold())]


def _seller_file(name: str) -> bool:
    """Not a hidden, Office lock or system file."""
    return not name.startswith((".", "~$")) and name.casefold() not in _SYSTEM_FILES


def _seller_dir(name: str) -> bool:
    return not name.startswith(".") and name.casefold() not in _SYSTEM_DIRS


def deliverable_files(source: Path) -> list[Path]:
    """What a buyer downloads for this product, in upload order.

    A loose design: the design file itself, byte for byte (never the flat render or a
    mockup). A folder product: every file in its `dosyalar` / `files` subfolder, in
    natural name order (01, 02, ... 10), without hidden and system files. Files in a
    subfolder of it are not among them (Etsy takes files, not folders); see
    nested_folders, which deliverable_issue reports.
    """
    if source.is_file():
        return [source]
    folder = files_folder(source)
    if folder is None:
        return []
    try:
        entries = list(folder.iterdir())
    except OSError:
        return []
    files = [p for p in entries if p.is_file() and _seller_file(p.name)]
    return sorted(files, key=_natural)


def _holds_a_file(folder: Path) -> bool:
    """Whether a seller's file sits in `folder` or anywhere below it."""
    for _root, dirs, names in os.walk(folder):
        dirs[:] = [d for d in dirs if _seller_dir(d)]
        if any(_seller_file(name) for name in names):
            return True
    return False


def nested_folders(source: Path) -> list[str]:
    """The subfolders of a folder product's `dosyalar` that hold files, by name.

    An SVG bundle is often `dosyalar/license.pdf` + `dosyalar/SVG/*.svg` +
    `dosyalar/PNG/*.png`. An Etsy download is a file, never a folder, so what is in
    `SVG/` and `PNG/` would silently not reach the buyer: deliverable_issue refuses it.
    """
    folder = files_folder(source) if source.is_dir() else None
    if folder is None:
        return []
    try:
        entries = sorted(folder.iterdir(), key=lambda p: p.name.casefold())
    except OSError:
        return []
    return [
        entry.name for entry in entries
        if entry.is_dir() and not entry.is_symlink() and _seller_dir(entry.name)
        and _holds_a_file(entry)
    ]


def deliverable_issue(
    source: Path, files: Sequence[Path]
) -> tuple[str, str, dict[str, Any]] | None:
    """Why this product's downloads cannot go up: (code, message, params), or None.

    Codes: nested_files (its `dosyalar` holds subfolders with files: Etsy takes files,
    so they would not be sent), no_deliverable (no files subfolder, `missing` True, or
    one with nothing in it, `missing` False), too_many_files (Etsy takes
    client.MAX_LISTING_FILES), and each file's own client.file_issue code (file_type,
    file_too_large, file_empty, file_missing, file_unreadable).
    """
    nested = nested_folders(source)
    if nested:
        folder = files_folder(source)
        where = folder.name if folder is not None else FILES_DIRS[0]
        names = ", ".join(nested[:3]) + (", ..." if len(nested) > 3 else "")
        return (
            "nested_files",
            f"{source.name}/{where} has subfolders ({names}); Etsy downloads cannot hold "
            "folders, so zip each subfolder (or the whole set) into one file",
            {"name": source.name, "folder": where, "folders": names, "n": len(nested)},
        )
    if not files:
        folder = files_folder(source)
        where = (
            f"its {folder.name!r} folder is empty" if folder is not None
            else f"it has no {FILES_DIRS[0]!r} (or {FILES_DIRS[1]!r}) subfolder"
        )
        return (
            "no_deliverable",
            f"the template is a digital product, but {where}; put the files buyers "
            f"download in {source.name}/{FILES_DIRS[0]}/",
            {"name": source.name, "folder": FILES_DIRS[0], "missing": folder is None},
        )
    if len(files) > MAX_LISTING_FILES:
        return (
            "too_many_files",
            f"{source.name} has {len(files)} files to download and Etsy takes "
            f"{MAX_LISTING_FILES} per listing; nothing was dropped, zip them together",
            {"name": source.name, "n": len(files), "max": MAX_LISTING_FILES},
        )
    for path in files:
        issue = file_issue(path)
        if issue is not None:
            return issue
    return None


def deliverables(
    source: Path, *, made_to_order: bool = False
) -> tuple[list[Path], tuple[str, str, dict[str, Any]] | None]:
    """(the product's downloads, why they cannot go up or None).

    `made_to_order` (the template's when_made): Etsy activates a made-to-order digital
    listing without a file (updateListing in the Open API spec), because the seller sends
    what was made after the order. A folder's `dosyalar` files still go up when there
    are some; a folder without them is fine; and a loose design is NOT attached: for a
    made-to-order product it is a sample, not what the buyer ordered.
    """
    if made_to_order and source.is_file():
        return [], None
    files = deliverable_files(source)
    issue = deliverable_issue(source, files)
    if made_to_order and issue is not None and issue[0] == "no_deliverable":
        issue = None
    return files, issue


def made_to_order(template: Template) -> bool:
    """The template's when_made is made_to_order (its downloads are optional)."""
    return str(template.fields.get("when_made") or "") == "made_to_order"


MADE_TO_ORDER_NOTE = (
    "the template is made to order, so no download file is attached; add the buyer's "
    "file in Etsy once it is made"
)


def photo_is_download(
    images: Sequence[Path], files: Sequence[Path]
) -> tuple[str, str, dict[str, Any]] | None:
    """A listing photo that is one of the files being sold: (code, message, params).

    Etsy shows a listing photo to anyone at up to 3000 px a side, so a download that is
    also a photo is free for the taking. The pipeline never makes one; this is the guard.
    """
    downloads = {os.path.normcase(os.path.abspath(str(p))) for p in files}
    for image in images:
        if os.path.normcase(os.path.abspath(str(image))) in downloads:
            return (
                "download_is_photo",
                f"{image.name} is both a listing photo and the file buyers download; "
                "anyone could save it from the listing page",
                {"name": image.name},
            )
    return None


def no_photos_message(source: Path) -> str:
    """Why a product folder with downloads but no photos cannot become a listing."""
    return (
        f"the product folder {source.name!r} has no photos, only its "
        f"{FILES_DIRS[0]!r} files; every listing needs at least one photo: put the "
        f"product's images (01, 02, ...) in {source.name}/ itself"
    )


# --- what Etsy accepts ---------------------------------------------------------------


def clean_title(title: str) -> str:
    """The title with what Etsy refuses taken out, or spelled out; "" if nothing is left.

    Disallowed characters (emoji, "$", "°") are dropped; a second "&" becomes "and",
    a second "+" "plus", a second "%" "percent" and a second ":" a dash. Plain titles
    come back unchanged.
    """
    text = unicodedata.normalize("NFC", str(title or ""))
    text = "".join(ch if _title_char_ok(ch) else (" " if ch.isspace() else "") for ch in text)
    for ch, word in TITLE_ONCE.items():
        first = text.find(ch)
        if first >= 0 and text.count(ch) > 1:
            text = text[: first + 1] + text[first + 1:].replace(ch, f" {word} ")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.;!?])", r"\1", text)
    text = re.sub(r"(?:,\s*){2,}", ", ", text)
    text = text.strip(" ,-|/")
    if len(text) > MAX_TITLE_LEN:
        cut = text.rfind(", ", 0, MAX_TITLE_LEN + 1)
        text = (text[:cut] if cut > 0 else text[:MAX_TITLE_LEN]).rstrip(" ,-|/")
    return text


def image_size(path: Path) -> tuple[int, int] | None:
    """(width, height) from the file's header, without decoding it; None if unreadable.

    Pillow refuses to even open an image of more than about 179M pixels; that one's
    size is estimated from its message, so it still reads as too large.
    """
    try:
        with Image.open(path) as image:
            return image.size
    except Image.DecompressionBombError as exc:
        return catalog.bomb_size(exc)
    except (OSError, ValueError, SyntaxError):
        return None


def oversized(paths: Sequence[Path]) -> tuple[Path, tuple[int, int]] | None:
    """The first image over the pixel limits (catalog.too_many_pixels), with its size.

    A design this large would need gigabytes to composite (MemoryError); it is refused
    as that product's problem, before any work.
    """
    for path in paths:
        size = image_size(path)
        if size is not None and catalog.too_many_pixels(size):
            return path, size
    return None


def fit_for_etsy(image: Path, out_dir: Path, *, limit: int | None = None) -> Path:
    """A JPEG copy under Etsy's 20 MB image limit when `image` is over it; else `image`.

    Finished photos go up byte for byte, so a 30 MB export would fail the check step on
    every run. The copy is made smaller in quality first, then in size, and lands in the
    batch folder beside the other outputs.
    """
    if limit is None:
        limit = MAX_IMAGE_BYTES
    try:
        if image.stat().st_size <= limit:
            return image
    except OSError:
        return image
    try:
        with Image.open(image) as raw:
            picture = mockup._as_displayed(raw)
            if picture.mode in ("RGBA", "LA", "PA") or (
                picture.mode == "P" and "transparency" in picture.info
            ):
                rgba = picture.convert("RGBA")
                picture = Image.new("RGB", rgba.size, (255, 255, 255))
                picture.paste(rgba, (0, 0), rgba)
            else:
                picture = picture.convert("RGB")
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ValidationError(f"Cannot open {image.name}: {exc}") from exc
    out = out_dir / f"{image.stem}-{image.suffix.lstrip('.').lower()}-etsy.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    quality = 90
    for _attempt in range(10):
        picture.save(out, "JPEG", quality=quality, optimize=True)
        if out.stat().st_size <= limit:
            return out
        if quality > 80:
            quality = 80
        else:
            width, height = picture.size
            picture = picture.resize(
                (max(1, round(width * 0.8)), max(1, round(height * 0.8))), Image.LANCZOS
            )
    raise ValidationError(f"{image.name} could not be made smaller than Etsy's 20MB limit.")


def ready_info_images(
    images: Sequence[infoimages.InfoImage], out_dir: Path
) -> tuple[list[infoimages.InfoImage], list[str]]:
    """(the shop's info images as Etsy takes them, what was done to them).

    Each is copied into `out_dir`/info-images first, once per run: the batch then keeps
    what it sent (review.csv points at the copies), and a picture taken out or replaced
    on Şablon İlan while the run is going changes nothing in it. The app stores them
    ready (JPG, PNG or GIF, at most 20 MB); one put in info-images/ by hand may be
    neither: its copy is converted the same way a ready photo is, and said so. Raises
    ValidationError for one that cannot be read at all: every draft would carry it, so
    the run stops before anything is sent.
    """
    ready: list[infoimages.InfoImage] = []
    notes: list[str] = []
    convert_dir = out_dir / INFO_DIR
    for image in images:
        try:
            convert_dir.mkdir(parents=True, exist_ok=True)
            copy = convert_dir / image.name
            shutil.copyfile(image.path, copy)
            converted = mockup.to_uploadable(copy, convert_dir)
            fitted = fit_for_etsy(converted, convert_dir)
            # A real decode, once per run: every draft carries it, so a file cut short
            # must stop the run here, not fail every upload after its draft exists.
            with Image.open(fitted) as opened:
                opened.load()
        except Exception as exc:  # noqa: BLE001 — named in the message, the run stops
            raise ValidationError(
                f"The info image {image.name} cannot be used ({exc}). Replace or remove it "
                "(Template listing page, or the info-images folder)."
            ) from exc
        if fitted != copy:
            notes.append(f"info image {image.name} was converted to {fitted.name} for Etsy")
        ready.append(infoimages.InfoImage(
            name=image.name, path=fitted, alt=image.alt, listing_id=image.listing_id,
            listing_image_id=image.listing_image_id,
        ))
    alts = {_relative(i.path, out_dir): i.alt for i in ready if i.alt}
    if alts:
        try:
            (out_dir / INFO_ALTS_FILE).write_text(
                json.dumps(alts, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:  # only `listings push` reads it later; the run itself does not
            log.warning("could not write %s", out_dir / INFO_ALTS_FILE)
    return ready, notes


def batch_upload_notes(batch_dir: Path) -> tuple[Path, dict[str, str]]:
    """(the batch's folder of watermarked copies, the info images' alt texts by resolved
    path): what `etsyotomasyon listings push` of a drop batch's review.csv tells Etsy."""
    alts: dict[str, str] = {}
    try:
        raw = json.loads((batch_dir / INFO_ALTS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if isinstance(raw, dict):
        for rel, alt in raw.items():
            if isinstance(rel, str) and isinstance(alt, str) and alt.strip():
                try:
                    alts[str((batch_dir / rel).resolve())] = alt
                except OSError:
                    continue
    return batch_dir / watermark_mod.STAMPED_DIR, alts


def info_note(appended: int, total: int) -> str:
    """The warning of a product that got only some of the shop's info images."""
    return (
        f"only {appended} of the {total} info images fit after this product's own photos "
        f"(Etsy takes {MAX_LISTING_IMAGES} pictures per listing); the first ones were added"
    )


def _batch_name() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S-%f")


def _output_name(source: Path, template_image: Path | None, taken: set[str]) -> str:
    """A composite filename that cannot quietly land on another product's.

    Built from `Path.stem` alone, `mug.png` and `mug.jpg` — or, on Windows, `Mug.png`
    and `mug.png` — would produce one path. The second compose() call would overwrite
    the first, and with both rows pointing at that same path the report, the CSV and
    the image-existence check would all agree nothing was wrong while two listings
    shipped the same pictures. Readable names are kept for the ordinary case and only
    a real collision gets a suffix.
    """
    base = f"{source.stem}--{template_image.stem if template_image is not None else 'flat'}"
    candidate = f"{base}.jpg"
    attempt = 1
    while candidate.casefold() in taken:
        attempt += 1
        candidate = f"{base}-{attempt}.jpg"
    taken.add(candidate.casefold())
    return candidate


def _research_concept(
    client: EtsyClient | None, concept: str, *, sample: int, use_cache: bool
) -> tuple[MarketReport | None, bool]:
    """Returns (report, came_from_cache).

    `concept` is the search as Etsy gets it (generate.research_keyword: the concept, plus
    the template's product when the concept does not name it). The cache key is that
    search and the sample size, `"<search>|<sample>"`, the same key the SEO page uses.
    """
    concept = " ".join(str(concept).split()).lower()
    key = f"{concept}|{sample}"
    if use_cache:
        cached = cache.load(key)
        if cached is not None:
            return MarketReport(**cached), True
    if client is None:
        return None, False
    report = research(client, concept, sample=sample)
    if use_cache:
        cache.store(key, report.__dict__)
    return report, False


def run(
    workspace: Workspace,
    template: Template,
    *,
    client: EtsyClient | None = None,
    mockups_per_product: int = 5,
    include_flat: bool = True,
    sample: int = 200,
    use_cache: bool = True,
    on_progress: Callable[[str], None] | None = None,
    exclude_products: set[str] | None = None,
    mockups: Sequence[Path] | None = None,
    watermark: bool = True,
    info_images: Sequence[infoimages.InfoImage] | None = None,
    avoid_tags: Sequence[Sequence[str]] = (),
) -> DropReport:
    """Composite, research, write copy, and emit review.csv. Nothing is sent to Etsy.

    `mockups` are the templates to composite onto, first (the main image) to last —
    normally `catalog.run_mockups(workspace, template.listing_type)`, the Mockuplar
    page's selection and order. Without it the first `mockups_per_product` files of
    1-MOCKUPS are used.

    `watermark`: stamp the workspace's watermark (drop.watermark) on a copy of every
    listing photo when it is on and takes this template; False never stamps. The copies
    go to the batch's `watermarked` folder, one name each; originals and download files
    are never changed. A product whose photo cannot take the mark is skipped, never sent
    bare, and a mark that is on but cannot be read stops the run (ValidationError).

    `info_images` end every row's images, after its own (default: the workspace's,
    drop.infoimages); a product folder gets the ones that fit (`infoimages.fitting`).
    They are never watermarked.

    `avoid_tags` are the tags of drafts made before this run (the upload history's,
    `automation.used_tags`); each row also avoids the tags of the rows before it, so a
    batch of designs does not end up with one set of thirteen tags (`generate.build_tags`).
    """
    workspace.require()
    # download / both: every row also carries the files a buyer downloads.
    digital = (template.fields.get("type") or "physical") in DIGITAL_TYPES
    to_order = made_to_order(template)

    def say(message: str) -> None:
        if on_progress:
            on_progress(message)

    groups = [
        (path, images) for path, images in workspace.product_groups()
        if path.name.casefold() not in {name.casefold() for name in exclude_products or ()}
    ]
    report = DropReport(batch=_batch_name(), out_dir=workspace.drafts / _batch_name())
    report.out_dir = workspace.drafts / report.batch

    if not groups:
        return report
    mark = (watermark_mod.for_run(workspace, template.fields.get("type") or "physical")
            if watermark else None)

    available = workspace.mockup_files()
    if mockups is not None:
        # The seller chose these (and their order) on the Mockuplar page; the ones left
        # out were switched off there, which the caller reports once, not per row.
        chosen = list(mockups)
        unused = 0
    else:
        chosen = available[:mockups_per_product]
        # A mockup left out by --mockups is never silently dropped: the row says so.
        unused = len(available) - len(chosen)
    mockups = chosen

    if info_images is None:
        info_images = infoimages.load(workspace)
    info = list(info_images)

    # A composited row carries one image per mockup plus the flat render plus the shop's
    # info images, and Etsy takes twenty per listing. Counted against the mockups that
    # actually exist rather than the number asked for, so `--mockups 50` on a workspace
    # holding three is still a fine run. Refused here, before a single image is
    # composited or a single research call is spent, because the alternative is a whole
    # batch of review rows that `listings push` is then obliged to reject one by one.
    planned = len(mockups) + (1 if include_flat else 0) + len(info)
    if planned > MAX_LISTING_IMAGES:
        allowed = infoimages.mockup_cap(len(info), include_flat=include_flat)
        flat_note = " plus the flat render" if include_flat else ""
        info_note_text = f" plus {len(info)} info image(s)" if info else ""
        remedy = (
            f"Use --mockups {allowed} or fewer" + (", or --no-flat." if include_flat else ".")
            if unused or mockups_per_product < len(available)
            else f"Turn some mockups off on the Mockups page (at most {allowed})."
        )
        raise ValidationError(
            f"{len(mockups)} mockup(s){flat_note}{info_note_text} is {planned} images per "
            f"listing, and Etsy allows {MAX_LISTING_IMAGES}. {remedy}"
        )
    info, info_notes = ready_info_images(info, report.out_dir)
    report.info_images = info
    for note in info_notes:
        say(note)

    # One calibration covers every mockup of the same size — that is the point of
    # storing fractions. A mockup with no entry of its own borrows the area of a
    # calibrated one with identical dimensions before falling back to the default:
    # catalog.effective_areas, the rule the app's runs and the Mockuplar page use too.
    areas = catalog.effective_areas(workspace)
    try:
        mockup_facts = catalog.load(workspace)
    except (OSError, ValueError):
        mockup_facts = {}

    def area_for(template_image: Path) -> mockup.PrintArea:
        return areas.get(template_image.name, (mockup.DEFAULT_PRINT_AREA, ""))[0]

    # The folder name is a useful fallback for `2-PRODUCTS/mountain sunset/IMG_01.png`,
    # but never for a file sitting directly in 2-PRODUCTS — that would turn the
    # workspace's own structural folder into a product concept.
    rows = [
        DropRow(
            source=path,
            seed=seeds.derive(
                path / "IMG_0001.jpg" if path.is_dir() else path,
                folder_fallback=path.is_dir() or path.parent != workspace.products,
            ),
            images=images,
        )
        for path, images in groups
    ]
    # A product folder with only its downloads (a `dosyalar` folder, no photos) is
    # reported below and needs no research.
    grouped = seeds.group([r.seed for r in rows if r.images or not r.source.is_dir()])
    report.concepts = len(grouped)
    # What the template says the product is (generate.hint_from): the market search
    # names it too, so "dog dad paw print" on a shirt template searches shirts.
    hint = generate.hint_from(template.source_title, template.tags, template.description,
                              template.materials)
    # The tags of every draft before this one, the history's first, then this run's.
    earlier_tags: list[list[str]] = [list(tags) for tags in avoid_tags]

    # Output names are handed out from one set per batch, so a collision between two
    # products is resolved rather than discovered later as a missing image.
    taken: set[str] = set()
    stamped_names: set[str] = set()  # the batch's stamped copies (watermark_mod.STAMPED_DIR)

    # One lookup per distinct concept, not per file. Eighteen concepts across a
    # hundred products is eighteen searches.
    reports: dict[str, MarketReport | None] = {}
    for concept, found in grouped.items():
        keyword = generate.research_keyword(found[0], hint)
        market, from_cache = _research_concept(client, keyword, sample=sample, use_cache=use_cache)
        reports[concept] = market
        if from_cache:
            report.cached += 1
        elif market is not None:
            report.researched += 1
        say(f"researched {keyword!r}" + (" (cached)" if from_cache else ""))

    for row in rows:
        if row.source.is_dir() and not row.images:
            row.skipped = True
            row.warnings.append(no_photos_message(row.source))
            say(f"skipped {row.source.name}")
            continue
        if len(row.images) > MAX_LISTING_IMAGES:
            row.skipped = True
            row.warnings.append(
                f"product folder has more than {MAX_LISTING_IMAGES} images; "
                "nothing was truncated"
            )
            continue
        if not row.seed:
            row.skipped = True
            row.warnings.append(row.seed.reason or "no concept could be read from the filename")
            say(f"skipped {row.source.name}")
            continue
        huge = oversized(row.images if row.source.is_dir() else [row.source])
        if huge is not None:
            (path, (width, height)) = huge
            row.skipped = True
            row.warnings.append(
                f"{path.name} is {width}x{height} px; images can be at most "
                f"{catalog.MAX_EDGE} px on a side and {catalog.MAX_PIXELS // 1_000_000} "
                "million pixels — make it smaller"
            )
            say(f"skipped {row.source.name}")
            continue
        if digital:
            # What the buyer downloads: the original design, or the folder's `dosyalar`.
            # A product without a download it can send is skipped here, before any work.
            files, issue = deliverables(row.source, made_to_order=to_order)
            if issue is not None:
                row.skipped = True
                row.warnings.append(issue[1])
                say(f"skipped {row.source.name}")
                continue
            row.files = files

        copy = generate.generate(
            row.seed,
            reports.get(row.seed.text),
            template_description=template.description,
            fallback_tags=template.tags,
            template_title=template.source_title,
            # The seller's description template, when saved (drop.description).
            description_template=template.description_template,
            product_words=template.category_path,
            template_materials=template.materials,
            taken_tags=earlier_tags,
        )
        row.title, row.tags = clean_title(copy.title), copy.tags
        earlier_tags.append(list(copy.tags))
        row.description = copy.description
        row.evidence, row.warnings = copy.sources, list(copy.warnings)
        if digital and to_order and not row.files:
            row.warnings.append(MADE_TO_ORDER_NOTE)
        if row.title != copy.title:
            row.warnings.append("characters Etsy does not accept were taken out of the title")
        if not row.title:
            row.skipped = True
            row.warnings.append("no title Etsy accepts could be made from the file name")
            continue

        # Only the routes that redraw pixels turn a photo upright (see the EXIF note on
        # mockup.ORIENTATION_TAG). A ready photo is uploaded byte for byte, and Etsy
        # honours its orientation tag exactly as Explorer does, so rewriting it here
        # would spend a JPEG generation — and any transparency — to reach the same result.
        if row.source.is_dir():
            # Explicit ready-photo input: never composite or flatten these files. A
            # transparent file in here is still uploaded as it is, which is a listing
            # image with a black hole in it once Etsy flattens it — so say so rather
            # than quietly shipping it.
            bare = [image.name for image in row.images if mockup.looks_like_artwork(image)]
            if bare:
                row.warnings.append(
                    f"{len(bare)} transparent file(s) in this folder are uploaded as they "
                    f"are, not composited onto a mockup ({', '.join(bare[:3])}). Move them "
                    f"to 2-PRODUCTS as loose designs if they are artwork."
                )
        elif not digital and not mockup.looks_like_artwork(row.source):
            # A finished product photo needs no compositing; use it as it is. Never for a
            # digital template: there the loose file is the download being sold (an
            # opaque printable too), so it goes onto the mockups and never up as it is.
            row.images = [row.source]
        else:
            if unused:
                row.warnings.append(
                    f"{unused} mockup(s) in 1-MOCKUPS were not used (--mockups "
                    f"{mockups_per_product}); raise it to include them, up to Etsy's "
                    f"{MAX_LISTING_IMAGES} images per listing"
                )
            for template_image in mockups:
                area = area_for(template_image)
                out = report.out_dir / _output_name(row.source, template_image, taken)
                try:
                    row.images.append(mockup.compose(
                        row.source, template_image, out, area=area,
                        kind=catalog.kind_of(mockup_facts, template_image.name),
                    ))
                except Exception as exc:  # noqa: BLE001 — one bad file must not stop a batch
                    row.warnings.append(f"mockup {template_image.name} failed: {exc}")
            if include_flat:
                flat = report.out_dir / _output_name(row.source, None, taken)
                # A digital design's flat render is a preview, not a copy of the download.
                edge = mockup.DIGITAL_PREVIEW_EDGE if digital else mockup.OUTPUT_MIN_EDGE
                try:
                    row.images.append(mockup.flatten_design(row.source, flat, edge=edge))
                except Exception as exc:  # noqa: BLE001
                    row.warnings.append(f"flat render failed: {exc}")

        # Ready photos go to Etsy unchanged, so a format it refuses has to be handled
        # here rather than discovered after the draft exists. Composited images are
        # already JPEG, so for them every call below returns the path untouched. One
        # folder per product, because two products can both hold `01-front.webp`.
        convert_dir = report.out_dir / (
            row.source.name if row.source.is_dir() else row.source.stem
        )
        # The batch's one folder of stamped copies, each name taken once (stamped_names).
        stamped_dir = report.out_dir / watermark_mod.STAMPED_DIR
        unstamped: list[Path] = []
        unmarked: str | None = None
        uploadable: list[Path] = []
        for image in row.images:
            try:
                converted = mockup.to_uploadable(image, convert_dir)
            except Exception as exc:  # noqa: BLE001 — one bad file must not stop a batch
                row.warnings.append(f"{image.name} could not be converted for Etsy: {exc}")
                continue
            if converted != image:
                row.warnings.append(
                    f"{image.name} was converted to {converted.name}: Etsy accepts only "
                    "JPG, PNG and GIF listing images"
                )
            fit_dir = convert_dir
            if mark is not None:
                # A stamped copy; the picture itself stays as it is. A photo the mark
                # cannot be put on stops this product: it never goes up without it.
                try:
                    stamped = mark.stamp(converted, stamped_dir, stamped_names)
                except Exception as exc:  # noqa: BLE001
                    unmarked = f"the watermark could not be put on {image.name}: {exc}"
                    break
                unstamped.append(converted)
                converted, fit_dir = stamped, stamped_dir
            try:
                fitted = fit_for_etsy(converted, fit_dir)
            except Exception as exc:  # noqa: BLE001
                row.warnings.append(f"{converted.name} could not be made smaller for Etsy: {exc}")
                continue
            if fitted != converted:
                row.warnings.append(
                    f"{converted.name} was over Etsy's 20MB image limit; made smaller as "
                    f"{fitted.name}"
                )
            if mark is not None:
                row.stamped.append(fitted)
            uploadable.append(fitted)
        if unmarked is not None:
            row.skipped = True
            row.warnings.append(unmarked)
            say(f"skipped {row.source.name}")
            continue
        row.images = uploadable

        # The shop's info images close every listing, after its own pictures (never
        # watermarked); a folder whose photos leave too little room gets the first ones
        # that fit, and says so.
        if info and row.images:
            extra = infoimages.fitting(len(row.images), info)
            if len(extra) < len(info):
                row.warnings.append(info_note(len(extra), len(info)))
            row.images.extend(image.path for image in extra)

        # The pictures a watermarked copy was made from are checked too.
        clash = photo_is_download([*row.images, *unstamped], row.files) if row.files else None
        if clash is not None:
            row.skipped = True
            row.warnings.append(clash[1])
            say(f"skipped {row.source.name}")
            continue
        if not row.images:
            row.skipped = True
            row.warnings.append(
                "no images produced — put at least one mockup in 1-MOCKUPS, "
                "or drop a finished product photo instead of transparent artwork"
            )
        say(f"prepared {row.source.name}")

    report.rows = rows
    if report.ready:
        report.csv_path = report.out_dir / REVIEW_FILE
        csvio.write_rows(
            report.csv_path,
            [_to_csv_row(r, template, report.csv_path.parent) for r in report.ready],
            columns=REVIEW_COLUMNS,
        )
    return report


def _to_csv_row(row: DropRow, template: Template, base: Path) -> dict[str, Any]:
    """One row in exactly the shape `etsyotomasyon listings push` consumes.

    `listing_id` is left empty, always. That is what makes this pipeline structurally
    incapable of updating or publishing an existing listing: Etsy only accepts a state
    change on an update, and there is never an id here to update.
    """
    data: dict[str, Any] = dict(template.fields)
    data.update(
        {
            "listing_id": "",
            "title": row.title,
            "description": row.description,
            "tags": row.tags,
            "materials": template.materials,
            "state": "",
            "images": [_relative(p, base) for p in row.images],
            FILES_COLUMN: [_relative(p, base) for p in row.files],
            "source_file": row.source.name,
            "concept": row.seed.text,
            "evidence": "; ".join(row.evidence),
            "warnings": "; ".join(row.warnings),
        }
    )
    return data


def _relative(path: Path, base: Path) -> str:
    """push() resolves image paths against the CSV's own folder.

    Ready photos live in 2-PRODUCTS, outside the batch folder, so a plain
    relative_to() fails for them, and an absolute path breaks the moment the
    workspace moves or syncs to another machine. `..` segments keep them
    relative; forward slashes keep the file readable on every platform. Only a path
    on another drive, which cannot be relative, stays absolute.
    """
    try:
        return Path(os.path.relpath(path, base)).as_posix()
    except ValueError:
        return path.as_posix()
