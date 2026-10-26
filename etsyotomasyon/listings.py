"""Bulk listing operations: export current listings, and create/update from a CSV.

Everything is validated locally before a single request goes out. A 400 from Etsy
costs a round trip and tells you very little; a local check tells you the row number
and the exact field. Rows are independent — one bad row does not stop the batch.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .client import MAX_LISTING_FILES, EtsyClient, file_problem, image_problem
from .config import (
    LISTING_TYPES,
    MAX_LISTING_IMAGES,
    MAX_MATERIAL_LEN,
    MAX_MATERIALS,
    MAX_QUANTITY,
    MAX_TAG_LEN,
    MAX_TAGS,
    MAX_TITLE_LEN,
    WHEN_MADE,
    WHO_MADE,
)
from .csvio import as_bool, as_float, as_int, resolve_paths, split_multi
from .errors import EtsyApiError, ValidationError
from .seo import tag_key  # seo imports only client and config: no cycle

# The CSV contract. `listing_id` empty means "create"; filled means "update".
LISTING_COLUMNS = [
    "listing_id",
    "title",
    "description",
    "price",
    "quantity",
    "who_made",
    "when_made",
    "taxonomy_id",
    "type",
    "tags",
    "materials",
    "shipping_profile_id",
    "return_policy_id",
    "shop_section_id",
    "readiness_state_id",
    "processing_min",
    "processing_max",
    "is_supply",
    "is_customizable",
    "is_taxable",
    "should_auto_renew",
    "item_weight",
    "item_weight_unit",
    "item_length",
    "item_width",
    "item_height",
    "item_dimensions_unit",
    "images",
    "files",
    "state",
]

# The column after `images`: the files a buyer downloads from a new `download` or `both`
# draft, separated like images, relative to the CSV. Empty for a physical listing. It is
# optional to read: a CSV written before it existed pushes exactly as before.
FILES_COLUMN = "files"

# Listing types that are delivered as files, and the ones that are shipped.
DIGITAL_TYPES = frozenset({"download", "both"})
SHIPPED_TYPES = frozenset({"physical", "both"})

# What a digital download is never sent, because nothing is shipped: Etsy's
# createDraftListing requires shipping_profile_id only "when listing type is physical",
# and a processing profile is only returned for physical listings (OAS).
SHIPPING_ONLY_FIELDS = (
    "shipping_profile_id", "readiness_state_id", "processing_min", "processing_max",
    "item_weight", "item_weight_unit", "item_length", "item_width", "item_height",
    "item_dimensions_unit",
)

# Etsy's title rule (createDraftListing in the Open API spec): "valid title strings
# contain only letters, numbers, punctuation marks, mathematical symbols, whitespace
# characters, ™, ©, and ®" — regex /[^\p{L}\p{Nd}\p{P}\p{Sm}\p{Zs}™©®]/u — and "you can
# only use the %, :, & and + characters once each". The drop pipeline cleans its titles
# with the same rule (drop.pipeline re-exports these); a CSV is checked with it here.
TITLE_SYMBOLS = "™©®"
TITLE_ONCE = {"&": "and", "+": "plus", "%": "percent", ":": "-"}

# Fields Etsy accepts on PATCH. `quantity` and `price` are deliberately absent:
# on a listing with variations they live in the inventory endpoint, and sending
# them here would silently flatten a seller's variation pricing.
UPDATABLE = {
    "title", "description", "tags", "materials", "taxonomy_id", "who_made", "when_made",
    "shipping_profile_id", "return_policy_id", "shop_section_id", "type", "is_supply",
    "is_taxable", "should_auto_renew", "item_weight", "item_weight_unit", "item_length",
    "item_width", "item_height", "item_dimensions_unit", "state",
}

# Etsy's tag charset: letters, digits, whitespace, and this handful of symbols.
_TAG_EXTRA = set(" -'™©®")

_WEIGHT_UNITS = {"oz", "lb", "g", "kg"}
_DIMENSION_UNITS = {"in", "ft", "mm", "cm", "m", "yd", "inches"}

# Every state a listing can report. Only two of them can be *set* through the API —
# the rest come back from `listings pull` and must round-trip without failing.
LISTING_STATES = {"active", "inactive", "draft", "expired", "sold_out"}
SETTABLE_STATES = {"active", "inactive"}


@dataclass
class RowResult:
    row: int
    action: str = "skip"
    status: str = "ok"
    listing_id: int | None = None
    title: str = ""
    message: str = ""
    images_uploaded: int = 0
    files_uploaded: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        """Only 'error' is a failure.

        'partial' means the listing was created and an image upload failed after it,
        so the row wrote something real and must not be counted as nothing happening.
        """
        return self.status == "error"

    @property
    def wrote(self) -> bool:
        return self.status in WROTE_STATUSES


# Statuses where a request actually reached Etsy and changed something.
WROTE_STATUSES = frozenset({"ok", "partial"})


@dataclass
class PushReport:
    results: list[RowResult] = field(default_factory=list)
    aborted: bool = False
    aborted_reason: str = ""

    @property
    def created(self) -> int:
        return sum(1 for r in self.results if r.action == "create" and r.wrote)

    @property
    def updated(self) -> int:
        return sum(1 for r in self.results if r.action == "update" and r.wrote)

    @property
    def partial(self) -> int:
        return sum(1 for r in self.results if r.status == "partial")

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.status == "skipped")

    @property
    def errors(self) -> int:
        return sum(1 for r in self.results if r.failed)

    @property
    def images(self) -> int:
        return sum(r.images_uploaded for r in self.results)

    @property
    def files(self) -> int:
        return sum(r.files_uploaded for r in self.results)


def title_char_ok(ch: str) -> bool:
    """Whether Etsy accepts this character in a title (see TITLE_SYMBOLS)."""
    if ch in TITLE_SYMBOLS:
        return True
    category = unicodedata.category(ch)
    return category[0] in "LP" or category in ("Nd", "Sm", "Zs")


def title_problems(title: str) -> list[str]:
    """Why Etsy would refuse this title's characters (the length is checked elsewhere)."""
    problems: list[str] = []
    bad = sorted({ch for ch in title if not title_char_ok(ch)})
    if bad:
        problems.append(
            "title contains characters Etsy does not accept: " + " ".join(repr(c) for c in bad)
        )
    for ch in TITLE_ONCE:
        count = title.count(ch)
        if count > 1:
            problems.append(f"title uses {ch!r} {count} times; Etsy allows it once")
    return problems


