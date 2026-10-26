"""Working out what a design is *about*, from its filename.

This is the honest weak point of the whole pipeline and it is better to say so than
to hide it. `mountain-sunset.png` yields a usable concept. `IMG_2043.png` does not,
and no amount of cleverness changes that — the information is simply not there.

So this module's real job is not extraction, it is **knowing when it failed**. A junk
seed is flagged, surfaced to the seller, and never quietly turned into a confident
title that would put a wrong listing in their shop.

Most junk is not a camera name but a default one: what Canva, Photoshop, Procreate, a
phone or a browser calls a file nobody named — `Adsız tasarım (3)`, `Untitled-1 copy`,
`WhatsApp Image 2026-09-01 at 10.10.10`, `indir (2)`. Those are recognised in Turkish
and English, and the marks a copy leaves on a good name (`Retro Sunset (2)`,
`Copy of …`, `… kopyası`, `… - Copy`) are taken off rather than put in a title.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path


def fold(text: str) -> str:
    """Compare text the way a person reads it: `Adsız Tasarım` is `adsiz tasarim`.

    Lowercasing alone keeps the Turkish dotless ı (and gives İ a combining dot), so a
    list of junk names written in ASCII never matched what apps export in Turkish.
    """
    text = str(text or "").replace("ı", "i").replace("İ", "i")
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()


def _lower(token: str) -> str:
    # "İ".lower() is "i" plus a combining dot; the concept keeps its letters, not that.
    return token.replace("İ", "i").lower()


# Camera and screenshot names. These carry a number, never a concept. Matched on the
# folded name, so `Adsız` and `adsiz` are the same thing.
_CAMERA = re.compile(
    r"^(img|dsc|dscn|dscf|pxl|gopr|mvimg|photo|image|screenshot|screen[\s_-]?shot|"
    r"ekran[\s_-]?goruntusu|ekran[\s_-]?resmi|adsiz|untitled|unnamed|document|scan|"
    r"vid|video|mov|snapchat|signal)"
    # Anything after is a timestamp, a counter or a date — never a product.
    r"[\s_.\-()]*[\d\s_.\-()]*$",
)

# Whole-name prefixes that only ever come from a device or a messaging app. Whatever
# follows (a date, a time, the app a screenshot was taken in) is not a product.
_DEVICE_PREFIX = re.compile(
    r"^(screenshot|screen[\s_-]*shot|screen[\s_-]*recording|"
    r"ekran[\s_-]*(goruntusu|resmi|alintisi|kaydi)|"
    r"whatsapp[\s_-]*(image|gorsel|video|photo|resim|audio|ses)|"
    r"snapchat[\s_-]|signal[\s_-]\d)",
)

# Tokens that are never part of a product name, wherever they appear: camera and
# screenshot prefixes, the marks a copy leaves, and words that only describe the file.
# Deliberately excludes "photo", "image" and "frame": those appear in real product
# names ("photo frame", "image transfer"), and stripping them would lose the concept.
_ALWAYS_NOISE = {
    "finalv", "copy", "kopya", "kopyasi", "ogesinin", "duzenlenmis", "untitled", "adsiz",
    "isimsiz", "temp", "tmp", "printfile", "unnamed", "recovered", "kurtarildi",
    "removebg", "nobg", "canva", "printready", "readytoprint", "forprint", "baskiyahazir",
    # Camera and screenshot prefixes, for when they survive as a bare token.
    "img", "dsc", "dscn", "dscf", "pxl", "gopr", "mvimg", "screenshot", "scan",
}

# Revision markers. These are ordinary product words in the middle of a name — "new" is
# in "New York", "son" is in Turkish names, "print" and "design" describe real products —
# so they only count as noise where they actually behave like a marker: at the end, after
# the name proper. Stripping them everywhere would turn "new-york-skyline" into "york
# skyline" and mislabel two of the biggest print categories on Etsy.
_TRAILING_NOISE = {
    "final", "new", "yeni", "edit", "edited", "draft", "taslak", "test", "deneme",
    "son", "orig", "original", "orijinal", "export", "output", "cikti", "asset", "file",
    "dosya", "version", "surum", "revised", "fix", "print", "baski", "design", "tasarim",
    "tasarimi", "artwork", "preview", "onizleme", "png", "jpg", "jpeg", "transparent",
    "seffaf", "hd", "hq", "hires", "upscaled", "mockup", "rev", "revision", "revize",
    "revizyon", "duzeltme", "duzeltilmis", "guncel", "yedek", "backup",
    # What a trailing date leaves behind: "… 2026-09-01 at 10.10.10".
    "at", "saat",
}

# Markers of more than one word, stripped together from the end: "…-print-ready",
# "… ready to print", "… baskıya hazır", "… son hali" (Turkish "final version").
_TRAILING_PHRASES = sorted(
    {
        ("print", "ready"), ("ready", "to", "print"), ("ready", "for", "print"),
        ("baskiya", "hazir"), ("baskiya", "uygun"), ("baski", "icin"),
        ("son", "hali"), ("son", "versiyon"), ("son", "surum"), ("yeni", "hali"),
        ("high", "res"), ("high", "resolution"), ("hi", "res"),
    },
    key=lambda words: (-len(words), words),
)

# A marker word is still the product when the word before it makes a phrase with it: a
# paw print, a leopard print, "like father like son", interior design, a coffee fix.
# "dog-dad-paw-print.png" is a paw print; "boeing-747-print.png" is a print of a 747.
_KEPT_AFTER = {
    "print": {
        "paw", "hand", "foot", "feet", "finger", "thumb", "lip", "kiss", "boot", "leopard",
        "cheetah", "zebra", "tiger", "snake", "snakeskin", "cow", "giraffe", "dalmatian",
        "animal", "block", "lino", "linocut", "woodblock",
    },
    "son": {
        "father", "dad", "daddy", "papa", "mother", "mom", "mommy", "mama", "mum", "and",
        "like", "of", "my", "our", "proud", "best", "only", "favorite", "favourite", "baby",
        "first", "oldest", "youngest", "middle", "eldest",
    },
    "design": {"interior", "graphic", "web", "fashion", "game"},
    "fix": {"coffee", "caffeine", "daily", "quick", "sugar", "chocolate", "tea", "book"},
}

# A name made only of these words is a default name, not a product: "Untitled design",
# "Adsız tasarım", "New Project", "image", "indir", "Çalışma Yüzeyi 1", "Layer 1".
_DEFAULT_WORDS = {
    "untitled", "adsiz", "isimsiz", "unnamed", "image", "images", "img", "photo", "photos",
    "picture", "pictures", "pic", "resim", "resimler", "gorsel", "gorseller", "foto",
    "fotograf", "file", "files", "dosya", "document", "belge", "design", "designs",
    "tasarim", "tasarimi", "artwork", "art", "artboard", "calisma", "yuzeyi", "project",
    "proje", "folder", "klasor", "new", "yeni", "download", "downloads", "indir",
    "indirilen", "indirme", "whatsapp", "screenshot", "screen", "shot", "ekran", "resmi",
    "goruntusu", "alintisi", "scan", "taranmis", "canva", "draft", "taslak", "test",
    "deneme", "sample", "ornek", "copy", "kopya", "kopyasi", "final", "son", "mockup",
    "template", "sablon", "sketch", "cizim", "drawing", "layer", "katman", "group", "grup",
    "png", "jpg", "jpeg", "transparent", "seffaf", "print", "baski", "logo", "export",
    "output", "cikti", "edit", "edited", "duzenlenmis", "version", "surum", "original",
    "orijinal", "preview", "onizleme", "temp", "tmp", "misc", "diger", "other", "my",
    "benim", "the", "a", "of", "at", "and", "ve", "saat", "vid", "video", "mov", "rev",
    "revision", "revize", "revizyon", "yedek", "backup",
}

# Default names that are only junk when a counter follows: Figma's "Frame 12" and
# "Page 3" are defaults, but a design called "frame" or "page" might not be.
_COUNTED_DEFAULTS = {
    "frame", "cerceve", "page", "sayfa", "slide", "rectangle", "dikdortgen", "shape",
    "sekil", "vector", "vektor", "element", "graphic", "grafik",
}

_STOP_TAIL = {"and", "ve", "of", "the", "a", "an", "for", "with", "ile", "in", "on", "to"}

# `Copy of X` (Google Drive, old Windows), `Copy (2) of X`.
_COPY_OF = re.compile(r"^\s*(copy|kopya)\s*(\(\s*\d+\s*\))?\s+of\s+", re.IGNORECASE)
# `(3)`, `[2]`, `{1}` — a browser's or a file manager's copy counter, never a product.
_BRACKET_COUNTER = re.compile(r"[(\[{]\s*\d{1,4}\s*[)\]}]")
_UUID = re.compile(
    r"[0-9a-f]{8}[-_]?[0-9a-f]{4}[-_]?[0-9a-f]{4}[-_]?[0-9a-f]{4}[-_]?[0-9a-f]{12}",
    re.IGNORECASE,
)
# A calendar date, year first or last, with or without separators: 2026-09-01,
# 20260901, 01.09.2026.
_DATE = re.compile(
    r"(?<!\d)(?:(?:19|20)\d{2}([-_./]?)(?:0[1-9]|1[0-2])\1(?:0[1-9]|[12]\d|3[01])"
    r"|(?:0?[1-9]|[12]\d|3[01])([-_./])(?:0?[1-9]|1[0-2])\2(?:19|20)\d{2})(?!\d)"
)
# A clock time, only looked for once a date was found: 10.10.10, 10-10, 101010, 10.10 AM.
_TIME = re.compile(
    r"(?<!\d)(?:(?:[01]?\d|2[0-3])[.:_-][0-5]\d(?:[.:_-][0-5]\d)?|\d{6}(?:\d{1,3})?)"
    r"(?:\s*[ap]\.?m\.?)?(?![\d])",
    re.IGNORECASE,
)

# A version is `v3` or `1.2`, never a bare `66` — that is a route number, a year or a
# model, and dropping it anywhere in the name would cost products their identity. Plain
# counters are handled as trailing tokens instead, where they actually behave like one.
_VERSION = re.compile(r"^(v\d+(\.\d+)*|\d+(\.\d+)+)$", re.IGNORECASE)
_HEXISH = re.compile(r"^[0-9a-f]{8,}$", re.IGNORECASE)
# Pixel sizes and resolutions describe the file: 4500x5400, 300dpi. A print size such as
# 8x10 is a product fact and stays.
_PIXELS = re.compile(r"^(\d{3,5}x\d{3,5}(px)?|\d+(dpi|ppi|px))$", re.IGNORECASE)
_WORD_COUNTER = re.compile(r"^([^\W\d_]+)(\d{1,4})$")
_LEADING_INDEX = re.compile(r"^\d{1,4}[\s._-]+")
_SEPARATORS = re.compile(r"[\s._\-+()\[\]{},;~#=|/\\]+")
# A year after "class of" or "est." is part of the product, not a counter.
_YEAR = re.compile(r"^(19|20)\d{2}$")
_YEAR_AFTER = {"of", "est", "established", "since", "class", "sinifi", "mezun"}


@dataclass
class Seed:
    """A product concept derived from a filename."""

    text: str
    source: str
    is_junk: bool = False
    reason: str = ""

    @property
    def words(self) -> list[str]:
        return self.text.split()

    def __bool__(self) -> bool:
        return bool(self.text) and not self.is_junk


def derive(path: Path, *, folder_fallback: bool = True) -> Seed:
    """Turn a design's path into a concept seed.

    Falls back to the containing folder name when the filename alone is junk, which
    is what saves a camera-roll export sitting in a folder called `mountain sunset`.
    """
    stem = path.stem
    seed = _from_text(stem, source=stem)
    if seed or not folder_fallback:
        return seed

    parent = path.parent.name
    if parent:
        fallback = _from_text(parent, source=stem)
        if fallback:
            return Seed(
                text=fallback.text,
                source=stem,
                is_junk=False,
                reason=f"filename carried no concept; used the folder name {parent!r}",
            )
    return seed


def _looks_like_id(token: str) -> bool:
    """A generated id such as Canva's `DAFx7Kq2Lm8`: long, and letters and digits mixed."""
    if len(token) < 8 or token.isalpha() or token.isdigit():
        return False
    digits = sum(ch.isdigit() for ch in token)
    switches = sum(1 for a, b in zip(token, token[1:]) if a.isdigit() != b.isdigit())
    return switches >= 3 or digits >= 4


