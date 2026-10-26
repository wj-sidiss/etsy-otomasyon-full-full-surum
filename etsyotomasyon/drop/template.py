"""The listing every later draft copies its settings from.

Some fields simply cannot be derived from an image. `taxonomy_id`,
`shipping_profile_id`, `return_policy_id`, `who_made`, `when_made`, processing times,
the shop section, the price — these are decisions about a business, not facts about a
picture. Guessing them would put wrong listings in a real shop.

So the seller builds one listing properly in Etsy, by hand, and etsyotomasyon copies it.
That is the whole mechanism, and it is why there is no six-question wizard here.

The listing's type travels with it: a `physical` template makes physical drafts, a
`download` template digital ones (no shipping profile needed) and `both` drafts that
ship and download. Which file a buyer downloads is not a setting — it is the design
itself, or a product folder's `dosyalar` subfolder (see drop.pipeline.deliverables).

The listing's description is about its own design, so a draft does not copy it as it is
(see drop.description): the seller may save a description template with placeholders
(`description_template`), and the category's names (`category_path`, saved by Şablon
İlan) say which words are about the product rather than one design.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from ..client import unescape_text
from ..config import LISTING_TYPES, MAX_QUANTITY, WHEN_MADE, WHO_MADE
from ..errors import ValidationError
from ..listings import DIGITAL_TYPES, SHIPPING_ONLY_FIELDS

# product.json files saved before 0.3.0 hold the listing's text exactly as Etsy sent it,
# HTML-escaped ("Mom&#39;s Mug &amp; Gift"), and every draft built from one would copy
# the entities onto Etsy. The text is now decoded where it is read from Etsy, and
# to_dict marks the file with this key. from_dict decodes the text of an unmarked file
# (once, as it reads it) and leaves a marked one alone, so a seller's own literal
# "&amp;" in a newer file stays what they typed.
PLAIN_TEXT = "plain_text"
# The seller's description template and the category's names (see Template).
DESCRIPTION_TEMPLATE = "description_template"
CATEGORY_PATH = "category_path"

# download / both drafts get the product's files (listings.DIGITAL_TYPES); physical never.
TYPE_NAMES = {
    "physical": "Physical product",
    "download": "Digital download (the design file is attached for buyers)",
    "both": "Physical and digital (shipped, plus a download file)",
}

# Copied verbatim onto every draft. Anything not in this list is derived per product.
INHERITED_FIELDS = (
    "taxonomy_id",
    "shipping_profile_id",
    "return_policy_id",
    "shop_section_id",
    "readiness_state_id",
    "who_made",
    "when_made",
    "type",
    "price",
    "quantity",
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
)

WEIGHT_FIELDS = ("item_weight",)
DIMENSION_FIELDS = ("item_length", "item_width", "item_height")


def clean_measures(fields: dict[str, Any]) -> None:
    """Leave out, in place, a weight or size that cannot go on a draft.

    Etsy reports 0 for a weight or size nobody entered and refuses 0 on a new listing
    ("If set, the value must be greater than 0"). A value that is not a number above 0
    is dropped, then a unit with no value left, and a value with no unit. A 0 means "not
    entered", so nothing is said about it. A product.json captured before this rule
    still holds such a 0: from_dict cleans it too, so it acts like a new capture.
    """
    for name in WEIGHT_FIELDS + DIMENSION_FIELDS:
        if name not in fields:
            continue
        value = fields[name]
        try:
            usable = not isinstance(value, bool) and float(value) > 0
        except (TypeError, ValueError):
            usable = False
        if not usable:
            del fields[name]
    for names, unit in ((WEIGHT_FIELDS, "item_weight_unit"),
                        (DIMENSION_FIELDS, "item_dimensions_unit")):
        present = [name for name in names if name in fields]
        if not present or not fields.get(unit):
            fields.pop(unit, None)
            for name in present:
                del fields[name]


def _description_template(data: dict[str, Any]) -> str | None:
    """The saved description template: a text with something in it, else None."""
    value = data.get(DESCRIPTION_TEMPLATE)
    if isinstance(value, str) and value.strip():
        return value
    return None


@dataclass
class Template:
    """Settings lifted from a real listing, plus what it teaches about copy."""

    source_listing_id: int
    source_title: str = ""
    fields: dict[str, Any] = field(default_factory=dict)
    materials: list[str] = field(default_factory=list)
    description: str = ""
    tags: list[str] = field(default_factory=list)
    # The seller's description template ({başlık}, {tasarım}); None: none saved, and a
    # draft gets the description with the template's title replaced (drop.description).
    description_template: str | None = None
    # The template's Etsy category, root first ("Home & Living", ..., "Wallpaper"): every
    # draft copies it, so its words are never about one design. Empty when not known.
    category_path: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "source_listing_id": self.source_listing_id,
            "source_title": self.source_title,
            "fields": self.fields,
            "materials": self.materials,
            "description": self.description,
            "tags": self.tags,
            PLAIN_TEXT: True,
        }
        if self.description_template is not None:
            data[DESCRIPTION_TEMPLATE] = self.description_template
        if self.category_path:
            data[CATEGORY_PATH] = list(self.category_path)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Template:
        if not isinstance(data, dict):
            raise ValidationError("product.json is malformed: not a JSON object")
        plain = data.get(PLAIN_TEXT) is True

        def text(value: Any) -> Any:
            return value if plain else unescape_text(value)

        try:
            fields = dict(data.get("fields") or {})
            clean_measures(fields)
            return cls(
                source_listing_id=int(data["source_listing_id"]),
                source_title=str(text(data.get("source_title", ""))),
                fields=fields,
                materials=[text(m) for m in data.get("materials") or []],
                description=str(text(data.get("description", ""))),
                tags=[text(t) for t in data.get("tags") or []],
                # Written by etsyotomasyon as plain text: never decoded.
                description_template=_description_template(data),
                category_path=[str(name) for name in data.get(CATEGORY_PATH) or []
                               if isinstance(name, str) and name.strip()],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError(f"product.json is malformed: {exc}") from exc

    @property
    def listing_type(self) -> str:
        """physical | download | both (as captured; physical when the template has none)."""
        return str(self.fields.get("type") or "physical")

    @property
    def section_id(self) -> int | None:
        """The shop section the template listing is in (None: none, or not a usable id)."""
        return section_number(self.fields.get("shop_section_id"))

    def with_section(self, section_id: int | None) -> Template:
        """A copy whose drafts go into shop section `section_id` (None: into none).

        The run's choice (the app's Başlat window, `drop auto/run --section`) for every
        draft of that run; the template itself (product.json) is not changed.
        """
        fields = dict(self.fields)
        if section_id is None:
            fields.pop("shop_section_id", None)
        else:
            fields["shop_section_id"] = int(section_id)
        return replace(self, fields=fields, materials=list(self.materials),
                       tags=list(self.tags))

    @property
    def digital(self) -> bool:
        """Its drafts carry download files: type download or both."""
        return self.listing_type in DIGITAL_TYPES

    def missing_for_a_physical_draft(self) -> list[str]:
        """What would stop these settings producing a publishable listing."""
        gaps = []
        if not self.fields.get("taxonomy_id"):
            gaps.append("taxonomy_id")
        if self.fields.get("type", "physical") in {"physical", "both"}:
            if not self.fields.get("shipping_profile_id"):
                gaps.append("shipping_profile_id")
        if not self.fields.get("price"):
            gaps.append("price")
        return gaps

    def describe(self) -> list[tuple[str, str]]:
        """Plain-language rows for the confirmation screen."""
        f = self.fields
        rows = [
            ("Copied from", f"listing {self.source_listing_id} — {self.source_title[:60]}"),
            ("Type", TYPE_NAMES.get(self.listing_type, self.listing_type)),
            ("Category", str(f.get("taxonomy_id", "—"))),
            ("Shipping profile", str(f.get("shipping_profile_id", "—"))),
            ("Return policy", str(f.get("return_policy_id", "—"))),
            ("Shop section", str(f.get("shop_section_id", "—"))),
            ("Price", f"{f.get('price', '—')}"),
            ("Quantity", str(f.get("quantity", "—"))),
            ("Who made it", str(f.get("who_made", "—"))),
            ("When made", str(f.get("when_made", "—"))),
            (
                "Processing",
                f"{f.get('processing_min', '?')}–{f.get('processing_max', '?')} days",
            ),
            ("Materials", ", ".join(self.materials) or "—"),
            ("Its tags", ", ".join(self.tags) or "—"),
        ]
        return rows


# `--section none` (and the app's "Bölüm yok"): the drafts go into no section.
NO_SECTION = "none"


def section_number(value: Any) -> int | None:
    """A shop section id (a positive int, or its digits as text); None for anything else."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    return value if isinstance(value, int) and value > 0 else None