def bad_tag_chars(tag: str) -> set[str]:
    """Characters Etsy will reject. str.isalnum() is Unicode-aware, so 'çiçek' passes."""
    return {ch for ch in tag if not (ch.isalnum() or ch in _TAG_EXTRA)}


def validate_tags(tags: Sequence[str]) -> list[str]:
    problems = []
    if len(tags) > MAX_TAGS:
        problems.append(f"{len(tags)} tags given, Etsy allows {MAX_TAGS}")
    for tag in tags:
        if len(tag) > MAX_TAG_LEN:
            problems.append(f"tag {tag!r} is {len(tag)} chars, max {MAX_TAG_LEN}")
        bad = bad_tag_chars(tag)
        if bad:
            problems.append(f"tag {tag!r} contains disallowed character(s): {''.join(sorted(bad))}")
    # "İstanbul poster" and "istanbul poster" are one tag to a buyer (seo.tag_key).
    lowered = [tag_key(t) for t in tags]
    dupes = {t for t in lowered if lowered.count(t) > 1}
    if dupes:
        problems.append(f"duplicate tags: {', '.join(sorted(dupes))}")
    return problems


_WEIGHT_FIELDS = ("item_weight",)
_DIMENSION_FIELDS = ("item_length", "item_width", "item_height")


def _drop_unusable_measures(payload: dict[str, Any], warnings: list[str] | None) -> None:
    """Leave out weight and size values Etsy would refuse, instead of failing the listing.

    Etsy takes a weight or a size only when it is above 0 and comes with its unit (OAS:
    "If set, the value must be greater than 0"). Listings made on etsy.com often report
    0 for a weight nobody entered, and a template copied from one used to send that 0
    on every draft, which Etsy refused. A value without its unit is left out too, and so
    is a unit without any value.
    """
    notes: list[str] = []
    for name in _WEIGHT_FIELDS + _DIMENSION_FIELDS:
        if name in payload and not payload[name] > 0:
            del payload[name]
            notes.append(f"{name} 0 not sent (Etsy needs a value above 0)")
    for fields, unit in ((_WEIGHT_FIELDS, "item_weight_unit"), (_DIMENSION_FIELDS, "item_dimensions_unit")):
        present = [name for name in fields if name in payload]
        if present and unit not in payload:
            for name in present:
                del payload[name]
            notes.append(f"{', '.join(present)} not sent: {unit} is empty")
        elif unit in payload and not present:
            del payload[unit]
    if notes and warnings is not None:
        warnings.extend(notes)


