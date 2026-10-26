"""What each mockup is: its product type, its colour, and whether drafts use it.

The mockup files themselves stay the source of truth — whatever image sits in
1-MOCKUPS is a mockup. This module only keeps the facts a file name cannot hold
reliably, in `1-MOCKUPS/mockups.json`:

    {"shirt-white.jpg": {"type": "tshirt", "color": "Beyaz", "enabled": true, "order": 0}}

A mockup with no entry gets a guess from its file name and is enabled. An entry
whose file is gone is ignored (kept on disk, so renaming the file back restores it).

`order` is the seller's own sequence (0 first). The first mockup a draft uses
becomes its main image (Etsy's rank 1), so the order matters. Mockups without an
order (every mockup of an older catalog, or one added after the last reorder)
follow the ordered ones, in folder order. Drafts use the enabled mockups in this
order, at most MAX_ENABLED of them less one per info image (drop.infoimages);
`usage` says exactly which.

Print areas stay in positions.json (see `mockup.load_positions`); the helpers at
the bottom answer "which rectangle does this mockup use, and why" exactly the way
`pipeline.run` decides it, so a screen can never show one area and composite another.
"""

from __future__ import annotations

import io
import json
import math
import re
import threading
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from PIL import Image

from ..errors import ValidationError
from . import mockup
from .workspace import IMAGE_SUFFIXES, Workspace

CATALOG_FILE = "mockups.json"

TYPES = (
    "tshirt",
    "sweatshirt",
    "hoodie",
    "mug",
    "poster",
    "canvas",
    "phone_case",
    "tote",
    "pillow",
    "sticker",
    "other",
)

# Etsy takes 20 images per listing, and every draft also carries the flat design. The
# shop's info images take their room too: max_enabled(ws) is the real number.
MAX_ENABLED = 19

# Mockup types that show a physical product: never the pictures of a download-only draft.
PHYSICAL_TYPES = frozenset({"tshirt", "sweatshirt", "hoodie", "mug", "phone_case", "tote",
                            "pillow", "sticker"})

# Pixel limits for an uploaded image (mockups and designs). Pillow itself refuses only
# above ~179M pixels, yet one 13000x13000 image already needs ~680 MB per decoded copy,
# and composing, thumbnails and previews each make copies. 60M pixels is 7745 px
# square, far more than Etsy shows (its zoom viewer wants 2000 px on the short side).
MAX_PIXELS = 60_000_000
MAX_EDGE = 12_000

SOURCE_OWN = "own"
SOURCE_SAME_SIZE = "same_size"
SOURCE_DEFAULT = "default"

# Words are compared after folding: lower case, Turkish letters to ASCII (ı→i, ş→s,
# ç→c, ğ→g, ö→o, ü→u), accents dropped. Checked in this order, so "hoodie" wins over
# "shirt" and "canvas" over "print".
_TYPE_WORDS: tuple[tuple[str, frozenset[str]], ...] = (
    ("hoodie", frozenset({"hoodie", "hoodies", "hoody", "kapusonlu", "kapsonlu"})),
    ("sweatshirt", frozenset({"sweatshirt", "sweatshirts", "sweat", "crewneck"})),
    ("tshirt", frozenset({"tshirt", "tshirts", "tee", "tees", "shirt", "tisort", "tisortu"})),
    ("mug", frozenset({"mug", "mugs", "cup", "cups", "kupa", "bardak"})),
    ("phone_case", frozenset({"phone", "phonecase", "case", "iphone", "samsung", "kilif"})),
    ("tote", frozenset({"tote", "totebag", "bag", "canta"})),
    ("pillow", frozenset({"pillow", "cushion", "yastik", "kirlent"})),
    ("sticker", frozenset({"sticker", "stickers", "decal", "etiket"})),
    ("canvas", frozenset({"canvas", "tuval"})),
    ("poster", frozenset({"poster", "print", "frame", "framed", "cerceve", "afis", "wallart"})),
)