def resolve_section(sections: Iterable[dict[str, Any]], wanted: str) -> dict[str, Any] | None:
    """The shop section `wanted` names, from the shop's list (getShopSections).

    `wanted` is a section's id or its title (case-insensitive, surrounding spaces
    ignored); `none` means no section (None), unless a section is called that. Raises
    ValidationError for a name or id the shop does not have (a deleted section), for a
    title two sections share (use the id then), and for a shop without sections.
    """
    items = [s for s in sections if isinstance(s, dict)]
    text = str(wanted or "").strip()
    if not text:
        raise ValidationError("--section needs a section name or id (or none).")
    number = section_number(text)
    if number is not None:
        for section in items:
            if section_number(section.get("shop_section_id")) == number:
                return section
    folded = text.casefold()
    matches = [s for s in items if str(s.get("title") or "").strip().casefold() == folded]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise ValidationError(
            f"More than one shop section is called {text!r}; use its id instead "
            "(see `etsyotomasyon shop profiles`)."
        )
    if folded == NO_SECTION:
        return None
    if not items:
        raise ValidationError(
            "Your shop has no sections. Add one in Etsy (Shop Manager → your shop → "
            "sections), or leave out --section."
        )
    names = ", ".join(f"{s.get('title') or '?'} ({s.get('shop_section_id')})" for s in items)
    what = f"id {number}" if number is not None else repr(text)
    raise ValidationError(
        f"Your shop has no section {what} (it may have been deleted). Its sections: {names}"
    )