def build_payload(
    row: dict[str, str], *, is_update: bool, warnings: list[str] | None = None
) -> dict[str, Any]:
    """Turn one CSV row into an Etsy payload, raising ValidationError with a precise reason.

    Non-fatal observations are appended to `warnings` when a list is supplied.
    """
    problems: list[str] = []
    payload: dict[str, Any] = {}

    title = row.get("title", "").strip()
    if title:
        if len(title) > MAX_TITLE_LEN:
            problems.append(f"title is {len(title)} chars, max {MAX_TITLE_LEN}")
        # The character rule too: Etsy refuses "Salt & Pepper & Co" or an emoji with a 400
        # that names neither, after the row was counted as sent.
        problems.extend(title_problems(title))
        payload["title"] = title
    elif not is_update:
        problems.append("title is required")

    description = row.get("description", "").strip()
    if description:
        payload["description"] = description
    elif not is_update:
        problems.append("description is required")

    listing_type = (row.get("type") or "").strip().lower()
    if listing_type:
        if listing_type not in LISTING_TYPES:
            problems.append(f"type must be one of {', '.join(LISTING_TYPES)}")
        payload["type"] = listing_type
    elif not is_update:
        listing_type = "physical"
        payload["type"] = listing_type

    # Lower bounds matter: a negative price or a negative stock level is nonsense
    # Etsy would reject anyway, and the whole point of validating here is that the
    # seller learns it before the request instead of after it.
    try:
        price = as_float(row.get("price", ""), "price", required=not is_update, minimum=0)
    except ValidationError as exc:
        price = None
        problems.append(str(exc))
    if price is not None:
        if price <= 0:
            problems.append("price must be greater than 0")
        else:
            payload["price"] = price

    for name, minimum, required in (
        ("quantity", 0, not is_update),
        ("taxonomy_id", 1, not is_update),
        ("shipping_profile_id", 1, False),
        ("return_policy_id", 1, False),
        ("shop_section_id", 1, False),
        ("readiness_state_id", 1, False),
        ("processing_min", 0, False),
        ("processing_max", 0, False),
    ):
        try:
            value = as_int(row.get(name, ""), name, required=required, minimum=minimum)
        except ValidationError as exc:
            problems.append(str(exc))
            continue
        if value is not None:
            payload[name] = value

    if payload.get("quantity", 0) > MAX_QUANTITY:
        problems.append(
            f"quantity {payload['quantity']} is over Etsy's limit of {MAX_QUANTITY}"
        )

    # A processing profile (readiness_state_id) is how Etsy now states processing time,
    # and it is required on a physical create. When one is given it is the single source
    # of truth, so the older day counts are not sent alongside it to contradict it.
    if "readiness_state_id" in payload:
        payload.pop("processing_min", None)
        payload.pop("processing_max", None)

    for name in ("item_weight", "item_length", "item_width", "item_height"):
        try:
            value = as_float(row.get(name, ""), name, minimum=0)
        except ValidationError as exc:
            problems.append(str(exc))
            continue
        if value is not None:
            payload[name] = value

    for name in ("is_supply", "is_customizable", "is_taxable", "should_auto_renew"):
        try:
            value = as_bool(row.get(name, ""), name)
        except ValidationError as exc:
            problems.append(str(exc))
            continue
        if value is not None:
            payload[name] = value

    who = (row.get("who_made") or "").strip().lower()
    if who:
        if who not in WHO_MADE:
            problems.append(f"who_made must be one of {', '.join(WHO_MADE)}")
        payload["who_made"] = who
    elif not is_update:
        problems.append("who_made is required")

    when = (row.get("when_made") or "").strip().lower()
    if when:
        if when not in WHEN_MADE:
            problems.append(f"when_made {when!r} is not a valid Etsy period")
        payload["when_made"] = when
    elif not is_update:
        problems.append("when_made is required")

    weight_unit = (row.get("item_weight_unit") or "").strip().lower()
    if weight_unit:
        if weight_unit not in _WEIGHT_UNITS:
            problems.append(f"item_weight_unit must be one of {', '.join(sorted(_WEIGHT_UNITS))}")
        payload["item_weight_unit"] = weight_unit

    dim_unit = (row.get("item_dimensions_unit") or "").strip().lower()
    if dim_unit:
        if dim_unit not in _DIMENSION_UNITS:
            problems.append(f"item_dimensions_unit must be one of {', '.join(sorted(_DIMENSION_UNITS))}")
        payload["item_dimensions_unit"] = dim_unit

    _drop_unusable_measures(payload, warnings)

    tags = split_multi(row.get("tags", ""), allow_comma=True)
    if tags:
        problems.extend(validate_tags(tags))
        payload["tags"] = tags

    materials = split_multi(row.get("materials", ""))
    if materials:
        if len(materials) > MAX_MATERIALS:
            problems.append(f"{len(materials)} materials given, Etsy allows {MAX_MATERIALS}")
        for material in materials:
            if len(material) > MAX_MATERIAL_LEN:
                problems.append(f"material {material!r} exceeds {MAX_MATERIAL_LEN} chars")
        payload["materials"] = materials

    state = (row.get("state") or "").strip().lower()
    if state:
        if state not in LISTING_STATES:
            problems.append(f"state {state!r} is not an Etsy listing state")
        elif state in SETTABLE_STATES:
            if not is_update:
                problems.append("state can only be set when updating an existing listing")
            else:
                payload["state"] = state
        # draft / expired / sold_out are readable but not settable. `listings pull`
        # writes them, so silently ignore them rather than failing a round trip on
        # this tool's own output.

    # A digital download is not shipped: Etsy requires a shipping profile only "when
    # listing type is physical" and a processing profile only exists for physical
    # listings (OAS), so a new download draft is created without them. A row that fills
    # them in (a physical CSV turned into a printable) is told what was left out.
    if not is_update and payload.get("type") == "download":
        dropped = [name for name in SHIPPING_ONLY_FIELDS if name in payload]
        for name in dropped:
            del payload[name]
        if dropped and warnings is not None:
            warnings.append(
                f"type is download, so nothing is shipped: {', '.join(dropped)} not sent"
            )

    # etsyotomasyon only ever creates drafts, and Etsy does not require a shipping profile
    # on a draft — so this is a warning, not a blocker. A seller who has not built a
    # profile yet can still stage 300 drafts; they just cannot publish them.
    if (
        not is_update
        and payload.get("type", "physical") in SHIPPED_TYPES
        and "shipping_profile_id" not in payload
        and warnings is not None
    ):
        warnings.append(
            "no shipping_profile_id — fine for a draft, but you cannot publish "
            "without one (see `etsyotomasyon shop profiles`)"
        )

    if is_update:
        # Etsy's updateListing accepts a narrower set of fields than createDraftListing,
        # and price/quantity are held back on purpose. Dropping them silently means a
        # seller edits a price in their spreadsheet, pushes, sees "updated", and finds
        # nothing changed — so say which fields are going nowhere.
        ignored = sorted(k for k in payload if k not in UPDATABLE)
        payload = {k: v for k, v in payload.items() if k in UPDATABLE}
        if ignored and warnings is not None:
            warnings.append(
                f"not sent on an update, so unchanged: {', '.join(ignored)}"
                + (
                    " — price and quantity live in Etsy's inventory endpoint and are "
                    "held back so they cannot flatten variation pricing"
                    if {"price", "quantity"} & set(ignored)
                    else ""
                )
            )
        if not payload:
            problems.append("no updatable fields present in this row")

    if problems:
        raise ValidationError("; ".join(problems))
    return payload


