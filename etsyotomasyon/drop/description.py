"""The description every new draft gets, made from the template listing's own.

A description is prose, and etsyotomasyon does not write marketing copy (see
generate.build_description): the seller's own words carry over. But the template
listing's description is about ITS design. Copied as it is, a line naming that listing
and a sentence about its pattern end up on every other design's draft ("a sunny grove
of lemons" on a woodland nursery mural). So:

- The template's own title, wherever its description repeats it, becomes the new draft's
  title, and so does a line made of the title's " | ", ", " or " - " segments ("Sage
  Lemon Wallpaper | Olive Citrus Mural").
- The seller can save a description template on Şablon İlan (product.json,
  `description_template`): the template's description with its title turned into
  {başlık}. {başlık} is each draft's title and {tasarım} its design (the concept read from
  the file name); {title} and {design} are the same placeholders in English.
- A sentence holding a word that names the template's own design (from its title and
  tags: "lemon", "sage"; never a product, room or material word such as "wallpaper",
  "sample" or "kitchen") is flagged: it would be copied onto every draft.

Nothing here talks to Etsy. Offsets are Python string indices (code points).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import generate
from .seeds import Seed, fold

if TYPE_CHECKING:  # pragma: no cover
    from .template import Template

# The placeholders, as each language's editor inserts them. Either spelling works in a
# saved template whatever the UI language is, in any case, with or without accents.
PLACEHOLDERS = {
    "tr": {"title": "{başlık}", "design": "{tasarım}"},
    "en": {"title": "{title}", "design": "{design}"},
}
_NAMES = {"baslik": "title", "title": "title", "tasarim": "design", "design": "design"}
_PLACEHOLDER = re.compile(r"\{\s*([^{}\s]{1,24})\s*\}")

# Our own bound on a saved description template. Etsy's API documents no maximum for a
# listing's description (createDraftListing: description is a required string).
MAX_CHARS = 50_000

# Where a title splits into segments: "A | B | C", "A, B, C" and "A - B" (a dash between
# spaces: "T-Shirt" stays one word).
_SEGMENT_SPLIT = re.compile(r"\s*[|,]\s*|\s+[-–—]\s+")
# A line's decoration around its text: bullets, emoji, quotes, a closing full stop.
_EDGES = re.compile(r"^(\W*)(.*?)(\W*)$", re.S)
# A sentence ends at . ! ? or … (a closing quote or bracket may follow) before a space.
_SENTENCE_END = re.compile(r"(?<=[.!?…])[\"'”’)\]]*\s+")

# Words that never name one design, only the product, its material, its format, where
# it goes, who it is for or how it is sold: a sentence about these suits any draft.
# generate's own vocabulary (product nouns, claims, generic and weak words) is left out
# too (generate._distinctive). Written folded (no accents, lowercase).
_NOT_DESIGN_WORDS = """
wallpaper wallpapers mural murals sample samples swatch swatches roll rolls panel panels
sheet sheets strip strips peel stick sticky removable adhesive self prepasted pre pasted
paste unpasted nonwoven non woven traditional washable scrubbable renter renters rental
friendly temporary permanent reusable repositionable install installation installs easy
hang hanging hung wall walls ceiling accent feature statement backdrop background border
decal decals tapestry tapestries rug rugs curtain curtains frame frames canvas painting
paintings drawing drawings illustration illustrations artwork artworks photo photograph
photography portrait candle candles holder holders soap soaps lamp lamps vase vases
planter planters clock clocks calendar calendars planner planners invitation invitations
template templates pattern patterns fabric fabrics bedding duvet quilt throw throws
cover covers sleeve sleeves pouch pouches wallet wallets bottle bottles glass glasses
ring rings jewelry jewellery scarf scarves dress dresses skirt leggings pajamas pyjamas
bib patch patches pin pins badge badges sign signs plaque plaques banner banners wreath
wreaths figurine figurines doll dolls toy toys plush crochet knit knitted embroidery
ebook worksheet worksheets font fonts preset presets file files mat mats mousepad napkin
napkins placemat placemats runner runners tile tiles board boards
decor decoration decorations decorative home house room rooms space spaces kitchen
bedroom bathroom bath living dining office hallway entryway playroom dorm apartment
nursery kids kid children child baby babies toddler teen teens adult adults family
size sizes sized inch inches cm mm meter meters metre metres foot feet ft width height
length wide tall long short dimension dimensions measurement measurements
available ready order orders ordering ordered custom made print printed printing ink
inks paper papers quality high thick thin heavy heavyweight light lightweight durable
sturdy strong eco sustainable recycled natural smooth textured texture matte satin
glossy finish finished coating coated water resistant proof fade uv
style styles styled look looks theme themed design designs designed pattern patterned
aesthetic boho bohemian minimalist minimal maximalist scandinavian scandi nordic
farmhouse rustic cottage cottagecore chic shabby contemporary elegant luxury luxurious
beautiful pretty lovely stunning gorgeous bold bright subtle neutral timeless
housewarming wedding weddings anniversary christmas xmas holiday holidays valentine
valentines easter halloween thanksgiving graduation shower bridal engagement retirement
new moving mother mothers father fathers day present presents gift gifts
extra big huge tiny
duvar kagit kagidi kagitlari tisort kupa bardak baski baskili tablo cerceve cerceveli
yastik canta etiket hediye dekor dekorasyon dekoratif ev oda odasi odalari mutfak yatak
salon banyo cocuk cocuklar bebek numune ornek rulo yapiskanli sokulebilir desen desenli
tasarim kaliteli ozel siparis boyut boyutlar olcu olculer kagidimiz urun urunler
"""


def _keys(words: Iterable[str]) -> frozenset[str]:
    return frozenset(generate._key(word) for word in words if word)


_NOT_DESIGN = _keys(_NOT_DESIGN_WORDS.split())


@dataclass(frozen=True)
class Flag:
    """A sentence that looks specific to the template listing's own design."""

    start: int
    end: int
    words: tuple[str, ...]  # the design words it holds (folded, singular), in order


