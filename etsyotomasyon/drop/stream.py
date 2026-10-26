"""Designs in, Etsy drafts out, one product at a time, with live progress.

This is the web UI's engine behind Tasarım Yükle. It does what `drop auto` does —
composite, research, write the copy, validate, create the draft — but per product
instead of per batch, so a screen can show every product move through six steps:

    mockup -> research -> title -> tags -> check -> draft

Steps 1-5 are local work (plus read-only research) and run for up to `concurrency`
products at once. Step 6 writes to Etsy and runs on the calling thread, one product
at a time, in folder order. One product that fails does not stop the others.

The guarantees of `drop auto` hold here too, because they are the same code:
- the workspace's upload lock, so two runs never write at once;
- upload-history.json, written BEFORE each create and after every write, so a product
  that was attempted is never recreated automatically (see `automation`);
- `listings.prepare` validates every row and every image is fully decoded before its
  draft is created;
- nothing is ever published: rows never carry a listing_id or a state.

The template's type decides what a draft is. `physical` as always; `download` and
`both` drafts also get the product's download files, after its images: a loose design's
ORIGINAL file, or every file in a folder product's `dosyalar` / `files` subfolder (the
folder's own images stay its photos). A digital loose design always goes onto the
mockups (plus a small flat preview), never up as it is, even when it is opaque: it is
the file being sold. Step 5 (check) finds and validates the downloads before the
product's draft is created; a missing, oversized or refused file, a subfolder in
`dosyalar`, more than Etsy takes, or a photo that is also the download, is that
product's error (no_deliverable, nested_files, too_many_files, file_type,
file_too_large, file_empty, download_is_photo, ...). A made-to-order template needs no
download (made_to_order_no_file warns; a loose design is then not attached). A product
folder with only its `dosyalar` and no photos fails step 5 at once (no_photos), whatever
the template. A file that fails after the draft exists leaves the product `partial`,
and the history records `files_uploaded` next to `images_uploaded`.

The workspace's watermark (drop.watermark), when it is on and its scope takes this
template, is stamped on a copy of every picture a draft shows buyers: the mockups, the
flat render or preview, a folder's own photos, a photo uploaded as it is. The copies go
to the batch's `watermarked` folder (one name each); the seller's files and the download
files are never touched, and Etsy is told each stamped picture is watermarked. A picture
the mark cannot be put on fails its product (watermark_failed): it never goes up bare.

A loose design without see-through pixels is a finished photo and goes up as it is,
with an `as_is` warning (never silently). With `opaque="place"` (the page's choice for
the batch), one saved on a solid background (all four borders one colour,
mockup.ground_colour) has that background removed from the edges inwards and goes onto
the mockups like any transparent design; a real photo still goes up as it is.

Every picture a draft gets carries an alt text Etsy stores with it: the concept, and
on a mockup the mockup's product and colour ("Retro Mountain Sunset t-shirt, white").

The shop's info images (drop.infoimages: the materials, size and how-to cards every
listing ends with) go up after the product's own pictures, in the seller's order, each
with its own alt text, never watermarked. They are read, converted if they have to be,
and fully decoded once, before the plan goes out: one that cannot be used stops the run
before anything is sent. Photos and info images never pass Etsy's 20: the caller's
mockups are already capped for them (catalog.usage), and a product folder whose photos
leave too little room gets the first ones that fit (`info_images_cut` warns). They are
not in `images` (the product's own pictures, as the screen shows them) but in
`info_images`; the history entry lists their names.
Step 5 warns (never fails) when a picture's short side is under 1000 px
(`small_image`), or when a design had to be enlarged more than twice onto a mockup or
its flat render (`design_small`): both would look soft on Etsy.

`drop run` and `drop auto` are untouched; this module only reuses their pieces.

Events: `on_event(name, step, status, data)`, always with `data["index"]`.
- step in STEPS, status in running | done | warn | error; data may carry `images`
  (paths relative to the workspace root), `flat` (the one of them that is the plain
  design, not a mockup), `mode`, `title`, `tags`, `sampled`, `deliverables` (the
  download files, relative paths), `images_uploaded` / `images_total`,
  `files_uploaded` / `files_total`, `listing_id`, `problem` ({code, message, ...}).
- step "item": the product's outcome or waiting state; status in waiting | ok |
  partial | error | cancelled | checked (a dry run's good product).
- step "batch" (name ""), status "running", once before any product: the plan, with
  `listing_type`, each product's `files_total` (its download files, 0 if physical) and
  `info_images` (the names every draft ends with).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

from .. import csvio, listings
from ..config import LISTING_TYPES, MAX_LISTING_IMAGES, MAX_TAGS
from ..errors import AuthError, AuthUnreachable, EtsyApiError, ValidationError
from ..listings import DIGITAL_TYPES
from ..seo import MarketReport, tag_key
from . import (
    automation,
    catalog,
    description,
    generate,
    infoimages,
    mockup,
    pipeline,
    printfiles,
    seeds,
)
from . import watermark as watermark_mod
from .template import Template
from .workspace import Workspace

log = logging.getLogger("etsyotomasyon.drop")

STEPS = ("mockup", "research", "title", "tags", "check", "draft")

# Step states (the UI's step dots).
TODO, RUNNING, DONE, WARN, ERROR = "todo", "running", "done", "warn", "error"

# Product states.
QUEUED = "queued"
ACTIVE = "running"
WAITING = "waiting"  # steps 1-5 done, waiting for its turn to be created
OK = "ok"
PARTIAL = "partial"  # the draft exists, but an image, a file or its variations did not make it
FAILED = "error"
CANCELLED = "cancelled"
CHECKED = "checked"  # a dry run's product that would be created
FINAL = frozenset({OK, PARTIAL, FAILED, CANCELLED, CHECKED})

# Fewer ranking listings than this is too thin a sample to borrow tags from.
THIN_SAMPLE = 20
# What becomes of a loose design without see-through pixels (run_stream's `opaque`).
OPAQUE_AS_IS = "as_is"  # a finished photo: up as it is
OPAQUE_PLACE = "place"  # on a solid background: the background removed, onto the mockups
OPAQUE_CHOICES = (OPAQUE_AS_IS, OPAQUE_PLACE)
# A picture shorter than this on its short side looks soft on Etsy (it recommends 2000).
SMALL_IMAGE_EDGE = 1000
# Etsy stores up to 500 characters of alt text (OAS uploadListingImage); we send 250.
ALT_TEXT_MAX = 250
# A mockup type as a buyer reads it in an alt text (the drafts' copy is English).
TYPE_NOUNS = {
    "tshirt": "t-shirt", "sweatshirt": "sweatshirt", "hoodie": "hoodie", "mug": "mug",
    "poster": "poster", "canvas": "canvas print", "phone_case": "phone case",
    "tote": "tote bag", "pillow": "pillow", "sticker": "sticker",
}
# Drafts failing one after another usually share a cause (a template Etsy refuses, a
# lost connection). Stopping spares the rest from being marked as failed attempts.
STOP_AFTER_FAILURES = 3

OnEvent = Callable[[str, str, str, dict], None]


@dataclass
class Problem:
    """Why a step warned or a product failed: a code the UI translates, plus detail."""

    code: str
    message: str = ""
    step: str = ""
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "step": self.step,
                "params": dict(self.params)}


class TemplateGone(ValidationError):
    """The template listing the drafts copy their variations from is gone from Etsy."""


class _ProductFailed(Exception):
    def __init__(self, problem: Problem) -> None:
        super().__init__(problem.message)
        self.problem = problem


class _Halted(Exception):
    """The run is stopping: leave this product where it is."""


@dataclass
class StreamItem:
    index: int
    name: str
    source: Path
    photos: list[Path] = field(default_factory=list)  # a ready-photo folder's images
    seed: seeds.Seed | None = None
    status: str = QUEUED
    steps: dict[str, str] = field(default_factory=lambda: {step: TODO for step in STEPS})
    mode: str = ""  # composited | as_is | photos
    title: str = ""
    tags: list[str] = field(default_factory=list)
    description: str = ""
    images: list[Path] = field(default_factory=list)
    flat: Path | None = None  # the plain design among `images` (not a mockup)
    evidence: list[str] = field(default_factory=list)
    warnings: list[Problem] = field(default_factory=list)
    error: Problem | None = None
    listing_id: int | None = None
    images_uploaded: int = 0
    deliverables: list[Path] = field(default_factory=list)  # a digital product's downloads
    files_uploaded: int = 0
    csv_data: dict[str, Any] | None = None  # the review.csv row
    row: dict[str, str] | None = None  # the same row as `listings push` reads it back
    csv_line: int | None = None
    market: MarketReport | None = None
    alts: dict[str, str] = field(default_factory=dict)  # image path -> its alt text
    upscale: float = 0.0  # the most a design was enlarged onto a mockup or flat render
    stamped: set[str] = field(default_factory=set)  # watermarked images (_image_key)
    unstamped: list[Path] = field(default_factory=list)  # what they were stamped from
    info: list[Path] = field(default_factory=list)  # the shop's info images, after `images`

    @property
    def kind(self) -> str:
        return "folder" if self.source.is_dir() else "design"

    @property
    def finished(self) -> bool:
        return self.status in FINAL


@dataclass
class StreamReport:
    batch: str
    out_dir: Path
    dry_run: bool = False
    csv_path: Path | None = None
    items: list[StreamItem] = field(default_factory=list)
    already_done: list[str] = field(default_factory=list)
    needs_review: list[str] = field(default_factory=list)
    cancelled: bool = False
    stopped: Problem | None = None  # why the remaining products were not attempted
    researched: int = 0
    cached: int = 0
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def count(self, *statuses: str) -> int:
        return sum(1 for item in self.items if item.status in statuses)

    @property
    def created(self) -> int:
        return self.count(OK, PARTIAL)

    @property
    def errors(self) -> int:
        return self.count(FAILED)

    @property
    def warnings(self) -> int:
        return sum(len(item.warnings) for item in self.items)


def check_template(template: Template) -> None:
    """Refuse a template that could not make a single draft, before any work starts.

    Every draft copies these fields, so a bad price or a missing category would fail
    each product the same way, one after another. Raised as ValidationError. Any of
    Etsy's three types is fine: physical, download (no shipping profile needed) and
    both; the download files come from each product (see the module doc).
    """
    listing_type = template.fields.get("type") or "physical"
    if listing_type not in LISTING_TYPES:
        raise ValidationError(
            f"The template listing's type {listing_type!r} is not one Etsy knows "
            f"({', '.join(LISTING_TYPES)}). Pick the template listing again."
        )
    data: dict[str, Any] = dict(template.fields)
    data.update(listing_id="", title="Example", description="Example", tags=[], state="",
                materials=template.materials, images=[])
    try:
        listings.build_payload(_as_strings(data), is_update=False)
    except ValidationError as exc:
        raise ValidationError(f"The template listing cannot make a draft: {exc}") from exc


def run_stream(
    workspace: Workspace,
    template: Template,
    client: Any,
    *,
    mockups: Sequence[Path],
    on_event: OnEvent | None = None,
    cancel: Any = None,
    concurrency: int = 3,
    include_flat: bool = True,
    sample: int = 200,
    use_cache: bool = True,
    dry_run: bool = False,
    opaque: str = OPAQUE_AS_IS,
    watermark: bool = True,
) -> StreamReport:
    """Create a draft for every product in 2-PRODUCTS the history has not seen yet.

    `mockups` are the templates to composite onto (normally `catalog.enabled_mockups`).
    `cancel` is anything with `is_set()`: once set, no new product is started and no new
    draft is created; the draft being created finishes. `dry_run` stops every product
    after the check step and needs no client (research is then skipped or cached).
    `opaque` says what becomes of a loose design without see-through pixels (OPAQUE_*).
    `watermark`: stamp the workspace's watermark when it is on and takes this template
    (drop.watermark.for_run); False never stamps. A watermark that is on but cannot be
    read stops the run before anything is sent.
    Raises ValidationError / UploadLocked for problems that stop the whole run before
    anything is sent; everything that concerns one product lands on that product.
    """
    return _Run(
        workspace, template, client, mockups=list(mockups), on_event=on_event, cancel=cancel,
        concurrency=concurrency, include_flat=include_flat, sample=sample,
        use_cache=use_cache, dry_run=dry_run, opaque=opaque, watermark=watermark,
    ).run()


def _as_strings(data: dict[str, Any]) -> dict[str, str]:
    """A row exactly as `listings push` reads it back from review.csv."""
    out: dict[str, str] = {}
    for key, value in data.items():
        if value is None:
            text = ""
        elif isinstance(value, bool):
            text = "true" if value else "false"
        elif isinstance(value, (list, tuple)):
            text = csvio.MULTI_SEP.join(str(v) for v in value)
        else:
            text = str(value)
        out[key] = text.strip()
    return out


def _never_arrived(exc: BaseException | None) -> bool:
    """True when Etsy provably did not act on the request."""
    if isinstance(exc, EtsyApiError):
        if 400 <= exc.status < 500:
            return True
        return exc.status == 0 and isinstance(
            exc.__cause__, (httpx.ConnectError, httpx.ConnectTimeout)
        )
    # The token could not be refreshed: raised before the request was sent.
    return isinstance(exc, AuthError)


def _fatal(exc: BaseException | None) -> Problem | None:
    """A failure that will hit every following product too: stop the run."""
    if isinstance(exc, AuthUnreachable):
        return Problem("offline", str(exc), "draft")
    if isinstance(exc, AuthError):
        return Problem("reconnect", str(exc), "draft")
    if not isinstance(exc, EtsyApiError):
        return None
    said = f"{exc.message} {exc.body}".lower()
    if exc.status == 0:
        return Problem("offline", exc.message, "draft")
    if exc.status == 401:
        return Problem("reconnect", exc.message, "draft")
    if exc.status == 403:
        return Problem("bad_keys" if "api key" in said else "forbidden", exc.message, "draft")
    if exc.status == 429:
        return Problem("rate_limited", exc.message, "draft")
    if exc.status >= 500:
        return Problem("etsy_down", exc.message, "draft", {"status": exc.status})
    return None


def _image_key(path: Any) -> str:
    """A picture's key in StreamItem.alts: its resolved path."""
    try:
        return str(Path(path).resolve())
    except OSError:
        return str(path)