@dataclass
class PreparedRow:
    """One CSV row, validated locally and ready to send — or already known bad."""

    result: RowResult
    is_update: bool = False
    payload: dict[str, Any] = field(default_factory=dict)
    image_paths: list[Path] = field(default_factory=list)
    file_paths: list[Path] = field(default_factory=list)  # a digital draft's downloads


ImageResolver = Callable[[str], Path]
"""Turns one `images` cell value into a path, or raises ValidationError to refuse it.

The CLI reads a CSV the seller keeps next to their pictures, so it takes any path
(csvio.resolve_paths). The web app's CSV arrives as an upload with no folder of its
own and may come from anywhere, so it passes a resolver that keeps every image inside
the workspace and refuses the rest before the filesystem is touched.
"""


def _image_paths(
    values: list[str], base_dir: Path, resolver: ImageResolver | None
) -> list[Path]:
    if resolver is None:
        return resolve_paths(values, base_dir)
    return [resolver(value) for value in values]


def _file_paths(
    row: dict[str, str],
    payload: dict[str, Any],
    result: RowResult,
    *,
    is_update: bool,
    base_dir: Path,
    resolver: ImageResolver | None,
) -> list[Path]:
    """The `files` column of one row, validated; ValidationError says why it cannot go.

    Files go only onto a NEW draft of type download or both. On an update they are
    left out with a warning: attaching a file to a live physical listing would turn it
    digital and drop its shipping and variations (OAS uploadListingFile).
    """
    values = split_multi(row.get(FILES_COLUMN, "") or "")
    listing_type = payload.get("type", "physical" if not is_update else None)
    if not values:
        # A made-to-order digital listing is activated without a file (updateListing
        # in the Open API spec): the seller sends what was made after the order.
        if (not is_update and listing_type in DIGITAL_TYPES
                and payload.get("when_made") != "made_to_order"):
            result.warnings.append(
                f"type is {listing_type} but no files are given — the draft is created, "
                "but Etsy needs its download file before it can be published"
            )
        return []
    if is_update:
        result.warnings.append(
            "files are only attached to new drafts; not sent on an update (add them in Etsy)"
        )
        return []
    if listing_type not in DIGITAL_TYPES:
        raise ValidationError(
            "files are given but type is physical — set type to download or both, or "
            "empty the files column"
        )
    paths = _image_paths(values, base_dir, resolver)
    if len(paths) > MAX_LISTING_FILES:
        raise ValidationError(
            f"{len(paths)} files given, Etsy allows {MAX_LISTING_FILES} per listing. "
            "Nothing was dropped — zip them together or remove some."
        )
    missing = [p for p in paths if not p.is_file()]
    if missing:
        raise ValidationError(f"file not found: {', '.join(str(p) for p in missing[:3])}")
    rejected = [problem for p in paths if (problem := file_problem(p))]
    if rejected:
        raise ValidationError("; ".join(rejected[:3]))
    return paths