# --- placeholders -----------------------------------------------------------------------


def placeholder_name(raw: str) -> str | None:
    """"title" | "design" for a placeholder's inner text ({Başlık}, {TASARIM}, {title})."""
    return _NAMES.get(fold(raw))


def unknown_placeholders(text: str) -> list[str]:
    """The {…} in a template that are not a placeholder: copied onto every draft as typed."""
    out: list[str] = []
    for match in _PLACEHOLDER.finditer(text or ""):
        if placeholder_name(match.group(1)) is None and match.group(0) not in out:
            out.append(match.group(0))
    return out


def has_placeholder(text: str, name: str) -> bool:
    return any(placeholder_name(m.group(1)) == name for m in _PLACEHOLDER.finditer(text or ""))


def design_name(seed: Seed | None) -> str:
    """{tasarım}: the design's concept as a title reads it ("Woodland Nursery Mural")."""
    if not seed or not seed.text:
        return ""
    return generate.titlecase(seed.text)


def render(template_text: str, *, title: str, design: str = "") -> str:
    """A saved description template filled in for one draft.

    Etsy needs a description on every draft: a template that fills to nothing gives
    the title as the opening line, as a template without a description always did.
    """

    def fill(match: re.Match[str]) -> str:
        name = placeholder_name(match.group(1))
        if name == "title":
            return title
        if name == "design":
            return design or title
        return match.group(0)

    text = _PLACEHOLDER.sub(fill, str(template_text or "")).strip()
    return text or f"{title}."


# --- the template's own title -----------------------------------------------------------


def _norm(text: str) -> str:
    return " ".join(fold(text).split())


def title_segments(title: str) -> list[str]:
    """The title's parts, split at " | " and ", " (normalised, in order, no repeats)."""
    out: list[str] = []
    for part in _SEGMENT_SPLIT.split(title or ""):
        key = _norm(part)
        if key and key not in out:
            out.append(key)
    return out