# The catalog's colour words (Turkish display words) in English, for the alt texts.
_ENGLISH_COLOURS = {
    "Beyaz": "white", "Siyah": "black", "Lacivert": "navy", "Krem": "cream", "Gri": "gray",
    "Kırmızı": "red", "Bordo": "burgundy", "Mavi": "blue", "Yeşil": "green",
    "Haki": "olive", "Pembe": "pink", "Sarı": "yellow", "Turuncu": "orange",
    "Mor": "purple", "Kahverengi": "brown", "Bej": "beige", "Meşe": "oak", "Ceviz": "walnut",
}
# Other words a seller writes next to a colour ("Meşe çerçeve", "açık mavi").
_ENGLISH_WORDS = {"cerceve": "frame", "cerceveli": "framed", "acik": "light", "koyu": "dark",
                  "ahsap": "wood"}


def english_colour(colour: str) -> str:
    """A mockup's colour as an English alt text says it: "Beyaz" -> "white", "Meşe
    çerçeve" -> "oak frame". A colour typed in English stays as typed; one that cannot
    be put in English is left out ("")."""
    text = colour.strip()
    if not text:
        return ""

    def word(folded: str) -> str | None:
        for display, keys in catalog._COLOR_WORDS:
            if folded in keys or folded == catalog._fold(display):
                return _ENGLISH_COLOURS.get(display)
        return _ENGLISH_WORDS.get(folded)

    whole = word(catalog._fold(text))
    if whole:
        return whole
    parts = [word(w) for w in catalog._fold(text).split()]
    if parts and all(parts):
        return " ".join(dict.fromkeys(parts))  # "white white" once
    return text if text.isascii() else ""