def prepare(
    rows: Sequence[dict[str, str]],
    *,
    base_dir: Path,
    upload_images: bool = True,
    image_resolver: ImageResolver | None = None,
    upload_files: bool = True,
) -> list[PreparedRow]:
    """Validate every row. Pure local work — no network, no writes, no side effects.

    `upload_files` covers the optional `files` column (a digital draft's downloads);
    the same resolver keeps those paths where it keeps the images.
    """
    prepared: list[PreparedRow] = []

    for index, row in enumerate(rows, start=2):  # row 1 is the header
        result = RowResult(row=index, title=row.get("title", "")[:60])

        try:
            listing_id = as_int(row.get("listing_id", ""), "listing_id", minimum=1)
        except ValidationError as exc:
            result.status = "error"
            result.message = str(exc)
            prepared.append(PreparedRow(result))
            continue

        result.listing_id = listing_id
        is_update = listing_id is not None
        result.action = "update" if is_update else "create"

        try:
            payload = build_payload(row, is_update=is_update, warnings=result.warnings)
        except ValidationError as exc:
            result.status = "error"
            result.message = str(exc)
            prepared.append(PreparedRow(result, is_update))
            continue

        # Only the run that will actually upload them cares how many there are, or
        # whether they exist. Under --no-images nothing is sent, and failing a row over
        # a column this run ignores would stop a seller fixing their titles.
        if upload_images:
            try:
                image_paths = _image_paths(
                    split_multi(row.get("images", "")), base_dir, image_resolver
                )
            except ValidationError as exc:
                result.status = "error"
                result.message = str(exc)
                prepared.append(PreparedRow(result, is_update, payload))
                continue
            if len(image_paths) > MAX_LISTING_IMAGES:
                # Etsy accepts the create and then refuses the eleventh upload, leaving
                # a draft etsyotomasyon has no delete scope to undo — the one state this tool
                # cannot get you out of. Which ten go up is the seller's call, so the row
                # fails here rather than losing the last one on the way out.
                result.status = "error"
                result.message = (
                    f"{len(image_paths)} images given, Etsy allows {MAX_LISTING_IMAGES}. "
                    "Nothing was dropped — remove the extra ones yourself."
                )
                prepared.append(PreparedRow(result, is_update, payload, image_paths))
                continue
            missing = [p for p in image_paths if not p.is_file()]
            if missing:
                result.status = "error"
                result.message = f"image not found: {', '.join(str(p) for p in missing[:3])}"
                prepared.append(PreparedRow(result, is_update, payload, image_paths))
                continue
            # Format and size belong here, with every other check. The draft is created
            # first and the images go up after it, so an image Etsy refuses does not
            # fail the row — it leaves a half-built listing in the shop for the seller
            # to finish by hand. --dry-run is only worth running if it knows this.
            rejected = [problem for p in image_paths if (problem := image_problem(p))]
            if rejected:
                result.status = "error"
                result.message = "; ".join(rejected[:3])
                prepared.append(PreparedRow(result, is_update, payload, image_paths))
                continue
        else:
            image_paths = []

        # The downloads are checked here with the images, for the same reason: they go
        # up after the draft exists, so a file Etsy refuses must be found before it.
        file_paths: list[Path] = []
        if upload_files:
            try:
                file_paths = _file_paths(row, payload, result, is_update=is_update,
                                         base_dir=base_dir, resolver=image_resolver)
            except ValidationError as exc:
                result.status = "error"
                result.message = str(exc)
                prepared.append(PreparedRow(result, is_update, payload, image_paths))
                continue

        prepared.append(PreparedRow(result, is_update, payload, image_paths, file_paths))

    return prepared