# folded word -> display word (Turkish, the app's first language).
_COLOR_WORDS: tuple[tuple[str, frozenset[str]], ...] = (
    ("Beyaz", frozenset({"white", "beyaz"})),
    ("Siyah", frozenset({"black", "siyah"})),
    ("Lacivert", frozenset({"navy", "lacivert"})),
    ("Krem", frozenset({"cream", "krem", "natural", "naturel", "ivory", "ecru"})),
    ("Gri", frozenset({"grey", "gray", "gri", "heather", "ash"})),
    ("Kırmızı", frozenset({"red", "kirmizi"})),
    ("Bordo", frozenset({"maroon", "burgundy", "bordo"})),
    ("Mavi", frozenset({"blue", "mavi"})),
    ("Yeşil", frozenset({"green", "yesil"})),
    ("Haki", frozenset({"olive", "khaki", "haki"})),
    ("Pembe", frozenset({"pink", "pembe"})),
    ("Sarı", frozenset({"yellow", "sari"})),
    ("Turuncu", frozenset({"orange", "turuncu"})),
    ("Mor", frozenset({"purple", "mor", "lila", "lilac"})),
    ("Kahverengi", frozenset({"brown", "kahverengi", "kahve"})),
    ("Bej", frozenset({"beige", "bej", "sand", "kum"})),
    ("Meşe", frozenset({"oak", "mese", "wood", "wooden", "ahsap"})),
    ("Ceviz", frozenset({"walnut", "ceviz"})),
)

_FOLD = str.maketrans({"ı": "i", "ş": "s", "ç": "c", "ğ": "g", "ö": "o", "ü": "u"})

# Read-modify-write of mockups.json / positions.json from several request threads.
_LOCK = threading.RLock()


class TooManyPixels(ValidationError):
    """An image whose pixel dimensions are too large to handle safely."""

    def __init__(self, name: str, width: int, height: int) -> None:
        self.name = name
        self.width = int(width)
        self.height = int(height)
        super().__init__(
            f"{name} is {self.width}x{self.height} px. Images can be at most {MAX_EDGE} px "
            f"on a side and {MAX_PIXELS // 1_000_000} million pixels; make it smaller."
        )


def too_many_pixels(size: tuple[int, int]) -> bool:
    """Whether an image of `size` (width, height) is over the pixel limits."""
    width, height = (int(v) for v in size)
    return width * height > MAX_PIXELS or max(width, height) > MAX_EDGE


def check_pixels(name: str, size: tuple[int, int]) -> None:
    """Raise TooManyPixels when `size` is over the limits."""
    if too_many_pixels(size):
        raise TooManyPixels(name, *size)


def bomb_size(exc: BaseException) -> tuple[int, int]:
    """A (width, height) for Pillow's DecompressionBombError, which only gives the pixel
    count: the square of that many pixels, or (0, 0) when the text has no count."""
    found = re.search(r"\((\d+) pixels\)", str(exc))
    side = math.isqrt(int(found.group(1))) if found else 0
    return side, side


@dataclass
class MockupInfo:
    name: str
    type: str
    color: str
    enabled: bool = True
    order: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type, "color": self.color,
                "enabled": self.enabled, "order": self.order}


def _fold(text: str) -> str:
    text = text.replace("İ", "i").replace("I", "i").lower().translate(_FOLD)
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _words(filename: str) -> list[str]:
    stem = Path(filename).stem
    # camelCase and digits split too: "tshirtWhite01" -> tshirt, white.
    stem = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", stem)
    folded = _fold(stem)
    words = [w for w in re.split(r"[^a-z]+", folded) if w]
    # "t-shirt" / "t shirt" arrive as two words.
    joined = [a + b for a, b in zip(words, words[1:]) if a == "t" and b.startswith("shirt")]
    return words + joined