def _title_pattern(title: str) -> re.Pattern[str] | None:
    """The whole title as it may appear in running text: any case, any spacing, whole words.

    None for a one-word title: "Poster" inside "This poster is printed on..." is a word
    of the sentence, not the listing's name (a line holding only it is still replaced,
    see _is_title_line).
    """
    words = str(title or "").split()
    if len(words) < 2:
        return None
    body = r"\s+".join(re.escape(word) for word in words)
    return re.compile(rf"(?<!\w){body}(?!\w)", re.IGNORECASE)


def _is_title_line(core: str, segments: list[str]) -> bool:
    """A line made of the title's own segments, its first (the design's name) among them.

    "Lemon Wallpaper | Sage Mural" for a "Lemon Wallpaper | Sage Mural | Peel and Stick"
    title, or "Lemon Wallpaper" alone. The last part may be cut short at a word
    ("Lemon Wallpaper | Sage"), as a shortened title line often is.
    """
    parts = [_norm(part) for part in _SEGMENT_SPLIT.split(core)]
    parts = [part for part in parts if part]
    if not parts or not segments or segments[0] not in parts:
        return False
    for index, part in enumerate(parts):
        if part in segments:
            continue
        last = index == len(parts) - 1 and len(parts) > 1
        if not (last and any(seg.startswith(part + " ") for seg in segments)):
            return False
    return True


def replace_title(text: str, source_title: str, new_title: str) -> str:
    """The description with the template's own title in it replaced by `new_title`.

    The whole title wherever it appears (any case, any spacing), then any line that is
    made of the title's segments (see _is_title_line), keeping the line's bullets or
    emoji around it.
    """
    body = str(text or "")
    title = " ".join(str(source_title or "").split())
    if not body or not title:
        return body
    pattern = _title_pattern(title)
    if pattern is not None:
        body = pattern.sub(lambda _m: new_title, body)
    segments = title_segments(title)
    if not segments:
        return body
    lines = body.split("\n")
    for index, line in enumerate(lines):
        match = _EDGES.match(line)
        if match is None:  # pragma: no cover - the pattern matches any string
            continue
        before, core, after = match.groups()
        if core and _norm(core) != _norm(new_title) and _is_title_line(core, segments):
            lines[index] = f"{before}{new_title}{after}"
    return "\n".join(lines)


def _with_opening(body: str, title: str) -> str:
    """The title as the opening line, unless the text already names it.

    What a draft's description always did (generate.build_description before 0.3.2): the
    seller's text, with the new title as its first line.
    """
    body = body.strip()
    if not body:
        return f"{title}."
    if title and (title.lower() in body.lower() or body.lower().startswith(title.lower()[:40])):
        return body
    return f"{title}.\n\n{body}"


def initial_template(description: str, source_title: str, *, lang: str = "tr") -> str:
    """The description template a seller starts from: the template listing's own
    description, its title turned into the title placeholder.

    Filled in for a draft it gives exactly what a draft gets with no template saved
    (fallback), so saving it unchanged changes nothing.
    """
    placeholder = PLACEHOLDERS.get(lang, PLACEHOLDERS["tr"])["title"]
    body = replace_title(description, source_title, placeholder)
    return _with_opening(body, placeholder)


def fallback(description: str, source_title: str, title: str) -> str:
    """A draft's description with no description template saved."""
    return _with_opening(replace_title(description, source_title, title), title)


def build(seed: Seed | None, *, title: str, description: str, source_title: str = "",
          template_text: str | None = None) -> str:
    """A draft's description: the saved description template, else the fallback."""
    if template_text is not None and template_text.strip():
        return render(template_text, title=title, design=design_name(seed))
    return fallback(description, source_title, title)


# --- sentences about the template's own design ------------------------------------------


def design_words(source_title: str, tags: Sequence[str] | None = None, *,
                 product_words: Iterable[str] = ()) -> list[str]:
    """The words that name the template listing's own design, folded and singular.

    From its title first, then its tags: what generate._distinctive keeps (not a product
    noun, a claim, a generic or a joining word), less the words that only say what the
    product is, what it is made of, where it goes or who it is for (_NOT_DESIGN), and
    less `product_words` (its category's names: "Wallpaper", "Candles").
    """
    skip = set(_NOT_DESIGN)
    for text in product_words or ():
        skip.update(generate._key(token) for token in generate._tokens(str(text)))
    out: list[str] = []
    for text in (source_title, *(tags or [])):
        for key in sorted(generate._distinctive(str(text or ""))):
            if key not in skip and key not in out and len(key) > 2:
                out.append(key)
    return out