def inventory_for_copy(
    inventory: dict[str, Any], *, readiness_state_id: int | None = None
) -> dict[str, Any]:
    """Turn a getListingInventory response into an updateListingInventory body.

    The two are nearly the same shape and not quite: the read carries ids the write
    must not echo (product_id, offering_id), prices come back as a Money object and go
    in as a plain number, and deleted offerings are listed but must not be recreated.
    Everything that defines the options — properties, their values and which of them
    drive price, quantity, SKU and processing time — is carried across unchanged.

    `readiness_state_id` on every offering is required by updateListingInventory
    (OAS: required [price, quantity, is_enabled, readiness_state_id], nullable), but
    getListingInventory only returns it for an active physical listing. An offering
    without one gets `readiness_state_id` (the template listing's processing profile)
    here, or None, which `_write_row` fills from the new draft's own profile.
    """
    products = []
    for product in inventory.get("products") or []:
        if product.get("is_deleted"):
            continue
        offerings = []
        for offering in product.get("offerings") or []:
            if offering.get("is_deleted"):
                continue
            price = offering.get("price")
            if isinstance(price, dict):
                price = price.get("amount", 0) / (price.get("divisor") or 100)
            readiness = offering.get("readiness_state_id")
            entry = {
                "price": round(float(price), 2),
                "quantity": int(offering.get("quantity") or 0),
                "is_enabled": bool(offering.get("is_enabled", True)),
                "readiness_state_id": readiness if readiness is not None else readiness_state_id,
            }
            offerings.append(entry)
        if not offerings:
            continue
        values = [
            {
                key: value[key]
                for key in ("property_id", "value_ids", "scale_id", "property_name", "values")
                if value.get(key) is not None
            }
            for value in product.get("property_values") or []
        ]
        products.append(
            {"sku": product.get("sku") or "", "property_values": values, "offerings": offerings}
        )
    if not products:
        raise ValidationError("That listing has no inventory to copy.")
    body: dict[str, Any] = {"products": products}
    for key in (
        "price_on_property", "quantity_on_property", "sku_on_property",
        "readiness_state_on_property",
    ):
        if inventory.get(key) is not None:
            body[key] = inventory[key]
    return body


def has_variations(inventory: dict[str, Any] | None) -> bool:
    return bool(inventory) and any(p.get("property_values") for p in inventory["products"])


def inventory_with_readiness(
    inventory: dict[str, Any], readiness_state_id: Any
) -> dict[str, Any]:
    """A copy of an updateListingInventory body whose offerings all name a profile.

    An offering that has none takes the new listing's own `readiness_state_id` (the one
    its create payload carried); every offering keeps the key, None at worst, because
    the endpoint requires it. The shared body is never changed: one run copies it onto
    many drafts.
    """
    products = []
    for product in inventory.get("products") or []:
        offerings = []
        for offering in product.get("offerings") or []:
            entry = dict(offering)
            if entry.get("readiness_state_id") is None:
                entry["readiness_state_id"] = readiness_state_id
            offerings.append(entry)
        products.append({**product, "offerings": offerings})
    return {**inventory, "products": products}