def guess(filename: str) -> tuple[str, str]:
    """(type, colour) read from a file name; ("other", "") when nothing matches.

    English and Turkish words are both understood: `kupa-beyaz.jpg` and
    `mug_white.png` both give ("mug", "Beyaz"). The colour is a display word in
    Turkish, the app's first language.
    """
    words = set(_words(filename))
    kind = next((name for name, keys in _TYPE_WORDS if words & keys), "other")
    color = next((label for label, keys in _COLOR_WORDS if words & keys), "")
    return kind, color


def catalog_path(ws: Workspace) -> Path:
    return ws.mockups / CATALOG_FILE


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _order_value(value: Any) -> int | None:
    # bool is an int in Python, but `true` is not a position.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _info(name: str, raw: Any) -> MockupInfo:
    kind, color = guess(name)
    if not isinstance(raw, dict):
        return MockupInfo(name, kind, color, True)
    stored_type = raw.get("type")
    stored_color = raw.get("color")
    enabled = raw.get("enabled")
    return MockupInfo(
        name=name,
        type=stored_type if stored_type in TYPES else kind,
        color=str(stored_color).strip() if isinstance(stored_color, str) else color,
        enabled=enabled if isinstance(enabled, bool) else True,
        order=_order_value(raw.get("order")),
    )


def _sorted(infos: list[MockupInfo]) -> list[MockupInfo]:
    """The seller's order: mockups with an order by it, then the rest in folder order.

    `infos` arrive in folder order, and equal orders keep it (sorted() is stable).
    """
    ordered = sorted((i for i in infos if i.order is not None), key=lambda i: i.order or 0)
    return ordered + [i for i in infos if i.order is None]


def _infos(ws: Workspace, raw: dict[str, Any]) -> list[MockupInfo]:
    return _sorted([_info(path.name, raw.get(path.name)) for path in ws.mockup_files()])


def load(ws: Workspace) -> dict[str, MockupInfo]:
    """Every mockup file with its catalog facts, first (main image) to last.

    The order is the seller's saved one; without any, it is `mockup_files()` order.
    """
    with _LOCK:
        raw = _read_json(catalog_path(ws))
        return {info.name: info for info in _infos(ws, raw)}


def ordered_names(ws: Workspace) -> list[str]:
    """Every mockup's file name, first (main image) to last."""
    return list(load(ws))


def _mockup_path(ws: Workspace, name: str) -> Path:
    """The file for `name`, which must be a mockup in 1-MOCKUPS — never anything else."""
    if not name or name != Path(name).name or name in (".", ".."):
        raise FileNotFoundError(name)
    for path in ws.mockup_files():
        if path.name == name:
            return path
    raise FileNotFoundError(name)


def update(
    ws: Workspace,
    name: str,
    *,
    type: str | None = None,  # noqa: A002 — the catalog's own field name
    color: str | None = None,
    enabled: bool | None = None,
) -> MockupInfo:
    """Change what is recorded about one mockup. FileNotFoundError if there is none."""
    _mockup_path(ws, name)
    if type is not None and type not in TYPES:
        raise ValidationError(f"Unknown mockup type {type!r}. Use one of: {', '.join(TYPES)}")
    if color is not None:
        color = str(color).strip()
        if len(color) > 40:
            raise ValidationError("A colour name can be at most 40 characters.")
    with _LOCK:
        path = catalog_path(ws)
        raw = _read_json(path)
        info = _info(name, raw.get(name))
        if type is not None:
            info.type = type
        if color is not None:
            info.color = color
        if enabled is not None:
            info.enabled = bool(enabled)
        raw[name] = _entry(info)
        _write_json(path, raw)
        return info


def _entry(info: MockupInfo) -> dict[str, Any]:
    """What mockups.json stores for one mockup (`order` only once it has one)."""
    entry: dict[str, Any] = {"type": info.type, "color": info.color, "enabled": info.enabled}
    if info.order is not None:
        entry["order"] = info.order
    return entry


