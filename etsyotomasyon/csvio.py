"""CSV helpers.

Sellers edit these files in Excel, Numbers or Google Sheets, so we read and write
UTF-8 with a BOM — without it Excel on Windows mangles accented characters, which
matters when your titles are in Turkish, German or French.

Multi-value cells (tags, materials, image paths) use '|' rather than ',' so that a
tag containing a comma survives a round trip through a spreadsheet.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from .errors import ValidationError

MULTI_SEP = "|"
ENCODING = "utf-8-sig"


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise ValidationError(f"CSV not found: {path}")
    with path.open("r", encoding=ENCODING, newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValidationError(f"{path} is empty — it needs a header row.")
        rows = []
        for row in reader:
            # Normalise headers and drop the all-blank rows spreadsheets love to append.
            clean = {
                (k or "").strip().lower(): (v or "").strip()
                for k, v in row.items()
                if k is not None
            }
            if any(clean.values()):
                rows.append(clean)
    return rows


def write_rows(path: Path, rows: Sequence[dict[str, Any]], *, columns: Sequence[str] | None = None) -> None:
    if not rows and not columns:
        raise ValidationError("Nothing to write and no columns given.")
    if columns is None:
        seen: list[str] = []
        for row in rows:
            for key in row:
                if key not in seen:
                    seen.append(key)
        columns = seen
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding=ENCODING, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _cell(row.get(k)) for k in columns})


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return MULTI_SEP.join(str(v) for v in value)
    return str(value)


# A decimal comma carries one or two digits after it. Three or more is a thousands
# separator in every locale that uses one, so it is never read as a decimal point,
# except after a lone 0: no thousands separator follows a 0, so 0,250 (kg) is 0.25.
DECIMAL_COMMA = re.compile(r"-?(?:\d+,\d{1,2}|0,\d+)")


def split_multi(value: str, *, allow_comma: bool = False) -> list[str]:
    """Split a multi-value cell on `|`.

    A comma is only a separator where a value cannot contain one. As a fallback for
    any cell without a `|` it would tear a single image path in two the moment a
    folder had a comma in its name — and a one-image row is the normal case, not an
    edge case, so `images` and `materials` never opt in.
    """
    if not value:
        return []
    raw = value.split(MULTI_SEP)
    if allow_comma and MULTI_SEP not in value:
        raw = value.split(",")
    return [part.strip() for part in raw if part.strip()]


def as_int(
    value: str, field: str, *, required: bool = False, minimum: int | None = None
) -> int | None:
    """Parse a whole number.

    A fractional value is an error, not something to round. Rounding `quantity=3.9`
    to 3 would set a stock level the seller never typed.
    """
    if not value:
        if required:
            raise ValidationError(f"{field} is required")
        return None
    try:
        number = float(value)
    except ValueError as exc:
        raise ValidationError(f"{field} must be a whole number, got {value!r}") from exc
    if number != int(number):
        raise ValidationError(f"{field} must be a whole number, got {value!r}")
    result = int(number)
    if minimum is not None and result < minimum:
        raise ValidationError(f"{field} must be {minimum} or more, got {result}")
    return result


def as_float(
    value: str, field: str, *, required: bool = False, minimum: float | None = None
) -> float | None:
    if not value:
        if required:
            raise ValidationError(f"{field} is required")
        return None
    # Accept '19,90' from locales that use a decimal comma — but only where the comma
    # cannot be a thousands separator. '1,299' is 1299 to a Turkish or German seller and
    # 1.299 to a naive parser. A rule of "one comma, no dot" cannot tell them apart and
    # would price a 1.299 TL poster at one lira thirty, with `price > 0` waving it
    # through. One or two digits after the comma is a decimal, and so is a comma after
    # a lone 0 ('0,250' kg); anything else is ambiguous and has to be typed
    # unambiguously rather than guessed at.
    text = value.strip()
    if DECIMAL_COMMA.fullmatch(text):
        text = text.replace(",", ".")
    elif "," in text:
        # web/api/listings.py reads this message's first words: keep them.
        raise ValidationError(
            f"{field} {value!r} is ambiguous — a comma here could be a decimal point or a "
            f"thousands separator. Write {text.replace(',', '')} if the comma separates "
            f"thousands, or {text.replace(',', '.')} if it is a decimal point."
        )
    try:
        number = float(text)
    except ValueError as exc:
        raise ValidationError(f"{field} must be a number, got {value!r}") from exc
    if minimum is not None and number < minimum:
        raise ValidationError(f"{field} must be {minimum} or more, got {number}")
    return number


def as_bool(value: str, field: str) -> bool | None:
    if not value:
        return None
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "y", "evet", "e"}:
        return True
    if lowered in {"0", "false", "no", "n", "hayir", "hayır", "h"}:
        return False
    raise ValidationError(f"{field} must be true/false, got {value!r}")


def resolve_paths(values: Iterable[str], base: Path) -> list[Path]:
    """Image paths in a CSV are relative to the CSV itself, which is what users expect."""
    out = []
    for value in values:
        candidate = Path(value)
        if value.startswith("~"):
            # A leading ~ is a home directory only if it expands to an absolute path;
            # otherwise it is just the first character of a filename, and expanding it
            # would throw the path into the user profile instead of the CSV's folder.
            expanded = candidate.expanduser()
            if expanded.is_absolute():
                out.append(expanded)
                continue
        out.append(candidate if candidate.is_absolute() else (base / candidate))
    return out