def alt_text(concept: str, kind: str = "", colour: str = "", *, flat: bool = False,
             digital: bool = False) -> str:
    """The alt text Etsy stores with a picture: "Retro Mountain Sunset t-shirt, white".

    kind/colour: the mockup's product type and colour (catalog); neither for a seller's
    own photo, which is named by its concept only. flat: the plain design
    on white; digital: that render is a digital product's preview. At most ALT_TEXT_MAX
    characters.
    """
    name = " ".join(word[:1].upper() + word[1:] for word in concept.split())
    if flat:
        text = f"{name} {'digital download preview' if digital else 'design'}"
    else:
        noun = TYPE_NOUNS.get(kind, "")
        text = f"{name} {noun}" if noun else name
        shade = english_colour(colour) if colour else ""
        if shade:
            text = f"{text}, {shade}"
    return text.strip()[:ALT_TEXT_MAX].strip()


class _Recorder(automation.RecordedClient):
    """automation's history-writing client, plus what the stream needs to know."""

    def __init__(self, client, path, state, entry, on_image, on_file=None,
                 alts=None, stamped=None, info=None) -> None:
        super().__init__(client, path, state, entry, stamped=stamped, info=info)
        self.on_image = on_image
        self.on_file = on_file
        self.alts: dict[str, str] = dict(alts or {})
        self.created = False
        self.error: BaseException | None = None

    def create_draft_listing(self, fields):
        try:
            result = super().create_draft_listing(fields)
        except BaseException as exc:
            self.error = exc
            raise
        self.created = True
        return result

    def update_listing_inventory(self, listing_id, inventory):
        try:
            return super().update_listing_inventory(listing_id, inventory)
        except BaseException as exc:
            self.error = exc
            raise

    def upload_listing_image(self, listing_id, image, *, rank):
        # By the picture's own path only: an info image and a product photo may share a
        # file name (size.jpg), and each goes up with its own alt text.
        alt = self.alts.get(_image_key(image), "")
        try:
            result = super().upload_listing_image(listing_id, image, rank=rank, alt_text=alt)
        except BaseException as exc:
            self.error = exc
            raise
        self.on_image(rank)
        return result

    def upload_listing_file(self, listing_id, path, *, rank):
        try:
            result = super().upload_listing_file(listing_id, path, rank=rank)
        except BaseException as exc:
            self.error = exc
            raise
        if self.on_file is not None:
            self.on_file(rank)
        return result


