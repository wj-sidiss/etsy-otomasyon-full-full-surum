"""Tasarım Yükle: design uploads, the pending list, and the draft run.

    PUT    /api/designs/files?path=<name | folder/name>[&batch=<id>]   raw image body
    PUT    /api/designs/files?path=<folder>/dosyalar/<name>[&batch=<id>]
           a digital product's download file (any type but programs), raw body
    DELETE /api/designs/files?path=<name | folder | folder/name>       -> moved to archive/
    GET    /api/designs/pending        what the next run would do, and what blocks it
    GET    /api/designs/sections[?refresh=1]
                                       the shop's sections for the start card's "Mağaza
                                       bölümü" select, and what it starts on
    POST   /api/designs/start          {"dry_run"?: bool, "opaque"?: "as_is" | "place",
                                        "section"?: "template" | "none" | <section id>}
                                       -> job "designs" (drop.stream)
    GET    /api/designs/last           the last finished run (3-DRAFTS/last-run.json)
    POST   /api/designs/review/forget  {"name", "confirm": true}: try a product again
    POST   /api/designs/unlock         {"confirm": true}: remove a crashed run's lock

A run creates DRAFTS only; nothing is published. The confirm modal in the page
("Başlat") is the consent for creating them.

A digital template (type download or both) makes digital drafts: each product's
download files go up after its images — a loose design's original file, a folder
product's `dosyalar` / `files` subfolder (drop.pipeline.deliverables). The pending view
says per product what would be attached and what is wrong with it.

A download-only template's drafts show what the buyer gets: of the switched-on mockups
only those that are no physical product (posters, canvases, frames, "other") are used,
plus the flat preview; the pending view names them and the ones left out.

The pending view also says, for the start card:
- per loose design its background (`ground`: transparent | flat | photo, None while not
  read yet): an opaque one goes up as it is unless the seller asks (start's `opaque`) to
  place those on a solid background onto the mockups (drop.stream);
- the template's product as a mockup type (`template.product`) and the main mockup's
  (`mockups.main_type`), so a mug template with a T-shirt main image is pointed out;
- the workspace's watermark (`watermark`: drop.watermark.describe for the template's
  type): whether the run stamps it, and where. A mark that is on but cannot be read
  blocks the start ("watermark"): its photos must never go up without it.

The shop section (the start card's "Mağaza bölümü"): every draft of a run goes into the
section chosen there, whatever the template listing's is. `section` on start:
- "template": the template listing's section, as always. When Etsy no longer has it (the
  seller deleted it), the drafts go into no section instead of failing one by one.
- "none": no section.
- a section id: that section, checked against the shop's sections first; one that is gone
  is 409 section_gone (the page reads the list again).
Left out, the run is what it was before there was a choice (the template's, unchecked).
A real run remembers the choice for the open shop (shop pref `drop_section`); the card
starts on it next time, unless its section is gone. A check (dry run) ignores it.
"""

from __future__ import annotations

import collections
import io
import json
import logging
import os
import re
import shutil
import threading
import time
import unicodedata
import weakref
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...errors import EtsyotomasyonError
from ..router import ApiError, Request

if TYPE_CHECKING:  # pragma: no cover
    from ..context import AppContext
    from ..jobs import Job
    from ..router import Router

log = logging.getLogger("etsyotomasyon.web")

KIND = "designs"
CONCURRENCY = 3
MAX_UPLOAD = 50 * 1024 * 1024
LAST_RUN_FILE = "last-run.json"
PRODUCTS_PREFIX = "2-PRODUCTS"
# The status states that stop a real run before it starts (a dry run needs none of them).
_STATE_BLOCKERS = {
    "keys": "keys",
    "bad_keys": "bad_keys",
    "disconnected": "connect",
    "reconnect": "reconnect",
    "offline": "offline",
}
_ETSY_BLOCKERS = set(_STATE_BLOCKERS.values())
# At most this often the whole job state is copied for GET /api/jobs/{id}.
STATE_INTERVAL = 0.5
# How long one pending view may spend reading new designs' backgrounds (the rest are
# read by the next view; every answer is kept per file version).
GROUND_BUDGET = 2.0
_grounds_lock = threading.Lock()
_grounds: dict[tuple[str, int, int], str] = {}  # (path, mtime_ns, size) -> ground
_KEEP_GROUNDS = 5000

# Checking "is a run active?" and starting one happen together, so a double click
# cannot queue a second run behind the first.
_start_lock = threading.Lock()
_claims_lock = threading.Lock()
# (products folder, upload batch, folder name) -> the folder that batch writes into.
_claims: collections.OrderedDict[tuple[str, str, str], str] = collections.OrderedDict()
_KEEP_CLAIMS = 2000

# The start card's shop section: the choice ("template" | "none" | a section id), the
# shop pref it is remembered in, and how long the shop's sections are kept in memory
# (per Etsy client, so per shop and sign-in; a start always reads them afresh).
SECTION_TEMPLATE = "template"
SECTION_NONE = "none"
SECTION_PREF = "drop_section"
SECTIONS_TTL = 60.0
_sections_lock = threading.Lock()
_sections_cache: weakref.WeakKeyDictionary[Any, tuple[float, list[dict[str, Any]]]] = (
    weakref.WeakKeyDictionary()
)


def register(r: Router, ctx: AppContext) -> None:
    r.put("/api/designs/files", upload_file)
    r.delete("/api/designs/files", delete_file)
    r.get("/api/designs/pending", pending)
    r.get("/api/designs/sections", sections)
    r.post("/api/designs/start", start)
    r.get("/api/designs/last", last_run)
    r.post("/api/designs/review/forget", forget)
    r.post("/api/designs/unlock", unlock)


def _ctx(req: Request) -> AppContext:
    assert req.ctx is not None
    return req.ctx


def _active_job(ctx: AppContext) -> Job | None:
    jobs = ctx.jobs.list(kind=KIND, active=True)
    return jobs[0] if jobs else None


def _rel(ws: Any, path: Path) -> str:
    try:
        return path.resolve().relative_to(ws.root.resolve()).as_posix()
    except (OSError, ValueError):
        return ""


# --- file names -------------------------------------------------------------------------

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(10)),
             *(f"lpt{i}" for i in range(10))}


def _safe_segment(text: str, default: str) -> str:
    """One file or folder name that is safe on Windows and macOS."""
    text = unicodedata.normalize("NFC", str(text or ""))
    text = _UNSAFE.sub("", text).strip().strip(".").strip()
    if len(text) > 120:
        stem, dot, suffix = text.rpartition(".")
        text = f"{stem[:110].rstrip()}.{suffix}" if dot and len(suffix) <= 5 else text[:120]
    if not text:
        text = default
    if Path(text).stem.lower() in _RESERVED:
        text = f"{default}-{text}"
    return text


def _is_files_dir(name: str) -> bool:
    from ...drop.pipeline import FILES_DIRS

    return name.casefold() in FILES_DIRS


def _split_path(raw: str) -> list[str]:
    """[name], [folder, name] or [folder, "dosyalar" | "files", name] (a download file)."""
    parts = [p for p in re.split(r"[\\/]+", raw or "") if p not in ("", ".")]
    three = len(parts) == 3 and _is_files_dir(parts[1])
    if not parts or (len(parts) > 2 and not three) or any(p == ".." for p in parts):
        raise ApiError(422, "invalid", "path must be <name>, <folder>/<name> or "
                       "<folder>/dosyalar/<name>", field="path")
    if any(p.startswith(".") for p in parts):
        raise ApiError(422, "invalid", "hidden files are not designs", field="path")
    return parts