def money(value: Any) -> float | None:
    if isinstance(value, dict):
        amount, divisor = value.get("amount"), value.get("divisor") or 100
        if isinstance(amount, (int, float)):
            return round(amount / divisor, 2)
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def capture(listing: dict[str, Any]) -> Template:
    """Turn an Etsy listing response into a reusable template."""
    listing_id = listing.get("listing_id")
    if not listing_id:
        raise ValidationError("That response carries no listing_id — is the id correct?")

    fields: dict[str, Any] = {}
    for name in INHERITED_FIELDS:
        if name == "price":
            price = money(listing.get("price"))
            if price is not None:
                fields["price"] = price
            continue
        if name == "type":
            value = listing.get("listing_type") or listing.get("type")
            if value in LISTING_TYPES:
                fields["type"] = value
            continue
        value = listing.get(name)
        if name == "quantity" and isinstance(value, int):
            # A varied listing reports the total across its variations; the draft only
            # needs a legal number here, and its real stock comes with the variations.
            value = min(value, MAX_QUANTITY)
        if value not in (None, ""):
            fields[name] = value

    # Etsy reports 0 for a weight or size nobody entered, and refuses 0 on a new listing
    # ("must be greater than 0"): an unset measure is not copied onto the drafts.
    clean_measures(fields)

    # Etsy will refuse anything outside these, and a bad template poisons every draft.
    if fields.get("who_made") not in WHO_MADE:
        fields.pop("who_made", None)
    if fields.get("when_made") not in WHEN_MADE:
        fields.pop("when_made", None)
    # A digital download is never shipped, so whatever shipping, processing or parcel
    # values Etsy still reports for it are not copied onto its digital drafts.
    if fields.get("type") == "download":
        for name in SHIPPING_ONLY_FIELDS:
            fields.pop(name, None)

    return Template(
        source_listing_id=int(listing_id),
        source_title=str(listing.get("title", "")),
        fields=fields,
        materials=[str(m) for m in (listing.get("materials") or [])],
        description=str(listing.get("description", "")),
        tags=[str(t) for t in (listing.get("tags") or [])],
    )