def push(
    client: EtsyClient | None,
    rows: Sequence[dict[str, str]],
    *,
    base_dir: Path,
    dry_run: bool = False,
    upload_images: bool = True,
    allow_partial: bool = False,
    on_progress: Callable[[RowResult], None] | None = None,
    inventory: dict[str, Any] | None = None,
    image_resolver: ImageResolver | None = None,
    upload_files: bool = True,
) -> PushReport:
    """Apply a CSV to the shop.

    The whole file is validated before anything is sent. If any row is bad the run
    stops with nothing written, because the alternative — discovering row 40 is
    invalid after rows 1-39 became real drafts — leaves a shop half-populated from a
    file the seller would never have pushed. `allow_partial` opts back into
    row-by-row behaviour.

    `client` may be None when dry_run is set: validation is entirely local, so a
    seller still waiting on API approval can check their file.
    """
    if client is None and not dry_run:
        raise ValidationError("A client is required unless dry_run is set.")

    report = PushReport()
    prepared = prepare(
        rows, base_dir=base_dir, upload_images=upload_images, image_resolver=image_resolver,
        upload_files=upload_files,
    )
    invalid = [p for p in prepared if p.result.failed]

    if dry_run:
        for item in prepared:
            if not item.result.failed:
                item.result.status = "dry-run"
                item.result.message = (
                    f"{len(item.payload)} fields, {len(item.image_paths)} image(s)"
                    + (f", {len(item.file_paths)} file(s)" if item.file_paths else "")
                )
            _emit(report, item.result, on_progress)
        return report

    if invalid and not allow_partial:
        report.aborted = True
        report.aborted_reason = (
            f"{len(invalid)} of {len(prepared)} row(s) failed validation. "
            "Nothing was sent. Fix them, or re-run with --partial to push the valid rows."
        )
        for item in prepared:
            if not item.result.failed:
                item.result.status = "skipped"
                item.result.message = "not sent — another row in this file is invalid"
            _emit(report, item.result, on_progress)
        return report

    for item in prepared:
        if not item.result.failed:
            _write_row(  # type: ignore[arg-type]
                client, item, upload_images=upload_images, inventory=inventory
            )
        _emit(report, item.result, on_progress)

    return report


def _emit(
    report: PushReport, result: RowResult, on_progress: Callable[[RowResult], None] | None
) -> None:
    report.results.append(result)
    if on_progress:
        on_progress(result)