def _clean_token(token: str) -> str:
    """Letters, digits and inner apostrophes: what both a title and a tag accept."""
    if token == "&":
        return "and"
    kept = "".join(ch for ch in token if ch.isalnum() or ch == "'")
    return kept.strip("'")


def _from_text(raw: str, *, source: str) -> Seed:
    text = unicodedata.normalize("NFC", str(raw or "")).replace("’", "'").strip()
    if not text:
        return Seed("", source, True, "empty filename")

    folded = fold(text)
    if _CAMERA.match(folded) or _DEVICE_PREFIX.match(folded):
        return Seed("", source, True, f"{raw!r} looks like a camera or screenshot name")

    text = _UUID.sub(" ", text)
    if _DATE.search(text):
        text = _TIME.sub(" ", _DATE.sub(" ", text))
    text = _BRACKET_COUNTER.sub(" ", text)
    text = _COPY_OF.sub("", text)
    # `001-retro-sunset-surf` -> `retro-sunset-surf`
    text = _LEADING_INDEX.sub("", text)

    tokens: list[str] = []
    for part in _SEPARATORS.split(text):
        # `Resim1`, `Image2`, `Tasarım3`: a default word glued to its counter.
        glued = _WORD_COUNTER.match(part)
        if glued and fold(glued.group(1)) in _DEFAULT_WORDS | _COUNTED_DEFAULTS:
            tokens.extend([glued.group(1), glued.group(2)])
        elif part:
            tokens.append(part)

    kept: list[str] = []
    dropped_default = False
    for token in tokens:
        key = fold(token)
        if key in _ALWAYS_NOISE:
            dropped_default = True
            continue
        if _VERSION.match(token) or _HEXISH.match(token) or _PIXELS.match(token):
            continue
        if _looks_like_id(token):
            continue
        cleaned = _clean_token(_lower(token))
        if not cleaned:
            continue
        # `t-shirt` and `tee shirt` are one word to a buyer, and one to a title.
        if fold(cleaned) in ("shirt", "shirts") and kept and fold(kept[-1]) in ("t", "tee"):
            kept[-1] = "tshirt"
            continue
        kept.append(cleaned)

    # Nothing but default words — "Untitled design", "Adsız tasarım", "New Project",
    # "image", "indir" — however many counters and copy marks came with them.
    words = [fold(word) for word in kept if not word.isdigit()]
    counted = len(words) < len(kept)
    if (
        (words and all(word in _DEFAULT_WORDS for word in words))
        or (counted and len(words) == 1 and words[0] in _COUNTED_DEFAULTS)
        or (not words and dropped_default)
    ):
        return Seed(
            "", source, True,
            f"{raw!r} is a default name from a design app, phone or browser, not a "
            "product name",
        )

    # A bare counter is the very last thing in a name, so that is the only place it is
    # dropped: `mountain-sunset-2` loses its 2, `route-66-poster` keeps its 66. Checked
    # before the markers and never again after, or `boeing-747-print` would lose the 747
    # as soon as `print` came off and exposed it. A year after "class of" or "est" is
    # part of the product, not a counter.
    while kept and kept[-1].isdigit():
        if _YEAR.match(kept[-1]) and len(kept) > 1 and fold(kept[-2]) in _YEAR_AFTER:
            break
        kept.pop()

    # Markers trail the name rather than interrupting it, and they stack:
    # `mountain-sunset-final-edit`. A name cannot end on "and" or "of" either.
    _strip_markers(kept)

    if not kept:
        return Seed("", source, True, f"{raw!r} carries no describable words")

    concept = " ".join(kept)
    letters = sum(1 for ch in concept if ch.isalpha())
    if letters < 3:
        return Seed("", source, True, f"{raw!r} is too short to describe a product")

    return Seed(concept, source)