def sentences(text: str) -> list[tuple[int, int]]:
    """(start, end) of each sentence: every line, cut again after . ! ? or … and a space."""
    out: list[tuple[int, int]] = []
    text = str(text or "")
    offset = 0
    for line in text.split("\n"):
        cuts = [0] + [m.end() for m in _SENTENCE_END.finditer(line)] + [len(line)]
        for begin, finish in zip(cuts, cuts[1:]):
            piece = line[begin:finish]
            stripped = piece.strip()
            if not stripped:
                continue
            start = offset + begin + (len(piece) - len(piece.lstrip()))
            out.append((start, start + len(stripped)))
        offset += len(line) + 1
    return out


def flags(text: str, words: Iterable[str], *, allowed: Iterable[str] = ()) -> list[Flag]:
    """The sentences of `text` that hold one of `words` (design_words), in order.

    `allowed`: words a draft's own design shares with the template ("mountain" on a
    "mountain goat trail" draft of a "Retro Mountain Sunset" template), never flagged.
    """
    wanted = set(words) - set(allowed)
    if not wanted:
        return []
    out: list[Flag] = []
    for start, end in sentences(text):
        found: list[str] = []
        for token in generate._tokens(text[start:end]):
            key = generate._key(token)
            if key in wanted and key not in found:
                found.append(key)
        if found:
            out.append(Flag(start, end, tuple(found)))
    return out


def own_words(seed: Seed | None, title: str = "") -> set[str]:
    """A draft's own design words: its concept's and its title's."""
    words = set(generate._distinctive(title or ""))
    if seed and seed.text:
        words |= generate._distinctive(seed.text)
    return words


def leftover(description: str, source_title: str, tags: Sequence[str] | None, *,
             seed: Seed | None, title: str, product_words: Iterable[str] = ()) -> list[Flag]:
    """The sentences about the template's own design that a draft with no description
    template saved would still carry: its fallback text, less the words its own design
    shares with the template."""
    words = design_words(source_title, tags, product_words=product_words)
    return flags(fallback(description, source_title, title), words,
                 allowed=own_words(seed, title))


def template_words(template: Template) -> list[str]:
    """design_words for a saved template (its title, tags and category)."""
    return design_words(template.source_title, template.tags,
                        product_words=template.category_path)


def effective(template: Template, *, lang: str = "tr") -> tuple[str, bool]:
    """(the description template drafts are built from, whether the seller saved it).

    Not saved: the template listing's own description as a template (initial_template),
    which fills in to exactly what a draft gets without one.
    """
    if template.description_template is not None:
        return template.description_template, True
    return initial_template(template.description, template.source_title, lang=lang), False


def template_flags(template: Template, text: str | None = None, *,
                   lang: str = "tr") -> list[Flag]:
    """The flagged sentences of `text` (default: the effective description template)."""
    if text is None:
        text = effective(template, lang=lang)[0]
    return flags(text, template_words(template))


def flag_words(found: Sequence[Flag]) -> list[str]:
    """The design words across these flags, first seen first."""
    out: list[str] = []
    for flag in found:
        for word in flag.words:
            if word not in out:
                out.append(word)
    return out


def leftover_message(found: Sequence[Flag]) -> str:
    """The run's (English) note on a draft whose description still has such sentences."""
    words = ", ".join(flag_words(found)[:4])
    n = len(found)
    return (f"the description still has {n} sentence{'s' if n != 1 else ''} about the "
            f"template listing's own design ({words}); edit the description template "
            "on the template page")


__all__ = [
    "MAX_CHARS", "PLACEHOLDERS", "Flag", "build", "design_name", "design_words", "effective",
    "fallback", "flag_words", "flags", "has_placeholder", "initial_template", "leftover",
    "leftover_message", "own_words", "placeholder_name", "render", "replace_title",
    "sentences", "template_flags", "template_words", "title_segments",
    "unknown_placeholders",
]