def arrange(
    ws: Workspace,
    *,
    order: list[str] | None = None,
    enabled: list[str] | None = None,
) -> dict[str, MockupInfo]:
    """Save the seller's order and/or exactly which mockups drafts may use, at once.

    `order` lists mockup names first (main image) to last; mockups it leaves out keep
    their sequence after the listed ones. `enabled` is the complete set to switch on:
    every other mockup is switched off. Everything is checked before anything is
    written: ValidationError for a name listed twice, FileNotFoundError for a name that
    is not a mockup. Returns the new `load(ws)`.
    """
    if order is None and enabled is None:
        raise ValidationError("Nothing to change: send an order or the enabled mockups.")
    known = {p.name for p in ws.mockup_files()}
    for names, what in ((order, "order"), (enabled, "enabled")):
        if names is None:
            continue
        for name in names:
            if not isinstance(name, str) or name not in known:
                raise FileNotFoundError(str(name))
        if len(set(names)) != len(names):
            raise ValidationError(f"A mockup is listed twice in {what}.")
    with _LOCK:
        path = catalog_path(ws)
        raw = _read_json(path)
        infos = _infos(ws, raw)
        if order is not None:
            by_name = {info.name: info for info in infos}
            listed = [by_name[name] for name in order if name in by_name]
            chosen = {info.name for info in listed}
            infos = listed + [info for info in infos if info.name not in chosen]
            for index, info in enumerate(infos):
                info.order = index
        if enabled is not None:
            switched_on = set(enabled)
            for info in infos:
                info.enabled = info.name in switched_on
        for info in infos:
            raw[info.name] = _entry(info)
        _write_json(path, raw)
        return {info.name: info for info in infos}


def remove(ws: Workspace, name: str) -> None:
    """Delete a mockup file and everything recorded about it (catalog and print area)."""
    target = _mockup_path(ws, name)
    with _LOCK:
        target.unlink()
        path = catalog_path(ws)
        raw = _read_json(path)
        if raw.pop(name, None) is not None:
            _write_json(path, raw)
        positions = _read_json(ws.positions_path)
        if positions.pop(name, None) is not None:
            _write_json(ws.positions_path, dict(sorted(positions.items())))


_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(10)), *(f"lpt{i}" for i in range(10))}


def safe_name(filename: str) -> str:
    """A file name that is safe on Windows and macOS, keeping what can be kept."""
    base = re.split(r"[\\/]", str(filename or ""))[-1]
    base = _UNSAFE.sub("", base).strip().strip(".").strip()
    stem, suffix = Path(base).stem.strip(), Path(base).suffix.lower()
    stem = stem[:100].rstrip(". ") or "mockup"
    if stem.lower() in _RESERVED:
        stem = f"mockup-{stem}"
    return f"{stem}{suffix}"


def add(ws: Workspace, filename: str, data: bytes) -> str:
    """Save an uploaded mockup into 1-MOCKUPS and return the name it was saved under.

    Refuses anything that is not an image Pillow can read. Never overwrites: a
    second `shirt.jpg` becomes `shirt-2.jpg`.
    """
    name = safe_name(filename)
    suffix = Path(name).suffix
    if suffix not in IMAGE_SUFFIXES:
        raise ValidationError(
            f"{name}: mockups can be {', '.join(sorted(s.lstrip('.') for s in IMAGE_SUFFIXES))} images."
        )
    if not data:
        raise ValidationError(f"{name} is empty.")
    try:
        with Image.open(io.BytesIO(data)) as image:
            size = image.size
            image.verify()
    except Image.DecompressionBombError as exc:
        # Pillow refuses above ~179M pixels before the size can be read.
        raise TooManyPixels(name, *bomb_size(exc)) from exc
    except (OSError, ValueError, SyntaxError) as exc:
        raise ValidationError(f"{name} is not an image that can be read ({exc}).") from exc
    check_pixels(name, size)
    ws.mockups.mkdir(parents=True, exist_ok=True)
    stem = Path(name).stem
    with _LOCK:
        number = 1
        while True:
            candidate = name if number == 1 else f"{stem}-{number}{suffix}"
            target = ws.mockups / candidate
            try:
                with target.open("xb") as handle:
                    handle.write(data)
            except FileExistsError:
                number += 1
                continue
            return candidate