def _write_row(
    client: EtsyClient,
    item: PreparedRow,
    *,
    upload_images: bool,
    inventory: dict[str, Any] | None = None,
) -> None:
    result = item.result
    try:
        if item.is_update:
            client.update_listing(result.listing_id, item.payload)  # type: ignore[arg-type]
            result.message = "updated"
        else:
            created = client.create_draft_listing(item.payload)
            raw_id = created.get("listing_id") if isinstance(created, dict) else None
            try:
                result.listing_id = int(raw_id)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                # Etsy said yes but did not say to what. A draft may well exist, so this
                # is reported as needing a look rather than as a failure to retry.
                result.status = "error"
                result.message = (
                    "Etsy accepted the create but returned no listing id — a draft may "
                    "exist. Check your drafts before running this row again."
                )
                return
            result.message = "created as draft"
    except EtsyApiError as exc:
        result.status = "error"
        hint = exc.hint()
        result.message = f"{exc.message}{' — ' + hint if hint else ''}"
        return
    except (OSError, KeyError, ValueError) as exc:
        result.status = "error"
        result.message = str(exc)
        return

    # Variations go on before the images: a draft with the right options and some photos
    # missing is closer to done than the other way round. Only new drafts get them — an
    # update never replaces options a seller may have tuned by hand.
    if inventory and not item.is_update and result.listing_id:
        try:
            client.update_listing_inventory(
                result.listing_id,
                inventory_with_readiness(inventory, item.payload.get("readiness_state_id")),
            )
            result.message = f"{result.message} with {len(inventory['products'])} variations"
        except (EtsyApiError, ValidationError, OSError, ValueError) as exc:
            result.status = "partial"
            result.message = (
                f"{result.message} (id {result.listing_id}), but its variations could not "
                f"be set: {exc}. The listing IS in your shop — add the options in Etsy."
            )

    image_failed = False
    if upload_images and item.image_paths and result.listing_id:
        for rank, image in enumerate(item.image_paths, start=1):
            try:
                client.upload_listing_image(result.listing_id, image, rank=rank)
                result.images_uploaded += 1
            except (EtsyApiError, ValidationError, OSError, ValueError) as exc:
                # The listing already exists. Reporting a plain "error" would send the
                # seller hunting for a draft they already have — and etsyotomasyon holds no
                # delete scope on purpose, so there is nothing to roll back to.
                result.status = "partial"
                result.message = (
                    f"{result.message} (id {result.listing_id}), but image {rank} of "
                    f"{len(item.image_paths)} failed: {exc}. The listing IS in your shop — "
                    "add the remaining images in Etsy, or fix and re-run just this row."
                )
                image_failed = True
                break

    # A digital draft's downloads go on last, after its photos: only ever onto the draft
    # this row just created (prepare() refuses files anywhere else). They still go when a
    # photo failed: a draft with its download and a photo missing is closer to done, and
    # the message then says what happened to the download as well.
    if item.is_update or not item.file_paths or not result.listing_id:
        return
    total = len(item.file_paths)
    for rank, path in enumerate(item.file_paths, start=1):
        try:
            client.upload_listing_file(result.listing_id, path, rank=rank)
            result.files_uploaded += 1
        except (EtsyApiError, ValidationError, OSError, ValueError) as exc:
            if image_failed:
                result.message = (
                    f"{result.message} Download file {rank} of {total} ({path.name}) "
                    f"failed too: {exc}. Add the download file in Etsy before publishing it."
                )
            else:
                result.status = "partial"
                result.message = (
                    f"{result.message} (id {result.listing_id}), but file {rank} of "
                    f"{total} ({path.name}) failed: {exc}. The listing IS in "
                    "your shop — add the download file in Etsy before publishing it."
                )
            return
    one = result.files_uploaded == 1
    if result.status == "ok":
        result.message = (
            f"{result.message}; {result.files_uploaded} download file{'' if one else 's'} "
            "attached"
        )
    elif image_failed:
        result.message = (
            f"{result.message} Its {result.files_uploaded} download file{'' if one else 's'} "
            f"{'was' if one else 'were'} attached."
        )


def pull(client: EtsyClient, *, state: str = "active", max_items: int | None = None) -> list[dict[str, Any]]:
    """Export listings into the same CSV shape that `push` consumes."""
    rows = []
    for listing in client.listings_by_shop(state=state, max_items=max_items):
        price = listing.get("price") or {}
        amount = price.get("amount")
        divisor = price.get("divisor") or 100
        rows.append(
            {
                "listing_id": listing.get("listing_id"),
                "title": listing.get("title", ""),
                "description": listing.get("description", ""),
                "price": round(amount / divisor, 2) if isinstance(amount, (int, float)) else "",
                "quantity": listing.get("quantity", ""),
                "who_made": listing.get("who_made", ""),
                "when_made": listing.get("when_made", ""),
                "taxonomy_id": listing.get("taxonomy_id", ""),
                "type": listing.get("listing_type", ""),
                "tags": listing.get("tags") or [],
                "materials": listing.get("materials") or [],
                "shipping_profile_id": listing.get("shipping_profile_id") or "",
                "return_policy_id": listing.get("return_policy_id") or "",
                "shop_section_id": listing.get("shop_section_id") or "",
                "readiness_state_id": listing.get("readiness_state_id") or "",
                "processing_min": listing.get("processing_min") or "",
                "processing_max": listing.get("processing_max") or "",
                "is_supply": listing.get("is_supply"),
                "is_customizable": listing.get("is_customizable"),
                "is_taxable": listing.get("is_taxable"),
                "should_auto_renew": listing.get("should_auto_renew"),
                "item_weight": listing.get("item_weight") or "",
                "item_weight_unit": listing.get("item_weight_unit") or "",
                "item_length": listing.get("item_length") or "",
                "item_width": listing.get("item_width") or "",
                "item_height": listing.get("item_height") or "",
                "item_dimensions_unit": listing.get("item_dimensions_unit") or "",
                "images": "",  # Etsy serves images by URL; re-uploading them is never wanted.
                "state": listing.get("state", ""),
                "url": listing.get("url", ""),
                # No "views": ShopListing (OAS) has no such field, so it was always empty.
                "num_favorers": listing.get("num_favorers", ""),
            }
        )
    return rows