def _entry_ci(folder: Path, name: str) -> Path | None:
    """The entry of `folder` called `name`, ignoring case (as Windows and macOS do)."""
    wanted = name.casefold()
    try:
        for entry in folder.iterdir():
            if entry.name.casefold() == wanted:
                return entry
    except OSError:
        return None
    return None


def _same_file(folder: Path, name: str, data: bytes) -> str | None:
    """The name of a file in `folder` holding exactly `data` (the same name tried first)."""
    first = _entry_ci(folder, name)
    candidates = [first] if first is not None else []
    try:
        candidates += sorted(entry for entry in folder.iterdir() if entry != first)
    except OSError:
        pass
    for entry in candidates:
        try:
            if (entry.is_file() and entry.stat().st_size == len(data)
                    and entry.read_bytes() == data):
                return entry.name
        except OSError:
            continue
    return None


def _claim_folder(products: Path, folder: str, batch: str, name: str, data: bytes,
                  sub: str | None = None) -> str:
    """The product folder an upload batch writes `folder` into.

    Within one batch every file of a dropped folder lands in the same place. When a
    folder of that name exists from before, the batch's first file decides:
    - it is byte for byte a photo already in that folder: the same product dropped
      again (or an interrupted upload being finished), so it is that folder — never a
      `-2` copy that would become a second, identical draft;
    - otherwise it is a new product with the same folder name, and it gets a new name
      (`-2`), so it is never merged into an old one the history would skip.
    A download file (`sub` is its `dosyalar` folder) is compared with that subfolder.
    A folder named like one of the workspace's own (1-MOCKUPS, 2-PRODUCTS, 3-DRAFTS) is
    refused: it would be saved and then never listed (Workspace.product_groups skips it).
    """
    from ...drop.workspace import SUBFOLDER_NAMES

    if folder.casefold() in {name.casefold() for name in SUBFOLDER_NAMES}:
        raise ApiError(422, "reserved_folder",
                       f"A product folder cannot be named {folder}; rename it, or upload "
                       "its images as separate designs.", name=folder)
    key = (str(products), batch, folder.casefold())
    with _claims_lock:
        if batch and key in _claims:
            return _claims[key]
        existing = _entry_ci(products, folder) if batch else None
        inside = existing if existing is None or sub is None else _entry_ci(existing, sub)
        if (existing is not None and existing.is_dir() and inside is not None
                and inside.is_dir() and _same_file(inside, name, data)):
            claimed = existing.name
        else:
            claimed, number = folder, 1
            while batch and _entry_ci(products, claimed) is not None:
                number += 1
                claimed = f"{folder}-{number}"
            (products / claimed).mkdir(parents=True, exist_ok=True)
        if batch:
            _claims[key] = claimed
            while len(_claims) > _KEEP_CLAIMS:
                _claims.popitem(last=False)
        return claimed


def _save_unique(folder: Path, name: str, data: bytes) -> tuple[str, bool]:
    """Save without overwriting: `x.png`, then `x-2.png`, ... (name, was_already_there).

    The same bytes under the same name are the same design dropped twice: nothing new
    is written, so one design never becomes two listings.
    """
    folder.mkdir(parents=True, exist_ok=True)
    stem, suffix = Path(name).stem, Path(name).suffix
    number = 1
    while True:
        candidate = name if number == 1 else f"{stem}-{number}{suffix}"
        existing = _entry_ci(folder, candidate)
        if existing is not None:
            try:
                same = (existing.is_file() and existing.stat().st_size == len(data)
                        and existing.read_bytes() == data)
            except OSError:
                same = False
            if same:
                return existing.name, True
            number += 1
            continue
        try:
            with (folder / candidate).open("xb") as handle:
                handle.write(data)
        except FileExistsError:
            number += 1
            continue
        return candidate, False