def enabled_mockups(ws: Workspace) -> list[Path]:
    """The mockups drafts are made with: enabled ones, in the seller's order, at most
    `max_enabled(ws)` (19, less one per info image).

    The first one becomes the draft's main image. `usage` has the same rule with names.
    """
    return _paths(ws, usage(ws)["used"])


def run_mockups(ws: Workspace, listing_type: str | None,
                infos: dict[str, MockupInfo] | None = None) -> list[Path]:
    """The mockups a run with a template of `listing_type` composites onto, first (the
    main image) to last: `usage(ws, listing_type=...)["used"]` as paths. The app's run,
    `drop run` and `drop auto` all use this one list."""
    return _paths(ws, usage(ws, infos, listing_type=listing_type)["used"])


def _paths(ws: Workspace, names: list[str]) -> list[Path]:
    paths = {path.name: path for path in ws.mockup_files()}
    return [paths[name] for name in names if name in paths]


def max_enabled(ws: Workspace, info: int | None = None) -> int:
    """How many mockups a draft can use: MAX_ENABLED (Etsy's 20 pictures less the flat
    design), less one per info image the drafts end with (drop.infoimages)."""
    from . import infoimages  # it builds on this module

    if info is None:
        info = infoimages.count(ws)
    return infoimages.mockup_cap(info)


def usage(ws: Workspace, infos: dict[str, MockupInfo] | None = None,
          info: int | None = None, *, listing_type: str | None = None) -> dict[str, Any]:
    """Which mockups a draft uses: the one rule every screen shows.

    {"used": [names first to last; the first is the main image],
     "over_limit": [switched-on names that do not fit, beyond "max"],
     "left_out": [switched-on names a download-only template leaves out],
     "enabled": how many switched-on ones it can use (before the limit), "total": how
     many mockups, "max": max_enabled (MAX_ENABLED less the info images), "info": the
     info images}
    `info` is the number of info images when the caller knows it (read otherwise).
    A `listing_type` of "download" leaves out the mockups showing a physical product
    (PHYSICAL_TYPES) first, and only then takes the first "max" of the rest: a poster
    after fifteen T-shirts is still used.
    """
    from . import infoimages

    if infos is None:
        infos = load(ws)
    if info is None:
        info = infoimages.count(ws)
    limit = max_enabled(ws, info)
    switched_on = [name for name, facts in infos.items() if facts.enabled]
    left_out: list[str] = []
    if listing_type == "download":
        left_out = [name for name in switched_on if infos[name].type in PHYSICAL_TYPES]
        switched_on = [name for name in switched_on if name not in left_out]
    return {
        "used": switched_on[:limit],
        "over_limit": switched_on[limit:],
        "left_out": left_out,
        "enabled": len(switched_on),
        "total": len(infos),
        "max": limit,
        "info": info,
    }


# --- print areas ------------------------------------------------------------------