def _kept_by_neighbour(words: list[str], index: int) -> bool:
    """Whether the marker at `index` is part of the product with the word before it."""
    if index <= 0:
        return False
    return fold(words[index - 1]) in _KEPT_AFTER.get(fold(words[index]), ())


def _strip_markers(kept: list[str]) -> None:
    """Take revision markers off the end of a name, in place, however many are stacked.

    `paw-print-final` keeps its paw print, and so do `paw-print-print-ready` and
    `paw-print-ready`: there the paw print's own "print" doubles as "print-ready".
    """
    while kept:
        folded = [fold(word) for word in kept]
        for marker in _TRAILING_PHRASES:
            size = len(marker)
            if len(kept) >= size and tuple(folded[-size:]) == marker:
                start = len(kept) - size
                del kept[start + 1 if _kept_by_neighbour(kept, start) else start:]
                break
        else:
            last = len(kept) - 1
            if (folded[last] in _TRAILING_NOISE and not _kept_by_neighbour(kept, last)) or (
                kept[last] in _STOP_TAIL
            ):
                kept.pop()
                continue
            return


def group(seeds: list[Seed]) -> dict[str, list[Seed]]:
    """Group by concept so research runs once per distinct idea, not once per file.

    A hundred products across eighteen concepts is eighteen searches, not a hundred —
    which is the difference between comfortable and rate-limited on a 5 QPS app.
    """
    grouped: dict[str, list[Seed]] = {}
    for seed in seeds:
        if seed:
            grouped.setdefault(seed.text, []).append(seed)
    return grouped