class _Run:
    def __init__(self, workspace: Workspace, template: Template, client: Any, *,
                 mockups: list[Path], on_event: OnEvent | None, cancel: Any,
                 concurrency: int, include_flat: bool, sample: int, use_cache: bool,
                 dry_run: bool, opaque: str = OPAQUE_AS_IS, watermark: bool = True) -> None:
        self.ws = workspace
        self.template = template
        self.listing_type = template.fields.get("type") or "physical"
        self.digital = self.listing_type in DIGITAL_TYPES
        # A made-to-order digital listing needs no file (pipeline.deliverables).
        self.made_to_order = pipeline.made_to_order(template)
        # The seller's own words about the product: what it is (a mug, a printable) and
        # which claims are theirs to make (generate.hint_from).
        self.hint = generate.hint_from(template.source_title, template.tags,
                                       template.description, template.materials)
        self.client = client
        self.mockups = mockups
        self.on_event = on_event
        self.cancel = cancel
        self.concurrency = max(1, int(concurrency))
        self.include_flat = include_flat
        self.sample = sample
        self.use_cache = use_cache
        self.dry_run = dry_run
        if opaque not in OPAQUE_CHOICES:
            raise ValueError(f"opaque must be one of {OPAQUE_CHOICES}")
        self.opaque = opaque
        self.use_watermark = watermark
        self.mark: watermark_mod.Watermark | None = None
        batch = pipeline._batch_name()
        self.report = StreamReport(batch=batch, out_dir=workspace.drafts / batch, dry_run=dry_run)
        self.out_dir = self.report.out_dir
        self.csv_path = self.out_dir / pipeline.REVIEW_FILE
        self._lock = threading.RLock()
        self._halt_flag = threading.Event()
        self._taken: set[str] = set()
        # The stamped copies' names in the batch's one `watermarked` folder (every
        # product's, so two designs called sunset.jpg and sunset.jpeg never share one).
        self._stamped_names: set[str] = set()
        self._concept_locks: dict[str, threading.Lock] = {}
        self._markets: dict[str, tuple[MarketReport | None, Problem | None]] = {}
        self._research_down: Problem | None = None
        self.areas: dict[str, tuple[mockup.PrintArea, str]] = {}
        self.mockup_facts: dict[str, catalog.MockupInfo] = {}  # for the alt texts
        self.mockup_sizes: dict[str, tuple[int, int]] = {}
        self.inventory: dict[str, Any] | None = None
        self.state: dict = {}
        self.history: dict = {}
        self.history_file = automation.history_path(workspace.root)
        # The tags the shop's other drafts carry, so these do not repeat them
        # (generate.build_tags): the history's (read once, before the run adds to it), and
        # each product's of this run, which a product reads only once every product before
        # it has written its own, so the same folder gives the same tags however its
        # products were scheduled.
        self.prior_tags: list[list[str]] = []
        self._tag_lists: dict[int, list[str]] = {}
        self._settled: set[int] = set()
        self._ledger = threading.Condition()
        # The shop's info images (read in run()), and each one's name by its picture.
        self.info: list[infoimages.InfoImage] = []
        self.info_names: dict[str, str] = {}

    # --- the run ----------------------------------------------------------------

    def run(self) -> StreamReport:
        self.ws.require()
        if self.client is None and not self.dry_run:
            raise ValidationError("Connect your Etsy shop before uploading drafts.")
        check_template(self.template)
        if self.use_watermark:
            # Loaded once: a mark replaced during the run does not change its pictures.
            self.mark = watermark_mod.for_run(self.ws, self.listing_type)
        info = infoimages.load(self.ws)
        planned = len(self.mockups) + (1 if self.include_flat else 0) + len(info)
        if planned > MAX_LISTING_IMAGES:
            flat = " plus the flat render" if self.include_flat else ""
            extra = f" plus {len(info)} info image(s)" if info else ""
            raise ValidationError(
                f"{len(self.mockups)} mockup(s){flat}{extra} is {planned} images per listing, "
                f"and Etsy allows {MAX_LISTING_IMAGES}. Turn some mockups off."
            )
        with automation.upload_lock(self.ws.root):
            self.state = automation.load_history(self.history_file)
            shop = None if self.dry_run else str(self.client.shop_id())
            self.history = automation.shop_history(self.state, shop)
            self.prior_tags = automation.used_tags(self.history)
            groups = self.ws.product_groups()
            names = {path.name.casefold() for path, _ in groups}
            self.report.already_done, self.report.needs_review = automation.known_products(
                self.history, names
            )
            known = {name.casefold() for name in self.history}
            fresh = [(path, photos) for path, photos in groups if path.name.casefold() not in known]
            items = [
                StreamItem(index=i, name=path.name, source=path, photos=list(photos))
                for i, (path, photos) in enumerate(fresh)
            ]
            for item in items:
                folder = item.kind == "folder"
                item.seed = seeds.derive(
                    item.source / "IMG_0001.jpg" if folder else item.source,
                    folder_fallback=folder or item.source.parent != self.ws.products,
                )
            self.report.items = items
            if items:
                # Every draft carries them: one that cannot be used stops the run here,
                # before the plan goes out and before anything is sent.
                self.info, _notes = pipeline.ready_info_images(info, self.out_dir)
                self.info_names = {_image_key(i.path): i.name for i in self.info}
            if items and not self.dry_run:
                # Before the plan goes out: a template that is gone stops the run cleanly.
                self.inventory = self._template_inventory()
            self._emit_batch()
            if not items:
                self.report.finished_at = time.time()
                return self.report
            # Relative image paths in review.csv are resolved against this folder, and on
            # macOS/Linux a `..` only resolves through a folder that exists.
            self.out_dir.mkdir(parents=True, exist_ok=True)
            self.areas = catalog.effective_areas(self.ws)
            try:
                self.mockup_facts = catalog.load(self.ws)
            except (OSError, ValueError):
                self.mockup_facts = {}
            self.mockup_sizes = mockup.mockup_sizes(list(self.mockups))
            if not self.dry_run:
                self.state[shop] = self.history
            try:
                self._pipeline(items)
            finally:
                self._halt_flag.set()
                self._write_review()
                self.report.finished_at = time.time()
        self.report.cancelled = self._cancelled()
        return self.report

    def _template_inventory(self) -> dict[str, Any] | None:
        """The template listing's variations, copied onto every new draft."""
        if not self.template.source_listing_id:
            return None
        try:
            raw = self.client.listing_inventory(self.template.source_listing_id)
        except EtsyApiError as exc:
            if exc.status == 404:
                raise TemplateGone(
                    f"The template listing {self.template.source_listing_id} no longer exists "
                    "on Etsy. Pick a new template listing."
                ) from exc
            raise
        source = listings.inventory_for_copy(raw)
        return source if listings.has_variations(source) else None

    def _pipeline(self, items: list[StreamItem]) -> None:
        futures: dict[int, Future] = {}
        submitted = 0
        failures = 0
        with ThreadPoolExecutor(max_workers=self.concurrency,
                                thread_name_prefix="etsyotomasyon-drop") as pool:
            try:
                for item in items:
                    # Keep the next few products preparing while this one is created,
                    # but no further ahead: a stop must not leave a hundred composites.
                    while (submitted < len(items) and submitted <= item.index + self.concurrency
                           and not self._stopping()):
                        futures[submitted] = pool.submit(self._prepare, items[submitted])
                        submitted += 1
                    future = futures.get(item.index)
                    if future is not None and not future.cancelled():
                        future.result()  # _prepare never raises; this only waits
                    if item.status != WAITING:
                        if not item.finished:
                            self._set_cancelled(item)
                        continue
                    if self._stopping():
                        self._set_cancelled(item)
                        continue
                    if self.dry_run:
                        self._finish(item, CHECKED)
                        continue
                    if self._draft(item):
                        failures = 0
                    else:
                        failures += 1
                        if failures >= STOP_AFTER_FAILURES and self.report.stopped is None:
                            self.report.stopped = Problem(
                                "repeated_failures",
                                f"{failures} drafts failed one after another; the rest were "
                                "not attempted.",
                                "draft", {"n": failures},
                            )
            finally:
                self._halt_flag.set()
                for future in futures.values():
                    future.cancel()
        for item in items:
            if not item.finished:
                self._set_cancelled(item)

    def _stopping(self) -> bool:
        return self._cancelled() or self.report.stopped is not None or self._halt_flag.is_set()

    def _cancelled(self) -> bool:
        return bool(self.cancel is not None and self.cancel.is_set())

    def _check_halt(self) -> None:
        if self._stopping():
            raise _Halted()

    # --- events -----------------------------------------------------------------

    def _rel(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.ws.root.resolve()).as_posix()
        except (OSError, ValueError):
            return ""

    def _notify(self, name: str, step: str, status: str, data: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(name, step, status, data)
        except Exception:  # noqa: BLE001 — a display problem must never stop a run
            log.exception("drop stream event handler failed")

    def _emit_batch(self) -> None:
        with self._lock:
            self._notify("", "batch", RUNNING, {
                "index": -1,
                "batch": self.report.batch,
                "out_dir": self._rel(self.out_dir) or self.out_dir.name,
                "dry_run": self.dry_run,
                "listing_type": self.listing_type,
                "items": [
                    {"index": item.index, "name": item.name, "kind": item.kind,
                     # A folder without photos has no picture to show.
                     "source": (self._rel(item.photos[0]) if item.photos
                                else "" if item.kind == "folder" else self._rel(item.source)),
                     "files": len(item.photos) if item.kind == "folder" else 1,
                     "files_total": (len(pipeline.deliverables(
                         item.source, made_to_order=self.made_to_order)[0])
                                     if self.digital else 0)}
                    for item in self.report.items
                ],
                "already_done": list(self.report.already_done),
                "needs_review": list(self.report.needs_review),
                "info_images": [image.name for image in self.info],
            })

    def _step(self, item: StreamItem, step: str, status: str, **data: Any) -> None:
        with self._lock:
            item.steps[step] = status
            self._notify(item.name, step, status, {"index": item.index, **data})

    def _warn(self, item: StreamItem, code: str, message: str, step: str, **params: Any) -> Problem:
        problem = Problem(code, message, step, params)
        with self._lock:
            item.warnings.append(problem)
        return problem

    def _item_data(self, item: StreamItem) -> dict[str, Any]:
        return {
            "index": item.index,
            "steps": dict(item.steps),
            "title": item.title,
            "tags": list(item.tags),
            "images": [self._rel(p) for p in item.images],
            "flat": self._rel(item.flat) if item.flat is not None else None,
            "mode": item.mode,
            "listing_id": item.listing_id,
            "images_uploaded": item.images_uploaded,
            "deliverables": [self._rel(p) for p in item.deliverables],
            "files_uploaded": item.files_uploaded,
            "files_total": len(item.deliverables),
            "watermarked": len(item.stamped),
            "info_images": [self._rel(p) for p in item.info],
            "warnings": [w.to_dict() for w in item.warnings],
            "problem": item.error.to_dict() if item.error else None,
        }

    def _finish(self, item: StreamItem, status: str, problem: Problem | None = None) -> None:
        with self._lock:
            item.status = status
            if problem is not None:
                item.error = problem
            self._notify(item.name, "item", status, self._item_data(item))

    def _set_cancelled(self, item: StreamItem) -> None:
        with self._lock:
            if item.finished:
                return
            for step, state in item.steps.items():
                if state == RUNNING:
                    item.steps[step] = TODO
            problem = None
            if self.report.stopped is not None and not self._cancelled():
                reason = self.report.stopped
                problem = Problem("stopped", reason.message, reason.step,
                                  {"reason": reason.code, **reason.params})
            self._finish(item, CANCELLED, problem)

    def _fail(self, item: StreamItem, problem: Problem) -> None:
        with self._lock:
            if problem.step:
                item.steps[problem.step] = ERROR
            for step, state in item.steps.items():
                if state == RUNNING:
                    item.steps[step] = TODO
            self._finish(item, FAILED, problem)

    # --- steps 1-5 (worker threads) --------------------------------------------------

    def _prepare(self, item: StreamItem) -> None:
        """Steps 1-5. Never raises: the outcome lands on the item."""
        try:
            self._check_halt()
            with self._lock:
                item.status = ACTIVE
            if item.kind == "folder" and not item.photos:
                # Only a `dosyalar` folder, no photos: nothing to show buyers. Found by the
                # check, before any mockup, research or copy is spent on it.
                self._step(item, "check", RUNNING)
                raise _ProductFailed(Problem(
                    "no_photos", pipeline.no_photos_message(item.source), "check",
                    {"name": item.name, "folder": pipeline.FILES_DIRS[0]},
                ))
            self._images(item)
            self._check_halt()
            self._research(item)
            self._check_halt()
            self._copy(item)
            self._check_halt()
            self._check(item)
            with self._lock:
                item.status = WAITING
                self._notify(item.name, "item", WAITING, self._item_data(item))
        except _ProductFailed as exc:
            self._fail(item, exc.problem)
        except _Halted:
            self._set_cancelled(item)
        except Exception as exc:  # noqa: BLE001 — one bad file must not stop a batch
            log.exception("preparing %s failed", item.name)
            step = next((s for s, state in item.steps.items() if state == RUNNING), "mockup")
            self._fail(item, Problem("internal", str(exc), step))
        finally:
            with self._ledger:
                self._settled.add(item.index)
                self._ledger.notify_all()

    def _tags_so_far(self, item: StreamItem) -> list[list[str]]:
        """The tags of the shop's earlier drafts: the history's, then those of the products
        before this one in the folder, once each has been written (or can never be)."""
        with self._ledger:
            while not all(i in self._settled or i in self._tag_lists for i in range(item.index)):
                if self._stopping():
                    raise _Halted()
                self._ledger.wait(0.2)
            return [*self.prior_tags,
                    *(self._tag_lists[i] for i in range(item.index) if i in self._tag_lists)]

    def _output_name(self, source: Path, template_image: Path | None) -> str:
        with self._lock:
            return pipeline._output_name(source, template_image, self._taken)

    def _enlarged(self, item: StreamItem, design: Path, mockup_size: tuple[int, int] | None,
                  area: mockup.PrintArea | None, *, edge: int = mockup.OUTPUT_MIN_EDGE) -> None:
        """Remember how much `design` was enlarged (the check step warns past 2x)."""
        size = mockup.display_size(design)
        if size is None or (mockup_size is None and area is not None):
            return
        factor = mockup.upscale_factor(size, mockup_size, area, min_edge=edge)
        item.upscale = max(item.upscale, factor)

    def _images(self, item: StreamItem) -> None:
        """Step 1: composite onto the mockups, or take the product's own photos."""
        self._step(item, "mockup", RUNNING)
        seed = item.seed
        if not seed:
            raise _ProductFailed(Problem(
                "junk_name",
                (seed.reason if seed is not None else "")
                or "No product name could be read from the file name; rename the file.",
                "mockup",
            ))
        warned = len(item.warnings)
        # A design too large to decode safely (a 13000 px square needs gigabytes per
        # copy) is this product's problem, found before any work; the others go on.
        huge = pipeline.oversized(item.photos or [item.source])
        if huge is not None:
            path, (width, height) = huge
            raise _ProductFailed(Problem(
                "too_many_pixels",
                f"{path.name} is {width}x{height} px; images can be at most "
                f"{catalog.MAX_EDGE} px on a side and {catalog.MAX_PIXELS // 1_000_000} million "
                "pixels. Make it smaller.",
                "mockup",
                {"name": path.name, "width": width, "height": height,
                 "max_edge": catalog.MAX_EDGE, "max_mp": catalog.MAX_PIXELS // 1_000_000},
            ))
        images: list[Path] = []
        flat_image: Path | None = None
        alts: dict[Path, str] = {}  # each picture's alt text, before any conversion
        concept = item.seed.text
        design = item.source  # what goes onto the mockups (its ground removed, maybe)
        # A loose design with nothing to see through: a finished photo, or a design saved
        # on a solid background. Never for a digital template: the loose file is then the
        # download being sold (an opaque printable too), so it always goes onto the mockups.
        opaque, ground = False, None
        if not item.photos and not self.digital:
            background, colour = mockup.design_ground(item.source)  # one read of the file
            opaque = background != "transparent"
            ground = colour if self.opaque == OPAQUE_PLACE else None
        if item.photos:
            item.mode = "photos"
            if len(item.photos) > MAX_LISTING_IMAGES:
                raise _ProductFailed(Problem(
                    "too_many_images",
                    f"The product folder has {len(item.photos)} images and Etsy takes "
                    f"{MAX_LISTING_IMAGES}; nothing was dropped.",
                    "mockup", {"n": len(item.photos), "max": MAX_LISTING_IMAGES},
                ))
            bare = [p.name for p in item.photos if mockup.looks_like_artwork(p)]
            if bare:
                self._warn(
                    item, "transparent_photos",
                    f"{len(bare)} transparent file(s) in this folder are uploaded as they are, "
                    f"not composited onto a mockup ({', '.join(bare[:3])}).",
                    "mockup", n=len(bare), names=", ".join(bare[:3]),
                )
            images = list(item.photos)
            alts = {photo: alt_text(concept) for photo in images}
        elif opaque and ground is None:
            # A finished product photo needs no compositing; it goes up as it is, and the
            # row says so (a design saved without transparency would otherwise land on no
            # mockup without a word).
            item.mode = "as_is"
            images = [item.source]
            alts = {item.source: alt_text(concept)}
            self._warn(item, "as_is",
                       f"{item.source.name} has no transparent background, so it was not "
                       "placed on the mockups: it goes up as it is.", "mockup",
                       name=item.source.name)
        else:
            if ground is not None:
                # The seller chose to place designs saved on a solid background: the
                # background joined to the edges becomes see-through, then as usual.
                keyed = self.out_dir / item.source.stem / (
                    f"{item.source.stem}-{item.source.suffix.lstrip('.').lower()}-transparent.png")
                design = mockup.remove_ground(item.source, keyed, ground)
            item.mode = "composited"
            if not self.mockups:
                self._warn(item, "no_mockups",
                           "No mockups are turned on, so only the flat design goes up.", "mockup")
            for template_image in self.mockups:
                self._check_halt()
                area = self.areas.get(template_image.name, (mockup.DEFAULT_PRINT_AREA, ""))[0]
                out = self.out_dir / self._output_name(item.source, template_image)
                facts = self.mockup_facts.get(template_image.name)
                kind, colour = ((facts.type, facts.color) if facts is not None
                                else catalog.guess(template_image.name))
                try:
                    images.append(mockup.compose(design, template_image, out, area=area,
                                                 kind=kind))
                except Exception as exc:  # noqa: BLE001 — one mockup must not stop the product
                    self._warn(item, "mockup_failed", f"mockup {template_image.name} failed: {exc}",
                               "mockup", mockup=template_image.name)
                    continue
                alts[images[-1]] = alt_text(concept, kind, colour)
                self._enlarged(item, design, self.mockup_sizes.get(template_image.name), area)
                self._step(item, "mockup", RUNNING, images=[self._rel(p) for p in images],
                           mode=item.mode)
            if self.include_flat:
                self._check_halt()
                flat = self.out_dir / self._output_name(item.source, None)
                # A digital design's flat render is a preview, not a copy of the download.
                edge = mockup.DIGITAL_PREVIEW_EDGE if self.digital else mockup.OUTPUT_MIN_EDGE
                try:
                    flat_image = mockup.flatten_design(design, flat, edge=edge)
                    images.append(flat_image)
                    alts[flat_image] = alt_text(concept, flat=True, digital=self.digital)
                    self._enlarged(item, design, None, None, edge=edge)
                except Exception as exc:  # noqa: BLE001
                    self._warn(item, "flat_failed", f"flat render failed: {exc}", "mockup")

        # Etsy takes JPG, PNG and GIF only; anything else is converted, and said so. A
        # file over Etsy's 20 MB limit is made smaller, and said so too.
        convert_dir = self.out_dir / (item.source.name if item.photos else item.source.stem)
        # One folder for the batch's stamped copies, each name taken once: a design's
        # name is in the path once (Windows' 260-character limit), and two products never
        # write the same file.
        stamped_dir = self.out_dir / watermark_mod.STAMPED_DIR
        uploadable: list[Path] = []
        for image in images:
            try:
                converted = mockup.to_uploadable(image, convert_dir)
            except Exception as exc:  # noqa: BLE001
                self._warn(item, "convert_failed",
                           f"{image.name} could not be converted for Etsy: {exc}", "mockup",
                           name=image.name)
                continue
            if converted != image:
                self._warn(item, "converted",
                           f"{image.name} was converted to {converted.name}: Etsy accepts only "
                           "JPG, PNG and GIF listing images", "mockup",
                           name=image.name, to=converted.name)
            fit_dir = convert_dir
            if self.mark is not None:
                # A copy with the mark: the picture itself (a seller's photo in
                # 2-PRODUCTS, a composite) stays as it is. Never up without the mark.
                self._check_halt()
                try:
                    stamped = self.mark.stamp(converted, stamped_dir, self._stamped_names)
                except Exception as exc:  # noqa: BLE001 — this product only
                    raise _ProductFailed(Problem(
                        "watermark_failed",
                        f"The watermark could not be put on {image.name}: {exc}",
                        "mockup", {"name": image.name},
                    )) from exc
                item.unstamped.append(converted)
                converted, fit_dir = stamped, stamped_dir
            try:
                size = converted.stat().st_size
                fitted = pipeline.fit_for_etsy(converted, fit_dir)
            except Exception as exc:  # noqa: BLE001
                self._warn(item, "convert_failed",
                           f"{converted.name} could not be made smaller for Etsy: {exc}",
                           "mockup", name=converted.name)
                continue
            if fitted != converted:
                self._warn(item, "shrunk",
                           f"{converted.name} was {size / 1024 / 1024:.1f} MB, over Etsy's 20 MB "
                           f"image limit; it was made smaller as {fitted.name}", "mockup",
                           name=converted.name, mb=round(size / 1024 / 1024, 1))
            if image == flat_image:
                item.flat = fitted
            if alts.get(image):
                item.alts[_image_key(fitted)] = alts[image]
            if self.mark is not None:
                item.stamped.add(_image_key(fitted))
            uploadable.append(fitted)
        if not uploadable:
            raise _ProductFailed(Problem(
                "no_images",
                "No images were produced. Turn on a mockup, or drop a finished product photo "
                "instead of transparent artwork.",
                "mockup",
            ))
        item.images = uploadable
        # The shop's info images close the listing, after its own pictures, with their
        # own alt texts; a folder whose photos leave too little room gets the first ones.
        info = infoimages.fitting(len(uploadable), self.info)
        if len(info) < len(self.info):
            self._warn(item, "info_images_cut", pipeline.info_note(len(info), len(self.info)),
                       "mockup", n=len(info), total=len(self.info), max=MAX_LISTING_IMAGES)
        item.info = [image.path for image in info]
        for image in info:
            if image.alt:
                item.alts[_image_key(image.path)] = image.alt
        self._step(item, "mockup", WARN if len(item.warnings) > warned else DONE,
                   images=[self._rel(p) for p in uploadable], mode=item.mode,
                   flat=self._rel(item.flat) if item.flat is not None else None,
                   watermarked=len(item.stamped),
                   info_images=[self._rel(p) for p in item.info])

    def _market(self, concept: str) -> tuple[MarketReport | None, Problem | None]:
        """One research per concept, however many products share it."""
        with self._lock:
            lock = self._concept_locks.setdefault(concept, threading.Lock())
        with lock:
            with self._lock:
                if concept in self._markets:
                    return self._markets[concept]
                down = self._research_down
            if down is not None:
                result: tuple[MarketReport | None, Problem | None] = (None, down)
            else:
                try:
                    if self.client is not None and hasattr(self.client, "attempts"):
                        # Research is optional: a dead connection must not hold every
                        # product for minutes of retries.
                        with self.client.attempts(2):
                            market, cached = pipeline._research_concept(
                                self.client, concept, sample=self.sample, use_cache=self.use_cache)
                    else:
                        market, cached = pipeline._research_concept(
                            self.client, concept, sample=self.sample, use_cache=self.use_cache)
                    result = (market, None)
                    with self._lock:
                        if cached:
                            self.report.cached += 1
                        elif market is not None:
                            self.report.researched += 1
                except Exception as exc:  # noqa: BLE001 — research is optional
                    problem = Problem("research_failed", f"market research failed: {exc}",
                                      "research")
                    result = (None, problem)
                    if isinstance(exc, (EtsyApiError, AuthError)) and (
                        getattr(exc, "status", 0) in (0, 401, 403) or isinstance(exc, AuthError)
                    ):
                        with self._lock:
                            self._research_down = problem
            with self._lock:
                self._markets[concept] = result
            return result

    def _research(self, item: StreamItem) -> None:
        """Step 2: what the listings that rank for this concept have in common."""
        self._step(item, "research", RUNNING)
        assert item.seed is not None
        # The concept, plus the template's product when the concept does not name it.
        concept = generate.research_keyword(item.seed, self.hint)
        market, problem = self._market(concept)
        item.market = market
        if problem is not None:
            self._warn(item, "no_market_data", problem.message, "research", concept=concept)
            self._step(item, "research", WARN)
        elif market is None or market.empty:
            self._warn(item, "no_market_data",
                       f"no market data for {concept!r}; tags come from the filename and your "
                       "template only", "research", concept=concept)
            self._step(item, "research", WARN)
        else:
            item.evidence.append(f"{market.sampled} listings ranking for {concept!r}")
            if market.sampled < THIN_SAMPLE:
                self._warn(item, "thin_market",
                           f"only {market.sampled} listing(s) rank for {concept!r}", "research",
                           n=market.sampled, concept=concept)
                self._step(item, "research", WARN, sampled=market.sampled)
            else:
                self._step(item, "research", DONE, sampled=market.sampled)

    def _copy(self, item: StreamItem) -> None:
        """Steps 3 and 4: the title, then thirteen tags and the description."""
        assert item.seed is not None
        self._step(item, "title", RUNNING)
        built = generate.build_title(item.seed, item.market, product_hint=self.hint)
        # Etsy refuses a title with an emoji or a "$", or with a second "&" (see
        # pipeline.TITLE_ONCE); a file name can carry any of them into the concept.
        item.title = pipeline.clean_title(built)
        if not item.title:
            raise _ProductFailed(Problem(
                "invalid_title",
                f"No title Etsy accepts could be made from {item.name!r}; rename the file.",
                "title",
            ))
        if item.title != built:
            self._warn(item, "title_cleaned",
                       "characters Etsy does not accept were taken out of the title", "title")
            self._step(item, "title", WARN, title=item.title)
        else:
            self._step(item, "title", DONE, title=item.title)

        self._check_halt()
        self._step(item, "tags", RUNNING)
        taken = self._tags_so_far(item)
        tags = generate.build_tags(item.seed, item.market, product_hint=self.hint,
                                   **({"taken": taken} if taken else {}))
        # A few of the template's own tags are in them already (the shop-wide kind, at most
        # three); free slots take more of those that suit any design of its product, not
        # the ones about the template's own design (generate.product_tags).
        filler = generate.product_tags(self.template.tags, self.template.source_title,
                                       item.seed, hint=self.hint)
        generate.fill_tags(tags, filler)
        template_keys = {tag_key(t) for t in self.template.tags}
        if any(tag_key(t) in template_keys for t in tags):
            item.evidence.append("your template listing's tags")
        item.tags = tags[:MAX_TAGS]
        with self._ledger:
            self._tag_lists[item.index] = list(item.tags)
            self._ledger.notify_all()
        item.description = generate.build_description(
            item.seed, self.template.description, item.title,
            source_title=self.template.source_title,
            description_template=self.template.description_template,
        )
        warned = False
        if self.template.description_template is None:
            # No description template saved: the template listing's own description, its
            # title replaced. A sentence about its design ("lemons") still reaches
            # this draft, so each product says so (Şablon İlan's description template).
            left = description.leftover(
                self.template.description, self.template.source_title, self.template.tags,
                seed=item.seed, title=item.title, product_words=self.template.category_path,
            )
            if left:
                self._warn(item, "description_design", description.leftover_message(left),
                           "tags", n=len(left),
                           words=", ".join(description.flag_words(left)[:4]))
                warned = True
        if len(item.tags) < MAX_TAGS:
            self._warn(item, "few_tags",
                       f"{len(item.tags)}/{MAX_TAGS} tags — the rest could not be filled honestly",
                       "tags", n=len(item.tags), max=MAX_TAGS)
            warned = True
        self._step(item, "tags", WARN if warned else DONE, tags=list(item.tags))

    def _check(self, item: StreamItem) -> None:
        """Step 5: the row `listings push` will send, validated, and every image decoded.

        A digital product's download files are found and checked here too, before its
        draft is created: none, too many, a program, an empty or an oversized file is
        this product's error, with the file's own code.
        """
        assert item.seed is not None
        self._step(item, "check", RUNNING)
        warned = False
        if self.digital:
            files, issue = pipeline.deliverables(item.source, made_to_order=self.made_to_order)
            # A listing photo that is also the download would give the product away (the
            # pictures a watermarked copy was made from are checked too).
            issue = issue or pipeline.photo_is_download([*item.images, *item.unstamped], files)
            if issue is not None:
                code, message, params = issue
                raise _ProductFailed(Problem(code, message, "check", dict(params)))
            with self._lock:
                item.deliverables = files
            if not files and self.made_to_order:
                self._warn(item, "made_to_order_no_file", pipeline.MADE_TO_ORDER_NOTE, "check")
                warned = True
        drop_row = pipeline.DropRow(
            source=item.source, seed=item.seed, title=item.title, tags=list(item.tags),
            description=item.description, images=[*item.images, *item.info],
            evidence=list(item.evidence), warnings=[w.message for w in item.warnings],
            files=list(item.deliverables),
        )
        data = pipeline._to_csv_row(drop_row, self.template, self.out_dir)
        row = _as_strings(data)
        prepared = listings.prepare([row], base_dir=self.out_dir)[0]
        if prepared.result.failed:
            raise _ProductFailed(Problem("invalid_row", prepared.result.message, "check"))
        title_problems = pipeline.title_problems(item.title)
        if title_problems:
            raise _ProductFailed(Problem("invalid_title", "; ".join(title_problems), "check"))
        for message in prepared.result.warnings:
            if message.startswith("no shipping_profile_id"):
                code = "no_shipping_profile"
            elif message.startswith("type is download, so nothing is shipped"):
                code = "not_shipped"  # a digital template with parcel values left in it
            elif message.startswith(("item_weight", "item_length", "item_width", "item_height")):
                code = "measure_not_sent"  # a weight or size Etsy refuses (0, or no unit)
            else:
                code = "check_warning"
            self._warn(item, code, message, "check")
            warned = True
        # Image.verify() is a no-op for JPEG; load() is a real decode. A file cut short by
        # a half-finished copy fails here, not after its draft already exists. The info
        # images were decoded once, before the plan (run()).
        for image in prepared.image_paths:
            if _image_key(image) in self.info_names:
                continue
            try:
                with Image.open(image) as opened:
                    opened.load()
            except (OSError, ValueError, Image.DecompressionBombError) as exc:
                raise _ProductFailed(Problem(
                    "invalid_image", f"Invalid image {image.name}: {exc}", "check",
                    {"name": image.name},
                )) from exc
        if self._small_pictures(item):
            warned = True
        with self._lock:
            item.csv_data = data
            item.row = row
        self._step(item, "check", WARN if warned else DONE,
                   deliverables=[self._rel(p) for p in item.deliverables])

    def _small_pictures(self, item: StreamItem) -> bool:
        """Warn (never fail) about pictures that would look soft on Etsy: a short side
        under SMALL_IMAGE_EDGE px, or a design enlarged past MAX_UPSCALE."""
        warned = False
        for image in item.images:
            size = mockup.display_size(image)
            if size is not None and min(size) < SMALL_IMAGE_EDGE:
                self._warn(item, "small_image",
                           f"{image.name} is {size[0]}x{size[1]} px; Etsy recommends at least "
                           "2000 px on the short side, so it may look soft.", "check",
                           name=image.name, px=min(size), min=SMALL_IMAGE_EDGE)
                warned = True
        if item.upscale > mockup.MAX_UPSCALE:
            size = mockup.display_size(item.source) or (0, 0)
            self._warn(item, "design_small",
                       f"{item.source.name} ({size[0]}x{size[1]} px) was enlarged "
                       f"{item.upscale:.1f}x to fill the print area; it may look soft.",
                       "check", name=item.source.name, px=min(size),
                       factor=round(item.upscale, 1))
            warned = True
        return warned

    # --- step 6 (the calling thread) ------------------------------------------------------

    def _write_review(self) -> None:
        """review.csv of every checked product so far, in folder order: the record."""
        with self._lock:
            rows = [item for item in self.report.items if item.csv_data is not None]
            for line, item in enumerate(rows, start=2):
                item.csv_line = line
            if not rows:
                return
            try:
                csvio.write_rows(
                    self.csv_path, [item.csv_data for item in rows],  # type: ignore[misc]
                    columns=pipeline.REVIEW_COLUMNS,
                )
                self.report.csv_path = self.csv_path
            except OSError:
                log.warning("could not write %s", self.csv_path)

    def _draft(self, item: StreamItem) -> bool:
        """Create one draft. False when it failed (for the stop-after-failures rule)."""
        assert item.row is not None
        if item.name.casefold() in {name.casefold() for name in self.history}:
            # The history already has it: never a second draft.
            self._fail(item, Problem("duplicate", "This product was already attempted.", "draft"))
            return True
        self._write_review()
        total = len(item.images) + len(item.info)  # its own pictures, then the info images
        files_total = len(item.deliverables)
        counts = {"images_total": total, "files_total": files_total}
        self._step(item, "draft", RUNNING, images_uploaded=0, files_uploaded=0, **counts)
        entry = {"status": "pending", "listing_id": None, "images_uploaded": 0,
                 "files_uploaded": 0, "review_csv": str(self.csv_path),
                 "tags": list(item.tags), **counts}
        if item.info:
            # Which of the pictures are the shop's info images: their names here, and
            # (the recorder, as each goes up) their Etsy image ids in "info_image_ids",
            # by which the İlanlar detail knows them apart from the product's own.
            entry["info_images"] = [p.name for p in item.info]
        self.history[item.name] = entry
        # Persist intent BEFORE the request, including ambiguous network failures. A
        # history that cannot be saved stops the whole run (ValidationError).
        automation.save_history(self.history_file, self.state)

        def on_image(rank: int) -> None:
            item.images_uploaded = rank
            self._step(item, "draft", RUNNING, images_uploaded=rank, files_uploaded=0,
                       **counts)

        def on_file(rank: int) -> None:
            item.files_uploaded = rank
            self._step(item, "draft", RUNNING, images_uploaded=item.images_uploaded,
                       files_uploaded=rank, **counts)

        recorder = _Recorder(self.client, self.history_file, self.state, entry, on_image,
                             on_file, alts=item.alts, stamped=item.stamped, info=item.info)
        try:
            result = listings.push(recorder, [item.row], base_dir=self.out_dir,
                                   inventory=self.inventory).results[0]
        except (AuthError, EtsyApiError) as exc:
            # push() lets a failed token refresh through (it is raised before a request
            # is sent). Before the create nothing exists; after it, the draft does.
            recorder.error = exc
            # What went up before it is what the history recorded along the way.
            result = listings.RowResult(row=item.csv_line or 2, action="create",
                                        status="partial" if recorder.created else "error",
                                        listing_id=entry.get("listing_id"), message=str(exc),
                                        images_uploaded=entry.get("images_uploaded", 0),
                                        files_uploaded=entry.get("files_uploaded", 0))
        except ValidationError:
            raise  # the history could not be saved: stop everything
        except Exception as exc:  # noqa: BLE001 — unknown state: keep the entry, stop the run
            log.exception("creating the draft for %s failed", item.name)
            entry.update(status="error", message=str(exc))
            automation.save_history(self.history_file, self.state)
            self.report.stopped = Problem("internal", str(exc), "draft")
            self._fail(item, Problem("draft_uncertain", str(exc), "draft"))
            return False

        result.row = item.csv_line or result.row
        item.listing_id = result.listing_id or entry.get("listing_id")
        item.images_uploaded = result.images_uploaded
        item.files_uploaded = result.files_uploaded
        entry.update(status=result.status, message=result.message,
                     files_uploaded=result.files_uploaded)
        if item.listing_id:
            # The draft exists: its design goes to print-files/<id>, so an order of it
            # can be printed even after the design leaves 2-PRODUCTS.
            printfiles.keep(self.ws.root, item.listing_id, item.source)
        refused = (result.status == "error" and not recorder.created
                   and _never_arrived(recorder.error))
        if refused:
            # Etsy provably did nothing, so the product may simply be tried again later.
            self.history.pop(item.name, None)
        automation.save_history(self.history_file, self.state)

        fatal = _fatal(recorder.error) if result.status != "ok" else None
        if fatal is not None and self.report.stopped is None:
            self.report.stopped = fatal

        uploaded = {"images_uploaded": item.images_uploaded,
                    "files_uploaded": item.files_uploaded, **counts}
        if result.status == "ok":
            self._step(item, "draft", DONE, listing_id=item.listing_id, **uploaded)
            self._finish(item, OK)
            return True
        if result.status == "partial":
            if files_total and item.files_uploaded < files_total:
                # A download file is not on the draft (a photo may be missing too; the
                # message says which). Said as such: the draft cannot be published until
                # the seller adds the file in Etsy.
                missing = item.deliverables[item.files_uploaded]
                self._warn(item, "partial_files", result.message, "draft",
                           listing_id=item.listing_id, n=item.files_uploaded,
                           total=files_total, name=missing.name)
            else:
                self._warn(item, "partial", result.message, "draft",
                           listing_id=item.listing_id)
            self._step(item, "draft", WARN, listing_id=item.listing_id, **uploaded)
            self._finish(item, PARTIAL)
            return False
        code = "draft_refused" if refused else "draft_uncertain"
        self._fail(item, Problem(code, result.message, "draft",
                                 {"status": getattr(recorder.error, "status", None)}))
        return False


def item_summary(item: StreamItem, root: Path) -> dict[str, Any]:
    """A JSON-able view of one product (for a screen or a saved run)."""
    def rel(path: Path) -> str:
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except (OSError, ValueError):
            return ""

    return {
        "index": item.index,
        "name": item.name,
        "kind": item.kind,
        "status": item.status,
        "steps": dict(item.steps),
        "mode": item.mode,
        "title": item.title,
        "tags": list(item.tags),
        "images": [rel(p) for p in item.images],
        "flat": rel(item.flat) if item.flat is not None else None,
        "listing_id": item.listing_id,
        "deliverables": [rel(p) for p in item.deliverables],
        "files_uploaded": item.files_uploaded,
        "watermarked": len(item.stamped),
        "info_images": [rel(p) for p in item.info],
        "warnings": [w.to_dict() for w in item.warnings],
        "error": item.error.to_dict() if item.error else None,
    }


__all__ = [
    "STEPS", "Problem", "StreamItem", "StreamReport", "TemplateGone", "check_template",
    "item_summary", "run_stream",
]