def effective_areas(
    ws: Workspace, *, sizes: dict[str, tuple[int, int]] | None = None
) -> dict[str, tuple[mockup.PrintArea, str]]:
    """(area, source) for every mockup, decided exactly as `pipeline.run` decides it.

    source is "own" (its positions.json entry), "same_size" (the place of the first
    calibrated mockup, by name, with identical pixel dimensions; its realism and curve are
    not borrowed) or "default".
    """
    available = ws.mockup_files()
    positions = mockup.load_positions(ws.positions_path)
    if sizes is None:
        sizes = mockup.mockup_sizes(available)
    by_size: dict[tuple[int, int], mockup.PrintArea] = {}
    for name in sorted(positions):
        if name in sizes:
            by_size.setdefault(sizes[name], positions[name])
    out: dict[str, tuple[mockup.PrintArea, str]] = {}
    for path in available:
        own = positions.get(path.name)
        if own is not None:
            out[path.name] = (own, SOURCE_OWN)
            continue
        borrowed = by_size.get(sizes.get(path.name, (0, 0)))
        if borrowed is not None:
            # Where the print goes, not how it looks: a same-size mockup can be another
            # product (a square mug shot beside a square tee), so its realism and curve
            # stay its own type's until a same-size save copies them on purpose.
            out[path.name] = (borrowed.geometry(), SOURCE_SAME_SIZE)
        else:
            out[path.name] = (mockup.DEFAULT_PRINT_AREA, SOURCE_DEFAULT)
    return out


def kind_of(infos: dict[str, MockupInfo] | None, name: str) -> str:
    """The mockup type compositing uses for `name`: its catalog type (`load`), else the
    guess from its file name. It picks the realism and curve an area without its own
    gets (`mockup.styled`)."""
    facts = (infos or {}).get(name)
    return facts.type if facts is not None else guess(name)[0]


def effective_area(ws: Workspace, name: str) -> tuple[mockup.PrintArea, str]:
    """(area, source) for one mockup. FileNotFoundError if it is not a mockup."""
    _mockup_path(ws, name)
    return effective_areas(ws)[name]


def same_size_names(
    ws: Workspace, name: str, *, sizes: dict[str, tuple[int, int]] | None = None
) -> list[str]:
    """Every mockup (in folder order, `name` included) with `name`'s pixel dimensions.

    Empty when `name` is not a mockup or its size cannot be read.
    """
    mockups = ws.mockup_files()
    if sizes is None:
        sizes = mockup.mockup_sizes(mockups)
    size = sizes.get(name)
    if not size or name not in {p.name for p in mockups}:
        return []
    return [p.name for p in mockups if sizes.get(p.name) == size]


def save_area(
    ws: Workspace,
    name: str,
    area: mockup.PrintArea,
    *,
    same_size: bool = False,
    dry_run: bool = False,
    sizes: dict[str, tuple[int, int]] | None = None,
    keep_style: bool = False,
) -> list[str]:
    """Give `name` (and, with same_size, every mockup of its size) the print area,
    its corners, realism and curve included.

    keep_style: change only where the print goes; each target keeps the realism and
    curve it had (the CLI's --area, which has no say in them). Without it, a realism or
    curve that `area` leaves on None still keeps another mockup's own value (the editor
    sends None for a slider the seller did not touch).
    Returns the names that were — or with dry_run, would be — changed.
    """
    _mockup_path(ws, name)
    targets = [name]
    if same_size:
        targets = same_size_names(ws, name, sizes=sizes)
        if not targets:
            raise ValidationError(
                f"Cannot read the pixel size of {name}, so there is nothing to match."
            )
    if dry_run:
        return targets
    with _LOCK:
        positions = mockup.load_positions(ws.positions_path)
        for target in targets:
            old = positions.get(target)
            if keep_style and old is not None:
                positions[target] = replace(area, realism=old.realism, curve=old.curve)
            elif target != name and old is not None:
                # A realism or curve left on the default (None) is not a setting, so a
                # sibling keeps its own instead of being reset to its type's default.
                positions[target] = replace(
                    area,
                    realism=old.realism if area.realism is None else area.realism,
                    curve=old.curve if area.curve is None else area.curve,
                )
            else:
                positions[target] = area
        mockup.save_positions(ws.positions_path, positions)
    return targets


def clear_area(ws: Workspace, name: str) -> bool:
    """Forget `name`'s own print area. False if it had none (already on the default)."""
    _mockup_path(ws, name)
    with _LOCK:
        positions = mockup.load_positions(ws.positions_path)
        if positions.pop(name, None) is None:
            return False
        mockup.save_positions(ws.positions_path, positions)
    return True