def _decode_all(image: Any) -> None:
    """Read the whole picture, so a file cut short by a half-finished copy is refused now.

    Image.verify() checks a PNG's chunks but does nothing for a JPEG, which then passed
    the upload, showed as ready and failed the check step on every run. A JPEG is
    decoded at 1/8 scale: every byte is still read, at a fraction of the memory.
    """
    if image.format == "PNG":
        image.verify()
        return
    if image.format == "JPEG":
        image.draft("RGB", (max(1, image.width // 8), max(1, image.height // 8)))
    image.load()


# --- history ---------------------------------------------------------------------------------


def _shop_key(ctx: AppContext) -> str | None:
    shop = (ctx.status.get("shop") or {}).get("etsy_shop_id")
    return str(shop) if shop else None


def _history(ctx: AppContext, ws: Any) -> tuple[dict, str | None]:
    """(entries that guard the open shop, a problem code). Every shop's when unknown."""
    from ...drop import automation
    from ...errors import ValidationError

    try:
        state = automation.load_history(automation.history_path(ws.root))
        return automation.shop_history(state, _shop_key(ctx)), None
    except ValidationError:
        return {}, "history"


# --- upload / remove -------------------------------------------------------------------------


def _save_deliverable(ctx: AppContext, parts: list[str], data: bytes,
                      batch: str) -> dict[str, Any]:
    """PUT <folder>/dosyalar/<name>: one download file of a digital folder product.

    Any type a buyer can open is kept (PDF, ZIP, SVG, ...); programs and scripts are
    refused (client.BLOCKED_FILE_SUFFIXES). A file over Etsy's 20 MB limit is kept and
    named by the pending view and the run's check step, so the seller sees which one.
    It lands in the same product folder as the photos of its upload batch; sent without
    a batch (only download files were dropped for that folder), in the folder of that
    name, which is how a seller adds the missing `dosyalar` to a product already there.
    A file of the same name but other bytes in a product not drafted yet is a corrected
    version: it replaces the old one, which is moved to archive/ (never two versions sent).
    A physical template sells no downloads: refused (not_digital).
    """
    from ...client import BLOCKED_FILE_SUFFIXES

    name = _safe_segment(parts[-1], "file")
    suffix = Path(name).suffix.lower()
    if not suffix or suffix in BLOCKED_FILE_SUFFIXES:
        raise ApiError(422, "not_deliverable", f"{name} cannot be sold as a download.",
                       name=name)
    ws = ctx.workspace()
    if _template_digital(ws) is False:
        raise ApiError(422, "not_digital", "The template listing is a physical product, so "
                       "no download files are taken; files in a 'dosyalar' folder are only "
                       "for a digital template.", name=name)
    history, _problem = _history(ctx, ws)
    known_names = {n.casefold() for n in history}
    batch = re.sub(r"[^A-Za-z0-9_-]", "", batch)[:64]
    folder = _claim_folder(ws.products, _safe_segment(parts[0], "product"), batch, name, data,
                           sub=parts[1])
    product = ws.products / folder
    existing_sub = _entry_ci(product, parts[1])
    target = existing_sub if existing_sub is not None and existing_sub.is_dir() else (
        product / _safe_segment(parts[1], "dosyalar"))
    if folder.casefold() in known_names:
        # The product already became a draft: the history skips it for good.
        same = _same_file(target, name, data) if target.is_dir() else None
        return {"name": folder, "file": same or name, "folder": folder,
                "path": _rel(ws, target / same) if same else "", "duplicate": same is not None,
                "known": True, "ignored": None, "size": len(data), "deliverable": True}
    older = _entry_ci(target, name) if target.is_dir() else None
    if (older is not None and older.is_file() and not older.name.startswith(".")
            and _same_file(target, name, data) is None):
        _replace_download(ws, folder, older, data)
        return {"name": folder, "file": older.name, "folder": folder,
                "path": _rel(ws, older), "duplicate": False, "known": False,
                "ignored": None, "size": len(data), "deliverable": True, "replaced": True}
    saved, duplicate = _save_unique(target, name, data)
    return {"name": folder, "file": saved, "folder": folder, "path": _rel(ws, target / saved),
            "duplicate": duplicate, "known": False, "ignored": None, "size": len(data),
            "deliverable": True}


def _replace_download(ws: Any, folder: str, older: Path, data: bytes) -> None:
    """Put `data` in place of `older`; the old file goes to archive/<folder>/<dosyalar>/."""
    archive = ws.archive / folder / older.parent.name
    archive.mkdir(parents=True, exist_ok=True)
    stem, suffix = older.stem, older.suffix
    destination, number = archive / older.name, 1
    while destination.exists():
        number += 1
        destination = archive / f"{stem}-{number}{suffix}"
    temporary = older.with_name(f".{older.name}.upload")
    try:
        temporary.write_bytes(data)
        shutil.move(str(older), str(destination))
        os.replace(temporary, older)
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise ApiError(500, "internal", f"Could not replace {older.name}: {exc}") from exc


def _template_digital(ws: Any) -> bool | None:
    """Whether the saved template is digital; None when there is none or it is unreadable."""
    from ...drop.template import Template
    from ...errors import ValidationError

    if not ws.template_path.is_file():
        return None
    try:
        return Template.from_dict(ws.read_template()).digital
    except ValidationError:
        return None


def upload_file(req: Request) -> dict[str, Any]:
    from PIL import Image

    from ...drop import catalog
    from ...drop.workspace import IMAGE_SUFFIXES, PREVIEW_SUFFIXES
    from .. import files

    ctx = _ctx(req)
    parts = _split_path(req.query.get("path", ""))
    data = req.body
    if not data:
        raise ApiError(422, "invalid", "The file is empty.", field="body")
    if len(data) > MAX_UPLOAD:
        raise ApiError(413, "too_large", "A design can be at most 50 MB.",
                       limit=MAX_UPLOAD, max_mb=MAX_UPLOAD // (1024 * 1024))
    if len(parts) == 3:
        return _save_deliverable(ctx, parts, data, req.query.get("batch", ""))
    name = _safe_segment(parts[-1], "design")
    suffix = Path(name).suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        raise ApiError(422, "not_image", f"{name} is not a PNG, JPG or other image file.",
                       name=name)
    name = f"{Path(name).stem}{suffix}"
    size: tuple[int, int] = (0, 0)
    try:
        with Image.open(io.BytesIO(data)) as image:
            size = image.size
            # A small file can still hold a huge picture (13000x13000 PNG in 656 KB):
            # every thumbnail and the draft run would then need gigabytes. Refused
            # here, 422, before anything is decoded.
            if catalog.too_many_pixels(size):
                raise files.too_many_pixels(name, *size)
            _decode_all(image)
    except Image.DecompressionBombError as exc:
        raise files.too_many_pixels(name, *catalog.bomb_size(exc)) from exc
    except MemoryError as exc:
        raise files.too_many_pixels(name, *size) from exc
    except (OSError, ValueError, SyntaxError) as exc:
        raise ApiError(422, "not_image", f"{name} cannot be read as an image ({exc}).",
                       name=name) from exc

    ws = ctx.workspace()
    history, _problem = _history(ctx, ws)
    known_names = {n.casefold() for n in history}
    folder = None
    if len(parts) == 2:
        batch = re.sub(r"[^A-Za-z0-9_-]", "", req.query.get("batch", ""))[:64]
        folder = _claim_folder(ws.products, _safe_segment(parts[0], "product"), batch, name,
                               data)
    target = ws.products / folder if folder else ws.products
    if folder and folder.casefold() in known_names:
        # A product folder that already became a draft: a photo it has is a duplicate,
        # and a new one is not added — the history skips this product for good, so the
        # photo would never reach Etsy and only confuse what the folder holds.
        same = _same_file(target, name, data)
        return {
            "name": folder,
            "file": same or name,
            "folder": folder,
            "path": _rel(ws, target / (same or name)) if same else "",
            "duplicate": same is not None,
            "known": True,
            "ignored": None,
            "size": len(data),
        }
    saved, duplicate = _save_unique(target, name, data)
    product = folder or saved
    known = product.casefold() in known_names
    ignored = None
    if not folder and any(Path(saved).stem.endswith(s) for s in PREVIEW_SUFFIXES):
        ignored = "preview_name"
    path = target / saved
    return {
        "name": product,
        "file": saved,
        "folder": folder,
        "path": _rel(ws, path),
        "duplicate": duplicate,
        "known": known,
        "ignored": ignored,
        "size": len(data),
    }


def delete_file(req: Request) -> dict[str, Any]:
    """Take a design (or a product folder, or one photo of it) out of the next run.

    It is moved to the workspace's archive/ folder, never deleted: the file may be the
    seller's only copy (2-PRODUCTS is also filled from Explorer or Finder).
    """
    from ...drop.workspace import IMAGE_SUFFIXES
    from .. import files

    ctx = _ctx(req)
    if _active_job(ctx) is not None:
        raise ApiError(409, "busy", "Wait for the running batch to finish first.")
    parts = _split_path(req.query.get("path", ""))
    if len(parts) > 2:
        raise ApiError(404, "not_found", "Only designs and product folders can be removed here.")
    ws = ctx.workspace()
    target = files.resolve_inside(ws.products, "/".join(parts))
    if target is None or not target.exists():
        raise ApiError(404, "not_found", "No such design in the products folder.")
    if target.is_file() and target.suffix.lower() not in IMAGE_SUFFIXES:
        raise ApiError(404, "not_found", "Only designs can be removed here.")
    if target.is_dir() and len(parts) != 1:
        raise ApiError(404, "not_found", "Only product folders can be removed here.")
    archive = ws.archive / parts[0] if len(parts) == 2 else ws.archive
    archive.mkdir(parents=True, exist_ok=True)
    stem, suffix = (target.stem, target.suffix) if target.is_file() else (target.name, "")
    destination = archive / target.name
    number = 1
    while destination.exists():
        number += 1
        destination = archive / f"{stem}-{number}{suffix}"
    shutil.move(str(target), str(destination))
    return {"ok": True, "archived": _rel(ws, destination)}


# --- what the next run would do ----------------------------------------------------------------


def _template_info(ctx: AppContext, ws: Any) -> tuple[dict[str, Any] | None, str | None]:
    """(template facts for the page, a blocker code)."""
    from ...drop import description, pipeline, stream
    from ...drop.template import Template
    from ...errors import ValidationError

    if not ws.template_path.is_file():
        return None, "template"
    try:
        raw = ws.read_template()
        template = Template.from_dict(raw)
    except ValidationError as exc:
        return {"title": None, "invalid": str(exc)}, "template_invalid"
    fields = template.fields
    has_variations = raw.get("has_variations") if isinstance(raw, dict) else None
    # The description drafts get (drop.description): a saved description template, or
    # the template listing's own; how many of its sentences are about that listing's design.
    text, custom = description.effective(template, lang=ctx.language)
    info: dict[str, Any] = {
        "title": template.source_title or None,
        "source_listing_id": template.source_listing_id,
        "price": fields.get("price"),
        "currency": (ctx.status.get("shop") or {}).get("currency"),
        "description": bool(template.description.strip() or template.description_template),
        "description_custom": custom,
        "description_flags": len(description.template_flags(template, text)),
        # physical | download | both; digital drafts get each product's download files.
        "listing_type": template.listing_type,
        "digital": template.digital,
        # when_made made_to_order: a digital draft may go without a download file (the
        # seller sends it after the order), and a loose design is not attached.
        "made_to_order": pipeline.made_to_order(template),
        # A download is not shipped: no profile is needed (both still ships).
        "needs_shipping": template.listing_type != "download",
        "shipping_profile": bool(fields.get("shipping_profile_id")),
        "tags": len(template.tags),
        # Saved by Şablon İlan; None for a template captured before it was (unknown).
        "has_variations": has_variations if isinstance(has_variations, bool) else None,
        # What the template sells, as a mockup type (tshirt, mug...): None when unknown.
        "product": _template_product(template),
        "invalid": None,
    }
    try:
        stream.check_template(template)
    except ValidationError as exc:
        info["invalid"] = str(exc)
        return info, "template_invalid"
    return info, None


def _review_problem(entry: dict[str, Any]) -> str:
    """What the review list says about a history entry that is not "ok" (a code).

    partial:   the draft exists, an image or its variations are missing;
    drafted:   Etsy created the draft (it has an id), how far it got is unknown;
    uncertain: the create went out and no answer came back: a draft may exist.
    A run in the app drops an entry whose create Etsy provably refused, so an entry
    without an id is one whose answer never came.
    """
    if entry.get("status") == "partial":
        return "partial"
    if entry.get("listing_id"):
        return "drafted"
    return "uncertain"


def _ground(path: Path, deadline: float) -> str | None:
    """transparent | flat | photo for a loose design (drop.mockup.design_ground), kept per
    file version; None when it is not known yet and the time for reading is up."""
    from ...drop import mockup

    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    with _grounds_lock:
        known = _grounds.get(key)
    if known is not None:
        return known
    if time.monotonic() > deadline:
        return None
    ground = mockup.design_ground(path)[0] or "photo"  # unreadable: never composited
    with _grounds_lock:
        if len(_grounds) >= _KEEP_GROUNDS:
            _grounds.clear()
        _grounds[key] = ground
    return ground


def _template_product(template: Any) -> str | None:
    """The template listing's product as a mockup type, from its category (the cached
    seller taxonomy, never a request) or its title; None when neither says."""
    from . import listings as listings_api

    listing = {"taxonomy_id": template.fields.get("taxonomy_id"), "title": template.source_title}
    return listings_api.product_type_of(listing, listings_api.cached_taxonomy_paths())


def used_mockups(ws: Any, listing_type: str | None,
                 infos: dict[str, Any] | None = None) -> tuple[list[str], list[str]]:
    """(the mockups a run uses, the switched-on ones a download-only template leaves out).

    catalog.usage's rule with the template's type: switched on, the seller's order, for a
    download-only template none that shows a physical product, then at most 19 less one
    per info image; the first is the main image.
    """
    from ...drop import catalog

    use = catalog.usage(ws, infos, listing_type=listing_type)
    return list(use["used"]), list(use["left_out"])


def _pending_info(ctx: AppContext) -> dict[str, Any]:
    from ...config import MAX_LISTING_IMAGES
    from ...drop import automation, catalog, infoimages, pipeline, seeds
    from ...drop import watermark as watermark_mod

    if not ctx.wait_first_status(0):
        ctx.set_status_soon(0.0)
        ctx.wait_first_status(3.0)
    status = ctx.status
    ws = ctx.workspace()
    history, history_problem = _history(ctx, ws)
    known = {name.casefold() for name in history}
    groups = ws.product_groups()
    items: list[dict[str, Any]] = []
    concepts: set[str] = set()
    template, template_problem = _template_info(ctx, ws)
    digital = bool(template and template.get("digital"))
    to_order = bool(template and template.get("made_to_order"))
    deadline = time.monotonic() + GROUND_BUDGET
    for path, photos in groups:
        if path.name.casefold() in known:
            continue
        folder = path.is_dir()
        seed = seeds.derive(
            path / "IMG_0001.jpg" if folder else path,
            folder_fallback=folder or path.parent != ws.products,
        )
        # A product folder with only its `dosyalar` and no photos: listed, so the seller
        # sees it, and failed by the run's check step (stream: no_photos).
        no_photos = folder and not photos
        if seed and not no_photos:
            concepts.add(seed.text)
        shown = photos[0] if photos else path
        try:
            stat = shown.stat()
            mtime, size = int(stat.st_mtime), stat.st_size
        except OSError:
            mtime, size = 0, 0
        # What a digital draft of this product would attach, and what stops it: the
        # run's check step fails the product for the same reason (pipeline.deliverables).
        downloads: list[dict[str, Any]] = []
        deliverable_problem = None
        if digital:
            found, issue = pipeline.deliverables(path, made_to_order=to_order)
            downloads = [{"name": f.name, "path": _rel(ws, f), "size": _size(f)} for f in found]
            if issue is not None:
                deliverable_problem = {"code": issue[0], "params": issue[2]}
        # A loose design's background (a digital one always goes onto the mockups).
        ground = None
        if not folder and not digital and seed:
            ground = _ground(path, deadline)
        items.append({
            "name": path.name,
            "kind": "folder" if folder else "design",
            "ground": ground,
            "files": len(photos) if folder else 1,
            "thumb_path": "" if no_photos else _rel(ws, shown),
            "mtime": mtime,
            "size": size,
            "concept": seed.text if seed else "",
            "junk_reason": None if seed else (seed.reason or "junk"),
            "too_many": len(photos) > MAX_LISTING_IMAGES,
            "deliverables": downloads,
            "deliverable_problem": deliverable_problem,
            "no_photos": no_photos,
        })
    names = {path.name.casefold() for path, _ in groups}
    already_done, _ = automation.known_products(history, names)
    review = [
        {"name": name, "status": entry.get("status"), "listing_id": entry.get("listing_id"),
         "problem": _review_problem(entry), "message": entry.get("message") or ""}
        for name, entry in history.items()
        if name.casefold() in names and entry.get("status") != "ok"
    ]

    infos = catalog.load(ws)
    # The shop's info images: every draft ends with them, after its own pictures.
    info_images = infoimages.load(ws)
    # The same rule as the Mockuplar page and the run itself (catalog.usage): switched-on
    # mockups in the seller's order, less (a download-only template) those showing a
    # physical product, then at most 19 less one per info image; the first is the main
    # image.
    use = catalog.usage(ws, infos, info=len(info_images),
                        listing_type=template.get("listing_type") if template else None)
    enabled, left_out = list(use["used"]), list(use["left_out"])
    types = collections.Counter(infos[name].type for name in enabled if name in infos)

    blockers: list[str] = []
    state = status.get("state")
    if state in _STATE_BLOCKERS:
        blockers.append(_STATE_BLOCKERS[state])
    if template_problem:
        blockers.append(template_problem)
    if history_problem:
        blockers.append(history_problem)
    mark = watermark_mod.describe(ws, template.get("listing_type") if template else None)
    if mark["on"] and mark["problem"] and mark["applies"] is not False:
        blockers.append("watermark")
    lock = automation.lock_info(ws.root)
    if _active_job(ctx) is not None:
        blockers.append("running")
    elif lock is not None:
        blockers.append("locked")
    runnable = sum(1 for item in items if _runnable(item))
    # Loose designs with nothing to see through: up as they are, or (the seller's choice
    # on the start card) onto the mockups when on a solid background.
    grounds = collections.Counter(item["ground"] or "unknown" for item in items
                                  if _runnable(item) and item["kind"] == "design"
                                  and item["ground"] != "transparent" and not digital)
    if not items:
        blockers.append("empty")
    elif not runnable:
        # Nothing could become a draft: the names say nothing, the product folders have
        # no photos, or (a digital template) no product has a download it can send.
        files_only = any(item["deliverable_problem"] and not item["junk_reason"]
                         and not item["too_many"] and not item["no_photos"] for item in items)
        if all(item["no_photos"] for item in items):
            blockers.append("photos_only")
        else:
            blockers.append("deliverables_only" if files_only else "junk_only")

    warnings: list[str] = []
    junk = sum(1 for item in items if item["junk_reason"] or item["too_many"])
    if junk:
        warnings.append("junk")
    if any(item["deliverable_problem"] and not item["junk_reason"] and not item["too_many"]
           and not item["no_photos"] for item in items):
        warnings.append("deliverables")
    if any(item["no_photos"] for item in items):
        warnings.append("no_photos")
    if not enabled and not left_out and any(item["kind"] == "design" for item in items):
        warnings.append("no_mockups")
    if template and template.get("needs_shipping") and not template.get("shipping_profile"):
        warnings.append("no_shipping_profile")
    # Sentences about the template listing's own design would reach every draft: the
    # start card says so once, with a link to Şablon İlan's description template.
    if template and template.get("description_flags") and runnable:
        warnings.append("description_flagged")
    info_count = len(info_images)
    images_each = len(enabled) + 1 + info_count
    # Only what will run, with each product's own image count, an upper bound: a
    # folder's photos; one upload for a JPEG finished photo (never composited); mockups +
    # the flat design for anything that may be transparent artwork, and for a JPEG on a
    # solid background too (the start card's "place" choice puts it onto every mockup).
    # A digital template composites every loose design, a JPEG too: it is the download.
    # Each ends with the info images that fit (drop.infoimages.fitting).
    run_items = [item for item in items if _runnable(item)]

    def own_images(item: dict[str, Any]) -> int:
        if item["kind"] == "folder":
            return item["files"]
        if (not digital and Path(item["name"]).suffix.lower() in (".jpg", ".jpeg")
                and item.get("ground") != "flat"):
            return 1
        return len(enabled) + 1

    images_total = sum(
        own + len(infoimages.fitting(own, info_images))
        for own in (own_images(item) for item in run_items)
    )
    # Product folders whose photos leave room for only some of the info images.
    info_cut = sum(1 for item in run_items if item["kind"] == "folder"
                   and len(infoimages.fitting(item["files"], info_images)) < info_count)
    # A digital draft uploads its download files too, one request each.
    files_total = sum(len(item["deliverables"]) for item in run_items)
    estimate = pipeline.estimate_requests(
        len(run_items),
        len({item["concept"] for item in run_items}),
        images=images_total,
        has_variations=bool(template and template.get("has_variations") is not False),
        template_inventory=bool(template and template.get("source_listing_id")),
        files=files_total,
    )
    quota = status.get("quota_remaining")
    if isinstance(quota, int) and estimate > quota:
        warnings.append("quota")
    if info_cut:
        warnings.append("info_cut")
    locked_at = int(lock["since"]) if lock and lock["since"] else None

    shop = status.get("shop") or {}
    return {
        "items": items,
        "count": len(items),
        "runnable": runnable,
        "already_done": len(already_done),
        "review": review,
        "mockups": {
            "enabled": len(enabled),
            "used": len(enabled),
            "switched_on": use["enabled"],
            "over_limit": len(use["over_limit"]),
            "max": use["max"],
            "main": enabled[0] if enabled else None,
            "main_type": (infos[enabled[0]].type if enabled and enabled[0] in infos
                          else None),
            "total": len(infos),
            "types": dict(types),
            "primary": types.most_common(1)[0][0] if types else None,
            # The ones this run uses, in order, and (a download-only template) the
            # switched-on ones it leaves out because they show a physical product.
            "names": list(enabled),
            # The same with each one's product and colour, for the start card's labels.
            "used_items": [{"name": n, "type": infos[n].type, "color": infos[n].color}
                           for n in enabled if n in infos],
            "left_out": [{"name": n, "type": infos[n].type} for n in left_out if n in infos],
        },
        # Every draft ends with these (Şablon İlan); `cut`: product folders that get only
        # the first ones, their photos leaving too little room of Etsy's `images_max`.
        "info_images": {"count": info_count, "names": [i.name for i in info_images],
                        "cut": info_cut},
        "images_max": MAX_LISTING_IMAGES,
        "opaque": {"flat": grounds.get("flat", 0), "photo": grounds.get("photo", 0),
                   "unknown": grounds.get("unknown", 0)},
        "watermark": mark,
        "template": template,
        "shop": {"connected": state == "connected", "name": shop.get("name"),
                 "state": state},
        "workspace": str(ws.root),
        "ready": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "estimate_requests": estimate,
        "files_total": files_total,
        "listing_type": template.get("listing_type") if template else None,
        "quota_remaining": quota,
        "concepts": len(concepts),
        "images_each": images_each,
        "locked_at": locked_at,
        "lock": None if lock is None else {"pid": lock["pid"], "alive": lock["alive"],
                                           "stale": lock["stale"]},
        "concurrency": CONCURRENCY,
    }


def _runnable(item: dict[str, Any]) -> bool:
    """A product the run would try: a readable name, photos (not too many), its downloads."""
    return (not item["junk_reason"] and not item["too_many"] and not item["no_photos"]
            and not item["deliverable_problem"])


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def pending(req: Request) -> dict[str, Any]:
    return _pending_info(_ctx(req))


# --- the shop section (the start card's "Mağaza bölümü") --------------------------------------


def _shop_sections(ctx: AppContext, *, fresh: bool = False) -> list[dict[str, Any]]:
    """The open shop's sections, in the shop's order: [{id, title, count}].

    getShopSections (read only; the OAS asks for the API key alone). Kept SECTIONS_TTL
    seconds per Etsy client; `fresh` reads them again. Errors propagate (ApiError
    setup_needed, EtsyApiError, AuthError, ...).
    """
    from ...drop.template import section_number

    client = ctx.client()
    now = time.monotonic()
    if not fresh:
        with _sections_lock:
            found = _sections_cache.get(client)
        if found is not None and now - found[0] < SECTIONS_TTL:
            return [dict(s) for s in found[1]]
    with client.attempts(2):
        raw = client.shop_sections()
    ranked: list[tuple[int, int, dict[str, Any]]] = []
    for n, record in enumerate(raw or []):
        if not isinstance(record, dict):
            continue
        section_id = section_number(record.get("shop_section_id"))
        if section_id is None:
            continue
        count = record.get("active_listing_count")
        rank = record.get("rank")
        ranked.append((
            rank if isinstance(rank, int) and not isinstance(rank, bool) else 1 << 30, n,
            {"id": section_id, "title": str(record.get("title") or "").strip(),
             "count": count if isinstance(count, int) and not isinstance(count, bool) else None},
        ))
    items = [entry for _rank, _n, entry in sorted(ranked, key=lambda r: (r[0], r[1]))]
    with _sections_lock:
        _sections_cache[client] = (now, items)
    return [dict(s) for s in items]


def _section_choice(value: Any) -> str | int:
    """"template" | "none" | a section id, from a request body; 422 for anything else."""
    from ...drop.template import section_number

    if value in (SECTION_TEMPLATE, SECTION_NONE):
        return value
    number = section_number(value)
    if number is None:
        raise ApiError(422, "invalid", 'section must be "template", "none" or a section id',
                       field="section")
    return number


def _etsy_shop_id(ctx: AppContext) -> int | None:
    value = (ctx.status.get("shop") or {}).get("etsy_shop_id")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _remembered_section(ctx: AppContext) -> dict[str, Any] | None:
    """The open shop's last choice ({choice, title}), or None. A choice made while the
    shop's keys signed in to another Etsy shop is not this shop's."""
    from ...drop.template import section_number

    saved = ctx.shop_prefs().get(SECTION_PREF)
    if not isinstance(saved, dict):
        return None
    choice = saved.get("choice")
    if choice not in (SECTION_TEMPLATE, SECTION_NONE):
        choice = section_number(choice)
        if choice is None:
            return None
    owner, current = saved.get("etsy_shop_id"), _etsy_shop_id(ctx)
    if isinstance(owner, int) and current is not None and owner != current:
        return None
    title = saved.get("title")
    return {"choice": choice, "title": title if isinstance(title, str) else None}


def _template_section(ctx: AppContext) -> int | None:
    """The saved template listing's section id (None: none, or no usable template)."""
    from ...drop.template import Template
    from ...errors import ValidationError

    ws = ctx.workspace()
    if not ws.template_path.is_file():
        return None
    try:
        return Template.from_dict(ws.read_template()).section_id
    except (ValidationError, OSError):
        return None


def sections(req: Request) -> dict[str, Any]:
    """What the start card's "Mağaza bölümü" select offers and starts on.

    {"available": bool, "error": code | null, "sections": [{id, title, count}],
     "template": {"id", "title", "missing"}, "choice": "template" | "none" | id,
     "remembered": {"choice", "title"} | null, "remembered_missing": bool}

    `template.missing`: the template listing's section is no longer in the shop (its
    drafts would go into no section). `remembered_missing`: the last choice was a
    section that is gone; the select starts on "template" instead. When the sections
    cannot be read (`available` false, `error` the reason), "template" and "none" are
    all it offers.
    """
    ctx = _ctx(req)
    fresh = req.bool_query("refresh", False)
    template_id = _template_section(ctx)
    remembered = _remembered_section(ctx)
    error = None
    try:
        items = _shop_sections(ctx, fresh=fresh)
    except (ApiError, EtsyotomasyonError) as exc:
        from ..errors import describe

        error = describe(exc)["code"]
        log.info("designs: could not read the shop's sections (%s)", error)
        items = []
    available = error is None
    by_id = {s["id"]: s for s in items}
    template_found = by_id.get(template_id) if template_id is not None else None
    choice: str | int = SECTION_TEMPLATE
    remembered_missing = False
    if remembered is not None:
        wanted = remembered["choice"]
        if wanted == SECTION_NONE and (items or not available):
            choice = SECTION_NONE
        elif isinstance(wanted, int):
            if wanted in by_id:
                choice = wanted
            elif available:
                remembered_missing = True
    return {
        "available": available,
        "error": error,
        "sections": items,
        "template": {
            "id": template_id,
            "title": template_found["title"] if template_found else None,
            "missing": available and template_id is not None and template_found is None,
        },
        "choice": choice,
        "remembered": remembered,
        "remembered_missing": remembered_missing,
    }


def _run_section(ctx: AppContext, template: Any, choice: str | int) -> tuple[Any, dict[str, Any]]:
    """(the template this run's drafts copy, the run's section {choice, id, title,
    template_gone}) for the start card's choice; 409 section_gone for a chosen section
    the shop no longer has."""
    run = {"choice": choice, "id": None, "title": None, "template_gone": None}
    if choice == SECTION_NONE:
        return template.with_section(None), run
    if choice == SECTION_TEMPLATE:
        own = template.section_id
        if own is None:
            return template, run
        run["id"] = own
        try:
            items = _shop_sections(ctx, fresh=True)
        except (ApiError, EtsyotomasyonError) as exc:
            # Not known now: the template's section goes as it is (as before the choice).
            log.info("designs: could not check the template's section (%s)", exc)
            return template, run
        found = next((s for s in items if s["id"] == own), None)
        if found is None:
            # Deleted in Etsy: no section, rather than every draft failing on it.
            run.update(id=None, template_gone=own)
            return template.with_section(None), run
        run["title"] = found["title"]
        return template, run
    items = _shop_sections(ctx, fresh=True)
    found = next((s for s in items if s["id"] == choice), None)
    if found is None:
        raise ApiError(409, "section_gone", "That shop section no longer exists on Etsy.",
                       id=choice)
    run.update(id=choice, title=found["title"])
    return template.with_section(choice), run


# --- the run -------------------------------------------------------------------------------------


class _JobCancel:
    def __init__(self, job: Job) -> None:
        self.job = job

    def is_set(self) -> bool:
        return self.job.cancelled


def _new_item(data: dict[str, Any]) -> dict[str, Any]:
    from ...drop.stream import STEPS

    # Every key exists from the start: the dicts are read while other threads write.
    return {
        "index": data["index"],
        "name": data["name"],
        "kind": data.get("kind", "design"),
        "thumb_path": data.get("source", ""),
        "files": data.get("files", 1),
        "status": "queued",
        "step": None,
        "steps": {step: "todo" for step in STEPS},
        "mode": "",
        "title": "",
        "tags": [],
        "images": [],
        "flat": None,
        "listing_id": None,
        "images_uploaded": 0,
        "images_total": 0,
        "deliverables": [],
        "files_uploaded": 0,
        "files_total": data.get("files_total", 0),
        "info_images": [],
        "sampled": None,
        "warnings": [],
        "error": None,
        "updated_at": time.time(),
    }


def _copy_item(item: dict[str, Any]) -> dict[str, Any]:
    out = dict(item)
    out["steps"] = dict(item["steps"])
    out["tags"] = list(item["tags"])
    out["images"] = list(item["images"])
    out["deliverables"] = list(item.get("deliverables") or [])
    out["info_images"] = list(item.get("info_images") or [])
    out["warnings"] = list(item["warnings"])
    return out


class _Tracker:
    """Turns drop.stream events into job state, SSE item events and progress."""

    def __init__(self, job: Job, *, dry_run: bool, template: dict[str, Any] | None,
                 mockups: dict[str, Any], section: dict[str, Any] | None = None) -> None:
        self.job = job
        self.lock = threading.RLock()
        self.items: list[dict[str, Any]] = []
        self.started = time.time()
        self.state: dict[str, Any] = {
            "total": 0, "done": 0, "created": 0, "errors": 0, "warnings": 0, "checked": 0,
            "cancelled_items": 0, "started_at": self.started, "finished_at": None,
            "items": [], "current": None, "dry_run": dry_run, "batch": None,
            "out_dir": None, "template": template, "mockups": mockups,
            # The start card's shop section for this run ({choice, id, title,
            # template_gone}); None when the drafts simply copy the template's.
            "section": section,
            "concurrency": CONCURRENCY, "already_done": 0, "needs_review": [],
            "result": None, "listing_type": (template or {}).get("listing_type") or "physical",
        }
        self._last_state = 0.0
        self.publish_state(force=True)

    # the job state other requests read: a copy, refreshed at most every STATE_INTERVAL
    def publish_state(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_state < STATE_INTERVAL:
            return
        self._last_state = now
        snapshot = dict(self.state)
        snapshot["items"] = [_copy_item(item) for item in self.items]
        self.job.set_state(**snapshot)

    def counts(self) -> dict[str, Any]:
        return {key: self.state[key] for key in (
            "total", "done", "created", "errors", "warnings", "checked", "cancelled_items",
            "current")}

    def _current(self) -> int | None:
        """The product the "being prepared" card follows: the first one still in steps 1-4,
        else the one being created."""
        for item in self.items:
            if item["status"] == "running" and item["step"] in ("mockup", "research", "title",
                                                                 "tags"):
                return item["index"]
        for item in self.items:
            if item["status"] == "running":
                return item["index"]
        return None

    def on_event(self, name: str, step: str, status: str, data: dict[str, Any]) -> None:
        from ...drop.stream import FINAL, STEPS

        with self.lock:
            if step == "batch":
                self.items = [_new_item(entry) for entry in data.get("items", [])]
                self.state.update(
                    total=len(self.items), batch=data.get("batch"), out_dir=data.get("out_dir"),
                    already_done=len(data.get("already_done") or []),
                    needs_review=list(data.get("needs_review") or []),
                    listing_type=data.get("listing_type") or self.state["listing_type"],
                )
                self.job.progress(0, len(self.items))
                self.publish_state(force=True)
                self.job.emit("batch", state=self.counts(),
                              items=[_copy_item(item) for item in self.items],
                              batch=data.get("batch"))
                return
            index = data.get("index")
            if not isinstance(index, int) or not 0 <= index < len(self.items):
                return
            item = self.items[index]
            for key in ("images", "flat", "mode", "title", "tags", "listing_id",
                        "images_uploaded", "images_total", "sampled", "deliverables",
                        "files_uploaded", "files_total", "info_images"):
                if key in data and data[key] is not None:
                    item[key] = list(data[key]) if isinstance(data[key], list) else data[key]
            if step in STEPS:
                item["steps"][step] = status
                if status == "running":
                    item["status"] = "running"
                    item["step"] = step
            elif step == "item":
                was_final = item["status"] in FINAL
                item["status"] = status
                item["step"] = None
                if isinstance(data.get("steps"), dict):
                    item["steps"].update(data["steps"])
                item["warnings"] = list(data.get("warnings") or [])
                item["error"] = data.get("problem")
                if status in FINAL and not was_final:
                    self.state["done"] += 1
                    if status in ("ok", "partial"):
                        self.state["created"] += 1
                    elif status == "error":
                        self.state["errors"] += 1
                    elif status == "checked":
                        self.state["checked"] += 1
                    elif status == "cancelled":
                        self.state["cancelled_items"] += 1
                    self.state["warnings"] += len(item["warnings"])
            item["updated_at"] = time.time()
            self.state["current"] = self._current()
            final = step == "item" and status in FINAL
            if final:
                current = self.state["current"]
                label = self.items[current]["name"] if current is not None else item["name"]
                self.job.progress(self.state["done"], self.state["total"], label=label)
            self.job.emit("item", item=_copy_item(item), state=self.counts())
            # A product's outcome is always in the copied state at once: a page that
            # reloads between two events must not show a finished product as still
            # running (it would until the next copy, up to STATE_INTERVAL later).
            self.publish_state(force=final)

    def abort(self) -> None:
        """The run broke off: nothing still waiting will happen now."""
        from ...drop.stream import FINAL

        with self.lock:
            for item in self.items:
                if item["status"] not in FINAL:
                    item["status"] = "cancelled"
                    item["step"] = None
                    item["steps"] = {k: ("todo" if v == "running" else v)
                                     for k, v in item["steps"].items()}
                    self.state["done"] += 1
                    self.state["cancelled_items"] += 1
            self.state["finished_at"] = time.time()
            self.publish_state(force=True)

    def finish(self, report: Any, ws: Any) -> dict[str, Any]:
        with self.lock:
            finished = report.finished_at or time.time()
            summary = {
                "batch": report.batch,
                "dry_run": report.dry_run,
                "total": len(report.items),
                "created": report.created,
                "ok": report.count("ok"),
                "partial": report.count("partial"),
                "errors": report.errors,
                "checked": report.count("checked"),
                "cancelled_items": report.count("cancelled"),
                "warnings": report.warnings,
                "cancelled": report.cancelled,
                "stopped": report.stopped.to_dict() if report.stopped else None,
                "started_at": self.started,
                "finished_at": finished,
                "duration": round(finished - self.started, 1),
                "csv": _rel(ws, report.csv_path) if report.csv_path else None,
                "out_dir": _rel(ws, report.out_dir) if report.out_dir.exists() else None,
                "already_done": len(report.already_done),
                "needs_review": list(report.needs_review),
                "researched": report.researched,
                "cached": report.cached,
                "listing_type": self.state.get("listing_type") or "physical",
                "files_uploaded": sum(item.files_uploaded for item in report.items),
            }
            self.state.update(finished_at=finished, result=summary)
            self.publish_state(force=True)
            return summary


def _write_last_run(ws: Any, job: Job, tracker: _Tracker, summary: dict[str, Any]) -> None:
    from ...drop import automation

    data = {
        "version": 1,
        "job_id": job.id,
        "summary": summary,
        "template": tracker.state.get("template"),
        "mockups": tracker.state.get("mockups"),
        "section": tracker.state.get("section"),
        "items": [_copy_item(item) for item in tracker.items],
    }
    path = ws.drafts / LAST_RUN_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        # Retried while a reader (GET /api/designs/last, a virus scan) has it open.
        automation.replace_file(tmp, path)
    except OSError:
        log.warning("could not save %s", path)


def _notify_end(ctx: AppContext, summary: dict[str, Any]) -> None:
    params = {"n": summary["created"], "total": summary["total"], "errors": summary["errors"],
              "checked": summary["checked"]}
    if summary["dry_run"]:
        ctx.notify(KIND, "notify.checked", params, tone="info", link="/tasarim-yukle")
    elif summary["stopped"]:
        params["reason"] = summary["stopped"]["code"]
        ctx.notify(KIND, "notify.stopped", params, tone="danger", link="/tasarim-yukle")
    elif summary["cancelled"]:
        ctx.notify(KIND, "notify.cancelled", params, tone="warning", link="/tasarim-yukle")
    elif summary["errors"]:
        ctx.notify(KIND, "notify.done_errors", params, tone="warning", link="/tasarim-yukle")
    else:
        ctx.notify(KIND, "notify.done", params, tone="success", link="/tasarim-yukle")


def start(req: Request) -> dict[str, Any]:
    ctx = _ctx(req)
    body = req.json_object()
    dry_run = body.get("dry_run", False)
    if not isinstance(dry_run, bool):
        raise ApiError(422, "invalid", "dry_run must be true or false", field="dry_run")
    from ...drop import stream

    opaque = body.get("opaque", stream.OPAQUE_AS_IS)
    if opaque not in stream.OPAQUE_CHOICES:
        raise ApiError(422, "invalid", "opaque must be as_is or place", field="opaque")
    section = _section_choice(body["section"]) if body.get("section") is not None else None
    with _start_lock:
        return _start(ctx, dry_run, opaque, section)


def _start(ctx: AppContext, dry_run: bool, opaque: str = "as_is",
           section: str | int | None = None) -> dict[str, Any]:
    from ...drop import automation, stream
    from ...drop.template import Template

    if _active_job(ctx) is not None:
        raise ApiError(409, "busy", "A batch is already running.")
    info = _pending_info(ctx)
    blockers = [b for b in info["blockers"] if not (dry_run and b in _ETSY_BLOCKERS)]
    if blockers:
        raise ApiError(409, "setup_incomplete", "Finish the setup before starting.",
                       blockers=blockers)
    ws = ctx.workspace()
    template = Template.from_dict(ws.read_template())
    run_section = None
    if section is not None and not dry_run:
        # The start card's "Mağaza bölümü": every draft of this run goes there.
        template, run_section = _run_section(ctx, template, section)
        ctx.update_shop_prefs(**{SECTION_PREF: {
            "choice": section, "title": run_section["title"],
            "etsy_shop_id": _etsy_shop_id(ctx)}})
    mockups_info = dict(info["mockups"])
    # The mockups the start card named (a download-only template's without the physical
    # ones), in its order: the pending view already applied catalog.usage's rule.
    files = {path.name.casefold(): path for path in ws.mockup_files()}
    mockup_paths = [files[name.casefold()] for name in mockups_info.get("names") or []
                    if name.casefold() in files]

    def work(job: Job) -> dict[str, Any]:
        tracker = _Tracker(job, dry_run=dry_run, template=info["template"],
                           mockups=mockups_info, section=run_section)
        try:
            if dry_run:
                try:
                    client = ctx.client(require_auth=False)  # research needs only the keys
                except ApiError:
                    client = None
            else:
                client = ctx.client()
            try:
                report = stream.run_stream(
                    ws, template, client, mockups=mockup_paths,
                    on_event=tracker.on_event, cancel=_JobCancel(job),
                    concurrency=CONCURRENCY, dry_run=dry_run, opaque=opaque,
                )
            except automation.UploadLocked as exc:
                raise ApiError(409, "locked", str(exc)) from exc
            except stream.TemplateGone as exc:
                raise ApiError(409, "template_gone", str(exc)) from exc
            finally:
                if not dry_run:  # drafts may have been created, even by a failed run
                    ctx.changed("listings", source="designs")
        except Exception as exc:
            from ..errors import describe

            tracker.abort()
            if not job.cancelled:
                ctx.notify(KIND, "notify.failed", {"code": describe(exc)["code"]},
                           tone="danger", link="/tasarim-yukle")
            raise
        summary = tracker.finish(report, ws)
        if report.items:
            _write_last_run(ws, job, tracker, summary)
            _notify_end(ctx, summary)
        if report.cancelled:
            job.check_cancel()  # ends the job as "cancelled"; the state keeps the results
        return summary

    job = ctx.jobs.start(
        KIND, "designs:job.title_dry" if dry_run else "designs:job.title", work,
        params={"n": info["count"], "dry_run": dry_run}, cancellable=True, refresh_status=True,
    )
    return job.summary()


def last_run(req: Request) -> dict[str, Any]:
    from ...drop import automation

    ctx = _ctx(req)
    path = ctx.workspace().drafts / LAST_RUN_FILE
    try:
        text = automation.read_text(path)
        data = json.loads(text) if text is not None else None
    except (OSError, ValueError):
        return {"run": None}
    return {"run": data if isinstance(data, dict) else None}


# --- recovery ---------------------------------------------------------------------------------


def forget(req: Request) -> dict[str, Any]:
    """Let a product the history holds as failed or uncertain be tried again.

    Only for entries that are not "ok": an "ok" entry is a real draft, and forgetting
    it would make a second one. The seller confirms they checked Etsy first.
    """
    from ...drop import automation

    ctx = _ctx(req)
    body = req.json_object()
    name = body.get("name")
    if not isinstance(name, str) or not name:
        raise ApiError(422, "invalid", "name is required", field="name")
    if body.get("confirm") is not True:
        raise ApiError(400, "confirm_required", "Forgetting an attempt needs {\"confirm\": true}.")
    if _active_job(ctx) is not None:
        raise ApiError(409, "busy", "Wait for the running batch to finish first.")
    shop = _shop_key(ctx)
    if shop is None:
        raise ApiError(409, "setup_needed", "Connect the shop first.", step="connect")
    ws = ctx.workspace()
    try:
        with automation.upload_lock(ws.root):
            path = automation.history_path(ws.root)
            state = automation.load_history(path)
            section = state.get(shop) or {}
            key = next((k for k in section if k.casefold() == name.casefold()), None)
            if key is None:
                raise ApiError(404, "not_found", "That product has no recorded attempt.")
            entry = section[key]
            # "ok" is a finished draft; a listing id means Etsy created one, however far
            # it got. Forgetting either would make a second draft of the same design.
            if entry.get("status") == "ok" or entry.get("listing_id"):
                raise ApiError(409, "already_drafted", "That product already has a draft.",
                               listing_id=entry.get("listing_id"))
            del section[key]
            state[shop] = section
            automation.save_history(path, state)
    except automation.UploadLocked as exc:
        raise ApiError(409, "locked", str(exc)) from exc
    ctx.set_status_soon()
    return {"ok": True, "name": key}


def unlock(req: Request) -> dict[str, Any]:
    """Remove the upload lock a crashed run left behind (the seller confirms).

    A lock whose process still runs on this computer (a `drop auto` in a terminal,
    another etsyotomasyon) is refused with 409 lock_active unless {"force": true}: the
    process id may since belong to another program, so the seller can still decide.
    """
    from ...drop import automation

    ctx = _ctx(req)
    body = req.json_object()
    if body.get("confirm") is not True:
        raise ApiError(400, "confirm_required", "Removing the lock needs {\"confirm\": true}.")
    if _active_job(ctx) is not None:
        raise ApiError(409, "busy", "A batch is running in this app right now.")
    root = ctx.workspace().root
    info = automation.lock_info(root)
    if info is not None and info["alive"] and body.get("force") is not True:
        raise ApiError(409, "lock_active", "The process that holds the lock is still running.",
                       pid=info["pid"])
    path = automation.lock_path(root)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ApiError(500, "internal", f"Could not remove the lock: {exc}") from exc
    return {"ok": True}
