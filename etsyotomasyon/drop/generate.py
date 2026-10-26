"""Building a title and thirteen tags from a concept and what actually ranks.

There is no search volume to work from — Etsy publishes none — so this does the only
honest thing available: it looks at the listings Etsy returns for the concept and
reuses the vocabulary they share. That is a measurement, not a prediction, and the
interface says so wherever these numbers appear.

A title is built the way a good Etsy title reads: the concept and the product first
("Retro Mountain Sunset Shirt"), then three or four market phrases that each add a new
search ("Nature Lover Gift, Hiking Shirt, Outdoor Adventure Tee…"), and who it is for
last when the market says so. Single words on their own ("Gift", "Him") never make a
segment, a phrase that only repeats the title is skipped, and a phrase about another
product ("Vinyl Sticker" in a shirt's title, because the concept search also returned
stickers) is left out. Claims about the physical product — a size, a material, a
blank's brand, "personalized" — are only used when the seller's own template listing
makes them too: the market's shirts may be Comfort Colors, the seller's may not be.

Everything here is deliberately deterministic. The same design and the same market
sample produce the same title twice, which is what makes a hundred-product batch
reviewable: a seller who checks ten rows has learned something about the other ninety.

The hard limits are enforced at the point of construction, not checked afterwards, so
this module cannot emit something `listings.build_payload` would reject: 140
characters, Etsy's title characters (letters, digits, punctuation, maths symbols and
spaces; "%", ":", "&" and "+" once each), thirteen tags of at most twenty characters.
"""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Union

from .. import vocab
from ..config import MAX_TAG_LEN, MAX_TAGS, MAX_TITLE_LEN
from ..listings import TITLE_ONCE, bad_tag_chars, title_char_ok
from ..seo import SHARED_TAGS_MIN, STOPWORDS, tag_key
from ..seo import _near_duplicates as _seo_near_duplicates
from ..seo import words as _seo_words
from .seeds import Seed, fold

if TYPE_CHECKING:  # pragma: no cover
    from ..seo import MarketReport

# The seller's own text about the product: the template's title, tags, description.
Hint = Union[str, Sequence[str], None]

# The concept and product, then at most four market phrases. Etsy truncates a title on a
# mobile card after about 40 characters, so the concept must land inside that window;
# everything after is for the search index, not the buyer's eye.
MAX_TITLE_SEGMENTS = 5
# At most this many product nouns in one title ("Shirt … T-Shirt … Tee … Shirt"), and
# the same one at most twice.
MAX_TITLE_NOUNS = 4
# At most this many phrases may bring a word of the concept back ("Dog Dad Shirt, Dog
# Lover Gift"): once or twice reads naturally, on every segment it is stuffing.
MAX_ECHOES = 2

# --- vocabulary ---------------------------------------------------------------------

# Product nouns, as tokens -> (family, how a title spells it). The family keeps a mug's
# title from borrowing "Poster" from a concept search that also returned posters.
_PRODUCTS = vocab.PRODUCTS
_NOUN_WORDS = vocab.PRODUCT_WORDS
# Phrases about one kind that suit another: "Living Room Decor" on a print or a pillow.
_FITS = {"decor": {"wallart", "wallpaper", "pillow", "blanket", "flag", "stake", "ornament",
                   "coaster", "towel", "puzzle"}}
# Too vague to name the product at the head of a title on their own.
_NOT_A_HEAD = {"Art", "Decor", "Home Decor", "Case", "Bag", "Cup", "Print"}
# Garments, where "for Men and Women" is how Etsy titles name the audience.
_APPAREL = {"shirt", "tank", "sweatshirt", "hoodie", "baby", "hat", "socks"}
# A physical product's title must not promise a file.
_DIGITAL_WORDS = {"printable", "digital", "download", "downloadable", "instant", "svg",
                  "png", "pdf", "jpg", "sublimation"}

# Words that carry no search of their own: a phrase must add something besides these.
_GENERIC = vocab.GENERIC
# Real searches, but marketplace-wide ones: a phrase whose only new words are these
# ("Graphic Tee", "Birthday Gift") ranks below one about the design's own theme.
_WEAK = vocab.WEAK
_AUDIENCE_WOMEN = vocab.AUDIENCE_WOMEN
_AUDIENCE_MEN = vocab.AUDIENCE_MEN
_AUDIENCE = vocab.AUDIENCE
# Padding around a product ("Wall Decor", "Home Decor"): a phrase of only these adds no
# search, however many of them are new to the title.
_FILLER = vocab.FILLER
# What the product is made of or how it is made: true of some listings in the market,
# not necessarily of this one. Used only when the seller's own template says so, and so
# is a size (11oz, 8x10, 20oz); "Class of 2026" or "40th Birthday" is the design's theme.
_CLAIMS = {
    "personalized", "personalised", "custom", "customized", "customised", "monogram",
    "monogrammed", "engraved", "embroidered", "handmade", "handpainted", "hand", "painted",
    "comfort", "colors", "colours", "color", "colour", "gildan", "bella", "canvas", "cotton",
    "organic", "ceramic", "enamel", "glass", "stainless", "steel", "vinyl", "waterproof",
    "glossy", "matte", "holographic", "die", "kiss", "framed", "unframed", "oversized",
    "slim", "protective", "magsafe", "set", "bundle", "pack", "piece", "pieces", "wood",
    "wooden", "metal", "leather", "linen", "silk", "gold", "silver", "sterling", "plated",
    "iphone", "samsung", "galaxy", "pixel", "matching", "name", "names", "photo", "free",
    "shipping", "discount", "bestseller", "reusable", "insulated", "dishwasher",
    "microwave", "safe", "large", "small", "mini", "xl",
}
# A word that cannot start a phrase: "Lover Gift" is half of "Nature Lover Gift".
_DEPENDENT_START = {"lover", "lovers", "loving", "themed", "inspired", "style", "shaped"}
# After a product noun or "gift", only these may follow inside one phrase; anything else
# means the n-gram ran across a comma in the listing's title ("shirt hiking shirt").
_AFTER_HEAD = {"gift", "gifts", "set", "idea", "ideas", "box", "card", "bag", "basket",
               "tag", "wrap", "guide", "bundle"}
_HEADS = {"gift", "gifts"}
_MINOR = vocab.MINOR
_CASING = {
    "tshirt": "T-Shirt", "t-shirt": "T-Shirt", "iphone": "iPhone", "ipad": "iPad",
    "airpods": "AirPods", "diy": "DIY", "svg": "SVG", "png": "PNG", "pdf": "PDF",
    "usa": "USA", "uk": "UK", "lgbt": "LGBT", "lgbtq": "LGBTQ", "bff": "BFF", "3d": "3D",
    "xl": "XL", "xxl": "XXL", "nyc": "NYC",
}
# Words that never make a title about one design: joining words, generic and weak ones,
# who it is for, "lover", "gift", claims about the product (material, size, brand).
_PLAIN = (_GENERIC | _WEAK | _AUDIENCE | _DEPENDENT_START | _HEADS | _MINOR | _CLAIMS
          | _DIGITAL_WORDS | vocab.ADHESIVE_WORDS | vocab.FILLER
          | {"graphic", "top", "tee", "gift", "unisex"})
# How a product noun is spelled in a tag: Etsy tags take letters, digits, spaces, - and '.
_TAG_NOUNS = {"T-Shirt": "tshirt", "iPhone Case": "iphone case"}
# Product names that are a search of their own, not a synonym: "iphone case" is not
# "phone case", while "t-shirt", "tee" and "shirt" are one search to a tag list.
_OWN_SEARCH = {"iPhone Case", "Wallpaper", "Mural", "Wall Mural"}
# ... except that "wall mural" and "mural" are one: "lemon mural", "lemon wall mural".
_BAG_NAME = {"Wall Mural": "mural"}
# What a buyer's search for a design adds to its name, by product: "lemon wallpaper",
# "lemon mural", "dog dad gift". Only kinds that are a search of their own (_bag).
_TAG_FORMS = {
    "wallpaper": ("mural", "decor"), "wallart": ("wall art", "decor"),
    "shirt": ("gift",), "sweatshirt": ("gift",), "hoodie": ("gift",), "tank": ("gift",),
    "mug": ("gift",), "tumbler": ("gift",), "tote": ("gift",), "pillow": ("decor",),
    "blanket": ("gift",), "ornament": ("gift",),
}
# A look or a room: the market phrase that closes a title before the material qualifier.
_STYLE_ROOM = vocab.ROOMS | vocab.STYLES
# Words a tag may be made of and still be about nothing in particular: "wallpaper mural",
# "removable wallpaper", "gift for her", "graphic tee".
_SHAPE_WORDS = (vocab.GENERIC | vocab.WEAK | vocab.AUDIENCE | vocab.ADHESIVE_WORDS
                | vocab.FILLER | vocab.MINOR | vocab.PRODUCT_WORDS | _CLAIMS)
# A colour or a plain adjective in front of the motif: "sage", "pastel", "dark".
_MODIFIERS = vocab.COLOURS | vocab.WEAK
# Generic shop-wide tags a draft may carry: they put the shop into those searches, but
# every draft has them, so they cannot be what tells this design apart. A tag is generic
# when the template's own tag list has it, or when it is among the market's most used
# (GENERIC_TOP of them, at GENERIC_SHARE of the sample or more) and holds no word of the
# design.
GENERIC_SLOTS = 3
GENERIC_SHARE = 0.25
GENERIC_TOP = 10
# Two drafts share at most this many tags: seo.SHARED_TAGS_MIN of them is cannibalising.
MAX_SHARED_TAGS = SHARED_TAGS_MIN - 1
# A number glued to its unit, the way sellers write it: 11oz, 8x10, 70s, 3d.
_UNITS = {"oz", "ml", "l", "cm", "mm", "inch", "in", "ft", "s", "th", "st", "nd", "rd", "d",
          "x", "k", "pcs", "pc"}
_MEASURE = re.compile(r"^\d+(oz|ml|l|cl|cm|mm|in|inch|inches|ft|pcs|pc|pk|x\d+[a-z]*)$")
# Letters only Turkish (among the languages sellers here write in) spells with: a text
# holding one is Turkish, and its "i" capitalises to "İ" ("Kedi Pati İzi").
_TURKISH_LETTERS = set("çğıöşüÇĞİÖŞÜ")


@dataclass
class Generated:
    title: str
    tags: list[str]
    description: str = ""
    sources: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def is_turkish(*texts: object) -> bool:
    """Whether any of these texts is written in Turkish (holds a Turkish-only letter)."""
    return any(_TURKISH_LETTERS & set(str(text or "")) for text in texts)


def _upper_first(part: str, turkish: bool) -> str:
    first = part[:1]
    if turkish and first == "i":
        return "İ" + part[1:]
    return first.upper() + part[1:]


def titlecase(text: str, *, turkish: bool | None = None) -> str:
    """Capitalise each word the way a listing title does.

    Without str.title()'s habit of mangling apostrophes ("Mom'S"); small joining words
    stay small after the first ("Shirt for Men and Women"); a few words keep their own
    spelling ("T-Shirt", "iPhone", "DIY"); "11oz" and "70s" stay as they are. Turkish
    text capitalises "i" to "İ" ("kedi pati izi" -> "Kedi Pati İzi"); `turkish=None`
    decides from the text itself (is_turkish), so English "ice" stays "Ice".
    """
    if turkish is None:
        turkish = is_turkish(text)
    out = []
    for index, word in enumerate(text.split()):
        low = word.lower()
        if low in _CASING:
            out.append(_CASING[low])
        elif index and low in _MINOR:
            out.append(low)
        elif word[:1].isdigit():
            out.append(word)
        else:
            out.append("-".join(_upper_first(part, turkish) for part in word.split("-")))
    return " ".join(out)


def clean_tag(raw: str) -> str:
    """Strip a tag down to something Etsy will accept, or return '' if nothing is left."""
    text = " ".join(str(raw or "").split()).strip().lower()
    if not text:
        return ""
    bad = bad_tag_chars(text)
    if bad:
        text = " ".join("".join(ch for ch in text if ch not in bad).split())
    return text if len(text) <= MAX_TAG_LEN else ""


class SellerText(list):
    """The seller's own words about the product: what `hint_from` returns.

    A plain list of strings (the template's title, then its tags, then its description),
    which is all `Hint` asks for; the parts it was made from ride along as attributes, so
    the builders can tell the title from the tags, and know the materials, without a
    second argument: `title`, `tags`, `description`, `materials`.
    """

    def __init__(self, parts: Sequence[str] = (), *, title: str = "",
                 tags: Sequence[str] = (), description: str = "",
                 materials: Sequence[str] = ()) -> None:
        super().__init__(parts)
        self.title = title
        self.tags = list(tags)
        self.description = description
        self.materials = list(materials)


def hint_from(title: str = "", tags: Sequence[str] | None = None,
              description: str = "", materials: Sequence[str] | None = None) -> SellerText:
    """The seller's own words about the product, in the order the builders trust them.

    `hint_from(template.source_title, template.tags, template.description,
    template.materials)` is what `build_title(..., product_hint=)` and
    `build_tags(..., product_hint=)` expect. The materials are not part of the list (the
    title, tags and description are); they only tell the builders what else the seller
    says about the product (a template that lists "Traditional" among its materials
    sells paste-up paper too).
    """
    texts = [str(text) for text in (title, *(tags or []), description) if text]
    return SellerText(
        texts, title=str(title or ""), tags=[str(t) for t in tags or [] if t],
        description=str(description or ""), materials=[str(m) for m in materials or [] if m],
    )


@dataclass
class _Seller:
    """What the template says about how the product is made, and its own tags."""

    adhesive: set[str] = field(default_factory=set)  # claims its title, tags, materials make
    paste: bool = False  # it also sells (or only is) a pasted product
    tags: list[str] = field(default_factory=list)  # its own tags, cleaned
    qualifier: str = ""  # how a title closes: "Peel and Stick or Traditional"


def _seller(hint: Hint) -> _Seller:
    """The claims a template makes about how its product goes on, and its own tags.

    "Removable", "self adhesive", "renter friendly", "temporary" and "peel and stick" are
    claims about the whole listing only when the template makes them (in its title, tags
    or materials) and nowhere says some of it is pasted: a template that lists
    "Traditional" next to "Peel and Stick" sells both, and "removable" is then false for
    one of the two (vocab.mentions_paste). Then none of them is written into a title or a
    tag, and the title closes with the qualifier "Peel and Stick or Traditional" instead.
    A hint that is not `SellerText` (a bare string or list) is one block of the seller's
    words: its tags are not known, and nothing in it is told from a description.
    """
    if isinstance(hint, SellerText):
        claims = [hint.title, *hint.tags, *hint.materials]
        prose = [hint.description]
        tags = [t for t in (clean_tag(t) for t in hint.tags) if t]
    else:
        claims, prose, tags = _hint_texts(hint), [], []
    adhesive: set[str] = set()
    for text in claims:
        adhesive |= vocab.adhesive_claims(_tokens(text))
    paste = vocab.mentions_paste(claims, prose)
    return _Seller(adhesive, paste, list(dict.fromkeys(tags)), _qualifier(adhesive, paste))


def _qualifier(adhesive: set[str], paste: bool) -> str:
    """The material qualifier that closes a title, or "" when the template makes no claim."""
    if not adhesive:
        return ""
    base = ("Peel and Stick" if adhesive & {vocab.PEEL_AND_STICK, vocab.ADHESIVE}
            else "Removable" if vocab.REMOVABLE in adhesive else "")
    if not base:
        return ""
    return f"{base} or Traditional" if paste else base


def research_keyword(seed: Seed, hint: Hint = None) -> str:
    """What to search Etsy for: the concept, plus the product when it does not say it.

    The template listing decides what the product is (as for the title): "dog dad paw
    print" on a shirt template searches "dog dad paw print shirt", where the concept
    alone returns posters ("print") and leaves the shirt's title nothing to borrow. A
    concept that already names the product ("retro sunset mug"), or ends in "gift", is
    searched as it is, and so is any concept when the template names no product.
    """
    text = " ".join(seed.text.split())
    product = _product(seed, None, hint, [])
    tokens = _tokens(text)
    if not product.family or product.in_concept or not tokens or tokens[-1] in _HEADS:
        return text
    return f"{text} {product.display.lower()}"


def _distinctive(text: str) -> set[str]:
    """The words that make a title about one design: not the product, not generic."""
    tokens = _tokens(text)
    nouns = {i for start, end, *_ in _nouns(tokens) for i in range(start, end)}
    return {
        _key(token) for index, token in enumerate(tokens)
        if index not in nouns and token not in STOPWORDS and token not in _PLAIN
        and _key(token) not in _PLAIN and not token[:1].isdigit() and len(_key(token)) > 1
    }


def product_tags(tags: Sequence[str] | None, template_title: str = "",
                 seed: Seed | None = None, hint: Hint = None) -> list[str]:
    """The template listing's tags that suit any design of its product, cleaned.

    Its tags about its own design are left out: a tag holding a distinctive word of the
    template's title ("retro mountain sun", "hiking gift" on a "Retro Mountain Sunset
    Shirt, ... Hiking Gift") would put another design's words on this one. A word the
    new design shares ("mountain" for "mountain goat trail") is fine. Without a title
    nothing is known to be the template's own, so every tag is kept.

    With `hint` (`hint_from(...)`), a tag whose adhesive claim ("removable", "self
    adhesive", "renter friendly") is not true of the whole listing is left out too: the
    template that also sells paste-up paper cannot tag every draft "removable wallpaper".
    """
    own = _distinctive(template_title) - (_distinctive(seed.text) if seed else set())
    allowed = _allowed_adhesive(hint) if hint is not None else None

    def about_the_template(tag: str) -> bool:
        # A word of it, or the start of one: Etsy's 20 characters cut "retro mountain
        # sunset" to "retro mountain sun".
        return any(
            word in own or (len(word) >= 3 and any(d.startswith(word) for d in own))
            for word in _distinctive(tag)
        )

    out: list[str] = []
    for raw in tags or []:
        tag = clean_tag(raw)
        if allowed is not None and tag and vocab.adhesive_claims(_tokens(tag)) - allowed:
            continue
        if tag and tag not in out and not (own and about_the_template(tag)):
            out.append(tag)
    return out


def _allowed_adhesive(hint: Hint) -> set[str]:
    """The adhesive claims that may be written for this seller's listings: the ones the
    template itself makes, and none at all when it also sells a pasted product."""
    seller = _seller(hint)
    return set() if seller.paste else set(seller.adhesive)


# --- words and phrases --------------------------------------------------------------


def _singular(token: str) -> str:
    if token.endswith("s") and token[:-1] in _NOUN_WORDS:
        return token[:-1]
    return token


def _key(token: str) -> str:
    """What makes two spellings the same word: case, accents, a plural "s"."""
    key = _singular(fold(token).replace("'", ""))
    if len(key) > 3 and key.endswith("s") and not key.endswith(("ss", "us", "is")):
        key = key[:-1]
    return key


def _tokens(text: str) -> list[str]:
    """Words as seo.research counts them, with what it splits put back together.

    "T-Shirt" is one word ("tshirt"), and so are "11oz", "8x10" and "70s", which the
    word pattern cuts into a number and a unit.
    """
    raw = _seo_words(unicodedata.normalize("NFC", str(text or "")))
    out: list[str] = []
    for word in raw:
        if word in ("shirt", "shirts") and out and out[-1] in ("t", "tee"):
            out[-1] = "tshirt"
        elif out and out[-1][:1].isdigit() and (
            (word in _UNITS and not out[-1][-1:].isalpha())
            or (word.isdigit() and out[-1].endswith("x"))
        ):
            out[-1] += word
        else:
            out.append(word)
    return out


def _nouns(tokens: Sequence[str]) -> list[tuple[int, int, str, str]]:
    """(start, end, family, display) for each product noun, longest match first."""
    found = []
    index = 0
    singular = [_singular(t) for t in tokens]
    while index < len(tokens):
        pair = tuple(singular[index:index + 2])
        if len(pair) == 2 and pair in _PRODUCTS:
            found.append((index, index + 2, *_PRODUCTS[pair]))
            index += 2
            continue
        one = (singular[index],)
        if one in _PRODUCTS:
            found.append((index, index + 1, *_PRODUCTS[one]))
        index += 1
    return found


def _is_claim(token: str) -> bool:
    folded = fold(token)
    return folded in _CLAIMS or _key(token) in _CLAIMS or bool(_MEASURE.match(folded))


@dataclass
class _Phrase:
    tokens: tuple[str, ...]
    count: int
    tagged: bool = False  # a seller chose it as a tag, rather than it being a title n-gram
    fragment: bool = False  # a piece of a longer phrase the market uses as often

    def __post_init__(self) -> None:
        self.keys = [_key(t) for t in self.tokens]
        self.nouns = _nouns(self.tokens)
        self.noun_positions = {i for start, end, *_ in self.nouns for i in range(start, end)}
        self.family = self.nouns[-1][2] if self.nouns else None
        # The head noun is the product; another product's noun before it describes it
        # ("Water Bottle Decal", "Coffee Mug Sticker") and counts as a word.
        head = set(range(self.nouns[-1][0], self.nouns[-1][1])) if self.nouns else set()
        self.words = {k for i, k in enumerate(self.keys)
                      if i not in head and k not in STOPWORDS}
        self.text = " ".join(self.tokens)
        self.claims = {t for t in self.tokens if _is_claim(t)}
        # "peel and stick", "removable": claims about how the product goes on, which only
        # the seller's own template may make (and not a template that sells paste too).
        self.adhesive = vocab.adhesive_claims(self.tokens)
        # A room or a look ("kitchen wallpaper", "cottagecore bedroom decor"): the phrase
        # a title ends on, after the ones that name what the design is.
        self.styled = any(k in _STYLE_ROOM for k in self.keys)

    @property
    def broken(self) -> bool:
        """An n-gram that ran across a comma, or half of a phrase."""
        tokens = self.tokens
        if len(tokens) < 2 or tokens[0] in STOPWORDS or tokens[-1] in STOPWORDS:
            return True
        if tokens[0] in _DEPENDENT_START:
            return True
        if any(len(t) == 1 and t.isalpha() for t in tokens):
            return True
        # Half of "peel and stick": the stopword cut it in two.
        if tokens[-1] == "peel" or tokens[0] == "stick":
            return True
        heads = {end - 1 for _start, end, *_ in self.nouns}
        heads |= {i for i, t in enumerate(tokens) if t in _HEADS}
        for i in heads:
            if i + 1 < len(tokens):
                follower = tokens[i + 1]
                if not (follower in STOPWORDS or follower in _AFTER_HEAD
                        or (i + 1) in self.noun_positions):
                    return True
        return False

    @property
    def audience(self) -> set[str]:
        return {k for k in self.keys if k in _AUDIENCE}

    def meaningful(self) -> set[str]:
        return {k for k in self.words
                if k not in _GENERIC and k not in _AUDIENCE and len(k) > 1}

    def novelty(self, used: dict[str, int]) -> tuple[float, set[str]]:
        """How much new search this phrase adds: a theme word counts 1, "gift" or
        "lover" a half, so "Coffee Lover Gift" still adds to "But First Coffee Mug".
        Padding ("Wall Decor", "Home Decor") counts a half at most, never a new search."""
        new = {k for k in self.meaningful() if k not in used and k not in _FILLER}
        if new:
            return float(len(new)), new
        generic = {k for k in self.words if (k in _GENERIC or k in _FILLER) and k not in used}
        return 0.5 * len(generic), new


# What an HTML entity leaves once the word pattern has cut it up: "mother&#39;s" was
# counted as "mother 39 s". A research cache written before titles were decoded (up to
# seven days old) can still hold these.
_ENTITY_BITS = {"39", "34", "amp", "quot", "apos", "nbsp", "x27"}
# A number joined to the next by a point, a comma or a slash: "8.5x11", "3/4", "1,000",
# "12.5oz". seo.research keeps such a size whole, but _tokens splits it again ("8 5x11",
# "3 4 sleeve"), and Etsy tags cannot hold ".", "," or "/" at all; the row is left out.
_JOINED_NUMBER = re.compile(r"\d[.,/]\d")


def _rows(rows: object) -> list[tuple[str, int]]:
    out = []
    for row in rows or []:  # type: ignore[union-attr]
        try:
            text, count = html.unescape(str(row[0])), int(row[1])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if _ENTITY_BITS & set(_seo_words(text)) or _JOINED_NUMBER.search(text):
            continue
        out.append((text, count))
    return out


def _has_market(report: MarketReport | None) -> bool:
    return report is not None and not getattr(report, "empty", True)


def _market_phrases(report: MarketReport | None) -> list[_Phrase]:
    """Every multi-word phrase the market uses, from its titles and its tags, merged."""
    if not _has_market(report):
        return []
    assert report is not None
    merged: dict[tuple[str, ...], _Phrase] = {}
    for rows, tagged in ((report.tags, True), (report.phrases, False)):
        for text, count in _rows(rows):
            tokens = tuple(_tokens(text))
            if len(tokens) < 2:
                continue
            known = merged.get(tokens)
            if known is None:
                merged[tokens] = _Phrase(tokens, count, tagged)
            else:
                known.count = max(known.count, count)
                known.tagged = known.tagged or tagged
    phrases = list(merged.values())
    # "Player Tee" out of "Pickleball Player Tee", "Year Gift" out of "End of Year Gift":
    # an n-gram that only ever appears inside a longer phrase is not a phrase of its own.
    # (Title n-grams never span "of" or "for", so one cut there is always a piece.)
    whole = [p for p in phrases if not p.broken]
    for short in phrases:
        if short.tagged:
            continue
        size = len(short.tokens)
        for longer in whole:
            if len(longer.tokens) <= size:
                continue
            starts = [i for i in range(len(longer.tokens) - size + 1)
                      if longer.tokens[i:i + size] == short.tokens]
            cut_at_stopword = any(i and longer.tokens[i - 1] in STOPWORDS for i in starts)
            if starts and (cut_at_stopword or longer.count >= 0.8 * short.count):
                short.fragment = True
                break
    return phrases


def _word_counts(report: MarketReport | None) -> dict[str, int]:
    """How many sampled listings use each single word, from titles and tags."""
    counts: dict[str, int] = {}
    if not _has_market(report):
        return counts
    assert report is not None
    for rows in (report.phrases, report.tags):
        for text, count in _rows(rows):
            tokens = _tokens(text)
            if len(tokens) == 1:
                key = _singular(tokens[0])
                counts[key] = max(counts.get(key, 0), count)
    return counts


def _share(count: int, report: MarketReport | None) -> float:
    sampled = int(getattr(report, "sampled", 0) or 0) if report is not None else 0
    return count / sampled if sampled else 0.0


# --- what the product is ------------------------------------------------------------


@dataclass
class _Product:
    family: str | None = None
    display: str = ""
    in_concept: bool = False
    audience: set[str] = field(default_factory=set)  # women / men, as the template says
    claims: set[str] = field(default_factory=set)  # claim words the seller's text makes
    adhesive: set[str] = field(default_factory=set)  # adhesive claims that may be written
    qualifier: str = ""  # how the title closes ("Peel and Stick or Traditional")
    template_tags: list[str] = field(default_factory=list)  # the template's own tags


def _hint_texts(hint: Hint) -> list[str]:
    if not hint:
        return []
    if isinstance(hint, str):
        return [hint]
    return [str(h) for h in hint if h]


def _head_noun(nouns: Sequence[tuple[int, int, str, str]]) -> tuple[int, int, str, str]:
    """The noun that names the product in a title's opening phrase: the last one, except
    that "Wallpaper Mural" is a wallpaper, not a mural."""
    last = nouns[-1]
    if last[2] == "wallpaper":
        plain = [n for n in nouns if n[2] == "wallpaper" and n[3] == "Wallpaper"]
        if plain:
            return plain[0]
    return last


def _product(seed: Seed, report: MarketReport | None, hint: Hint,
             phrases: list[_Phrase]) -> _Product:
    """Which product this listing is: named in the file, in the template, or by the market."""
    texts = _hint_texts(hint)
    hint_tokens = [t for text in texts for t in _tokens(text)]
    product = _Product(claims={fold(t) for t in hint_tokens if _is_claim(t)})
    product.claims |= {_key(t) for t in hint_tokens if _is_claim(t)}
    seller = _seller(hint)
    product.adhesive = set() if seller.paste else set(seller.adhesive)
    product.qualifier = seller.qualifier
    product.template_tags = seller.tags
    hint_keys = {_key(t) for t in hint_tokens}
    if hint_keys & _AUDIENCE_WOMEN:
        product.audience.add("women")
    if hint_keys & _AUDIENCE_MEN:
        product.audience.add("men")

    # The seller's template listing decides first: the draft is created in its category,
    # so a design called "route-66-poster" on a shirt template is a shirt. Its title's
    # opening names the product; else the kind its tags name most.
    tally: dict[str, int] = {}
    shown: dict[str, str] = {}
    for index, text in enumerate(texts):
        first = re.split(r"[,|/:;]| - ", text, maxsplit=1)[0]
        nouns = [n for n in _nouns(_tokens(first)) if n[2] not in _FITS]
        if index == 0 and nouns:
            head = _head_noun(nouns)
            tally = {head[2]: 1}
            shown = {head[2]: head[3]}
            break
        for _s, _e, family, display in _nouns(_tokens(text)):
            tally[family] = tally.get(family, 0) + 1
            shown.setdefault(family, display)
    families = [f for f in sorted(tally) if f not in _FITS]
    concept = _nouns(_tokens(seed.text))
    if families:
        family = max(families, key=lambda f: tally[f])
        product.family, product.display = family, shown[family]
        # "Retro Sunset Mug" on a mug template: the name already says it.
        product.in_concept = any(f == family for _s, _e, f, _d in concept)
        return product

    # Then the file name, when it ends on what the product is: "retro-sunset-mug".
    tokens = _tokens(seed.text)
    for _start, end, family, display in reversed(concept):
        if family not in _FITS and all(t in _HEADS for t in tokens[end:]):
            product.family, product.display, product.in_concept = family, display, True
            return product

    # Otherwise the product most of the ranking listings are, when enough of them agree.
    if not _has_market(report):
        return product
    # How often each noun appears: on its own ("shirt") and inside phrases ("phone case").
    seen: dict[str, int] = {}
    kinds: dict[str, str] = {}
    for word, count in _word_counts(report).items():
        entry = _PRODUCTS.get((word,))
        if entry:
            seen[entry[1]] = max(seen.get(entry[1], 0), count)
            kinds[entry[1]] = entry[0]
    for phrase in phrases:
        if phrase.family and not phrase.broken:
            display = phrase.nouns[-1][3]
            seen[display] = max(seen.get(display, 0), phrase.count)
            kinds[display] = phrase.family
    totals: dict[str, int] = {}
    for display, count in seen.items():
        totals[kinds[display]] = max(totals.get(kinds[display], 0), count)
    families = [f for f in sorted(totals) if f not in _FITS]
    if not families:
        return product
    family = max(families, key=lambda f: totals[f])
    if _share(totals[family], report) < 0.2:
        return product
    # "Phone Case" rather than "Case", "Tote Bag" rather than "Bag", when the market
    # spells it out often enough.
    names = sorted((d for d in seen if kinds[d] == family), key=lambda d: (-seen[d], d))
    common = [d for d in names if d not in _NOT_A_HEAD and seen[d] >= 0.5 * seen[names[0]]]
    if common:
        longest = max(len(d.split()) for d in common)
        display = next(d for d in common if len(d.split()) == longest)
    else:
        display = names[0]
    product.family, product.display = family, display
    product.in_concept = any(f == family for _s, _e, f, _d in _nouns(_tokens(seed.text)))
    return product


# --- the title ----------------------------------------------------------------------


def _phrase_display(tokens: Sequence[str]) -> str:
    nouns = {start: (end, shown) for start, end, _f, shown in _nouns(tokens)}
    words: list[str] = []
    index = 0
    while index < len(tokens):
        if index in nouns:
            end, shown = nouns[index]
            words.append(shown)
            index = end
            continue
        words.append(tokens[index])
        index += 1
    return titlecase(" ".join(words))


def _title_safe(text: str) -> str:
    """Only what Etsy accepts in a title (listings.title_char_ok, TITLE_ONCE)."""
    text = unicodedata.normalize("NFC", text)
    kept = []
    for ch in text:
        if title_char_ok(ch):
            kept.append(ch)
        elif ch.isspace():
            kept.append(" ")
    text = "".join(kept)
    for ch, word in TITLE_ONCE.items():
        first = text.find(ch)
        if first >= 0 and text.count(ch) > 1:
            text = text[: first + 1] + text[first + 1:].replace(ch, f" {word} ")
    return " ".join(text.split())


def _fit(text: str, limit: int) -> str:
    """Cut at a word boundary, never through a word."""
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit + 1)
    return (text[:cut] if cut > 0 else text[:limit]).rstrip(" ,-")


def _allowed(phrase: _Phrase, product: _Product) -> bool:
    """A phrase about this product, that promises nothing the seller's own text does not."""
    if phrase.broken or phrase.fragment:
        return False
    if phrase.family is not None and not _fits(product.family, [phrase.family]):
        return False
    if product.family != "digital" and set(phrase.keys) & _DIGITAL_WORDS:
        return False
    if phrase.adhesive - product.adhesive:
        return False  # "removable": the template does not say so, or also sells paste
    return all(fold(c) in product.claims or _key(c) in product.claims for c in phrase.claims)


def _other_products(phrases: list[_Phrase], candidates: list[_Phrase],
                    product: _Product) -> list[_Phrase]:
    """Phrases that name no product but only ever go with another one.

    A concept search returns other products too; "Coffee Lover Gift" comes from the
    mugs among them, and the market shows it: "coffee" only appears in "coffee mug"
    and "coffee cup". On a sticker it would be a stranger's phrase.
    """
    families: dict[str, dict[str, int]] = {}
    for phrase in phrases:
        if phrase.family and not phrase.broken:
            for word in phrase.words:
                seen = families.setdefault(word, {})
                seen[phrase.family] = max(seen.get(phrase.family, 0), phrase.count)
    out = []
    for phrase in candidates:
        if phrase.family is not None:
            continue
        for word in phrase.meaningful():
            seen = families.get(word, {})
            if seen and not _fits(product.family, seen) and (
                phrase.count <= 1.2 * max(seen.values())
            ):
                out.append(phrase)
                break
    return out


def _spans(phrase: _Phrase, concept_last: str) -> bool:
    """A title n-gram that ran on past the concept: "sunset, gift…" read as one phrase.

    The concept opens most of the titles in its own search, so its last word followed
    by anything but this product's noun is the comma after it, not a phrase.
    """
    if phrase.tagged or not concept_last:
        return False
    nouns = {start for start, _end, *_ in phrase.nouns}
    return any(
        key == concept_last and index + 1 < len(phrase.keys) and index + 1 not in nouns
        for index, key in enumerate(phrase.keys)
    )


def _family_count(phrases: list[_Phrase], report: MarketReport | None,
                  family: str | None) -> int:
    """How many sampled listings name this kind of product, by its commonest noun."""
    best = 0
    for word, count in _word_counts(report).items():
        entry = _PRODUCTS.get((word,))
        if entry and entry[0] == family:
            best = max(best, count)
    for phrase in phrases:
        if phrase.family == family and not phrase.broken:
            best = max(best, phrase.count)
    return best


def _fits(family: str | None, families: object) -> bool:
    """Whether phrases about these product kinds may go on this one."""
    return any(f == family or family in _FITS.get(f, ()) for f in families)  # type: ignore


@dataclass
class _Plan:
    head: str
    segments: list[str]  # the chosen market phrases, as the title spells them
    chosen: list[_Phrase]
    phrases: list[_Phrase]  # every market phrase this product may use
    product: _Product
    concept_keys: set[str]
    qualifier: str = ""  # the seller's material qualifier, last in the title


def _noun_word(display: str) -> str:
    """The word a reader counts: "Tote Bag" and "Bag" are both a bag."""
    return display.split()[-1].lower()


def _plan(seed: Seed, report: MarketReport | None = None, hint: Hint = None) -> _Plan:
    phrases = _market_phrases(report)
    product = _product(seed, report, hint, phrases)
    concept_tokens = _tokens(seed.text) or seed.text.split()
    concept_keys = {_key(t) for t in concept_tokens if t not in STOPWORDS}
    concept_last = _key(concept_tokens[-1]) if concept_tokens else ""

    # The seller's own name for the design, cased in its own language: Turkish when it,
    # or the template listing's text, is (a Turkish seller's "kedi pati izi").
    turkish = is_turkish(seed.text, *_hint_texts(hint))
    head = _title_safe(titlecase(seed.text, turkish=turkish))
    ends_in_gift = bool(concept_tokens) and concept_tokens[-1] in _HEADS
    if product.family and not product.in_concept and not ends_in_gift:
        head = f"{head} {product.display}"
    head = _fit(head, MAX_TITLE_LEN)

    # Every word the title has used, and how often.
    used: dict[str, int] = {}
    for token in _tokens(head):
        used[_key(token)] = used.get(_key(token), 0) + 1
    nouns_used: dict[str, int] = {}
    primary = ""
    if product.display:
        primary = _noun_word(product.display)
        nouns_used[primary] = 1

    # The material qualifier closes the title ("Peel and Stick or Traditional"): its room
    # is kept back, and it is left out only when the design's own name already says it.
    qualifier = product.qualifier
    if qualifier and {_key(t) for t in _tokens(qualifier)} <= set(used):
        qualifier = ""

    candidates = [p for p in phrases if _allowed(p, product)]
    elsewhere = {id(p) for p in _other_products(phrases, candidates, product)}
    # A phrase that names no product and is used far less often than this product's own
    # noun comes from a corner of the market (the prints in a mug search): "Gallery Wall".
    ours = _family_count(phrases, report, product.family)
    minor = {id(p) for p in candidates if p.family is None and p.count < 0.25 * ours}
    # The qualifier says how the product goes on, once: a phrase that says it again
    # ("Peel and Stick Wallpaper") is left out of a title.
    pool = [p for p in candidates if not p.adhesive]
    chosen: list[_Phrase] = []
    segments: list[str] = []
    length = len(head) + (2 + len(qualifier) if qualifier else 0)
    echoes = 0  # phrases that brought a concept word back

    def choose(kind: str) -> bool:
        """Add the best market phrase of this kind: "buyer" (what the design is) or
        "style" (a room or a look). False when none fits."""
        nonlocal length, echoes
        best: tuple[tuple[float, int, str], _Phrase, str, bool] | None = None
        for phrase in pool:
            if any(phrase is c for c in chosen) or id(phrase) in elsewhere | minor:
                continue
            if phrase.styled != (kind == "style"):
                continue
            novelty, new = phrase.novelty(used)
            if novelty < 1:
                continue
            # One word of the concept may come back, once per phrase and in at most two
            # phrases: "Pickleball Queen Shirt, Pickleball Lover Gift" reads naturally,
            # "Mountain Sunset Tee" after "Retro Mountain Sunset Shirt" does not. "Gift"
            # may come back once, like a product noun: "Nature Lover Gift, Hiker Gift"
            # are two searches.
            repeats = {k for k in phrase.words if k in used
                       and not (k in _HEADS and used[k] < 2)}
            if repeats and (
                kind == "style"  # a room or a look adds new words, it does not echo
                or len(repeats) > 1
                or echoes >= MAX_ECHOES
                or not repeats <= concept_keys
                or any(used[k] > 1 for k in repeats)
                or repeats & (_GENERIC | _AUDIENCE)
            ):
                continue
            if _spans(phrase, concept_last):
                continue
            if phrase.audience and any(k in _AUDIENCE for k in used):
                continue
            # The product's own noun twice at most, any other noun ("Mural" after
            # "Wallpaper") once.
            words = [_noun_word(shown) for _s, _e, family, shown in phrase.nouns
                     if family == product.family]
            if any(nouns_used.get(w, 0) + words.count(w) > (2 if w == primary else 1)
                   for w in words):
                continue
            if sum(nouns_used.values()) + len(words) > MAX_TITLE_NOUNS:
                continue
            shown = _phrase_display(phrase.tokens)
            if length + 2 + len(shown) > MAX_TITLE_LEN:
                continue
            score = _share(phrase.count, report)
            score *= 1 + 0.4 * (novelty - 1)
            if new and new <= _WEAK:
                score *= 0.5
            if len(phrase.tokens) >= 3:
                score *= 1.25
            if phrase.family is None and not (set(phrase.keys) & _HEADS):
                score *= 0.8
            if repeats:
                score *= 0.85
            if phrase.tagged:
                score *= 1.1
            rank = (score, phrase.count, phrase.text)
            if best is None or rank > best[0]:
                best = (rank, phrase, shown, bool(repeats))
        if best is None:
            return False
        _rank, phrase, shown, echoed = best
        echoes += echoed
        chosen.append(phrase)
        segments.append(shown)
        length += 2 + len(shown)
        for token in phrase.tokens:
            used[_key(token)] = used.get(_key(token), 0) + 1
        for _s, _e, family, noun in phrase.nouns:
            if family == product.family:
                nouns_used[_noun_word(noun)] = nouns_used.get(_noun_word(noun), 0) + 1
        return True

    # The design's own phrase leads (the head); then up to two phrases of what it is, then
    # one of the room or look it suits, which a buyer scans last. With no room or look in
    # the market, a third phrase of what it is takes that place.
    for kind in ("buyer", "buyer", "style"):
        if not choose(kind) and kind == "style":
            choose("buyer")

    # The audience goes last, the way a buyer reads it: "..., Camping Shirt for Men and Women".
    order = sorted(range(len(segments)), key=lambda i: bool(chosen[i].audience))
    chosen = [chosen[i] for i in order]
    segments = [segments[i] for i in order]
    _add_audience(segments, chosen, product, report, used, length)
    usable = [p for p in candidates
              if id(p) not in elsewhere and not _spans(p, concept_last)]
    return _Plan(head, segments, chosen, usable, product, concept_keys, qualifier)


def _add_audience(segments: list[str], chosen: list[_Phrase], product: _Product,
                  report: MarketReport | None, used: dict[str, int], length: int) -> None:
    """Close with who it is for, when the template or enough of the market says so."""
    if not segments or any(k in _AUDIENCE for k in used):
        return
    sides = set(product.audience)
    if not sides and _has_market(report):
        counts = _word_counts(report)
        for phrase in _market_phrases(report):
            for key in phrase.audience:
                counts[key] = max(counts.get(key, 0), phrase.count)
        women = max((counts.get(k, 0) for k in _AUDIENCE_WOMEN), default=0)
        men = max((counts.get(k, 0) for k in _AUDIENCE_MEN), default=0)
        unisex = counts.get("unisex", 0)
        if _share(women, report) >= 0.1:
            sides.add("women")
        if _share(men, report) >= 0.1:
            sides.add("men")
        if product.family in _APPAREL and _share(unisex, report) >= 0.1:
            sides |= {"women", "men"}
    if not sides:
        return
    if product.family in _APPAREL:
        if len(sides) == 2:
            closer = "for Men and Women"
        else:
            closer = "for Women" if "women" in sides else "for Men"
        targets = [i for i, p in enumerate(chosen) if p.family == product.family]
    elif len(sides) == 1:
        closer = "for Her" if "women" in sides else "for Him"
        targets = [i for i, p in enumerate(chosen) if p.tokens[-1] in _HEADS]
    else:
        return
    if not targets or length + 1 + len(closer) > MAX_TITLE_LEN:
        return
    index = targets[-1]
    segments[index] = f"{segments[index]} {closer}"
    # Keep it last: a closer reads as the end of the title.
    segments.append(segments.pop(index))
    chosen.append(chosen.pop(index))


def build_title(seed: Seed, report: MarketReport | None = None, *,
                product_hint: Hint = None) -> str:
    """The concept and product first, then 3-4 market phrases that each add a new search.

    `product_hint` is the seller's own text about the product (see `hint_from`): the
    template listing's title first, then its tags and description. It names the product
    when the file name does not, and it is the only thing that lets a size, a material
    or a brand into the title. Never exceeds 140 characters, and only uses characters
    Etsy accepts.
    """
    plan = _plan(seed, report, product_hint)
    parts = [plan.head, *plan.segments]
    if plan.qualifier:
        parts.append(plan.qualifier)
    return _fit(_title_safe(", ".join(parts)), MAX_TITLE_LEN).strip(" ,-|/")


# --- tags ---------------------------------------------------------------------------


def _bag(tag: str) -> frozenset[str]:
    """A tag as the set of words a search matches, with shirt/tee/t-shirt as one.

    "canvas tote" and "canvas tote bag" are one search; "iphone case" is its own.
    """
    tokens = _tokens(tag)
    keys: set[str] = set()
    covered: set[int] = set()
    for start, end, family, shown in _nouns(tokens):
        keys.add("@" + _BAG_NAME.get(shown, shown.lower() if shown in _OWN_SEARCH else family))
        covered.update(range(start, end))
    keys |= {_key(t) for i, t in enumerate(tokens) if i not in covered}
    return frozenset(k for k in keys if k not in STOPWORDS)


def _too_similar(candidate: str, existing: list[str]) -> bool:
    """Catch pairs that would burn two of the thirteen slots on one search.

    'gift'/'gifts', 'mountain shirt'/'mountain t-shirt', 'retro sunset'/'sunset retro',
    and a single word that another tag already contains.
    """
    squashed = candidate.replace(" ", "").replace("-", "").rstrip("s")
    bag = _bag(candidate)
    single = len(_tokens(candidate)) == 1
    for other in existing:
        if candidate == other:
            return True
        if squashed == other.replace(" ", "").replace("-", "").rstrip("s"):
            return True
        other_bag = _bag(other)
        if bag and (bag == other_bag or (single and bag <= other_bag)):
            return True
    return False


def _tag_pieces(phrase: _Phrase) -> list[str]:
    """Shorter tags inside a phrase too long for one: its last three or two words.

    The end keeps the product ("leaf phone case", "paw print shirt"); a cut never splits
    a product noun ("phone | case") and never leaves half a phrase ("lover gift").
    """
    tokens = phrase.tokens
    inside = {i for start, end, *_ in phrase.nouns for i in range(start + 1, end)}
    pieces = []
    for size in (3, 2):
        start = len(tokens) - size
        if start <= 0 or start in inside:
            continue
        piece = _Phrase(tokens[start:], phrase.count)
        if not piece.broken and piece.meaningful() and len(piece.text) <= MAX_TAG_LEN:
            pieces.append(piece.text)
    return pieces


@dataclass
class _Cand:
    """A tag that may be one of the thirteen: its text, how much it is worth before the
    market says anything (`prior`), its share of the sampled listings, and whether it is
    one of the shop-wide tags every draft would carry."""

    text: str
    prior: float
    share: float = 0.0
    generic: bool = False


def _shares(report: MarketReport | None) -> dict[str, float]:
    """tag_key -> the share of the sampled listings that use it, as a tag or in a title."""
    out: dict[str, float] = {}
    if not _has_market(report):
        return out
    assert report is not None
    for rows in (report.tags, report.phrases):
        for text, count in _rows(rows):
            key = tag_key(text)
            out[key] = max(out.get(key, 0.0), _share(count, report))
    return out


def _near(a: str, b: str) -> bool:
    """The audit's near-duplicate rule (seo._near_duplicates) for two tags, or one tag."""
    return tag_key(a) == tag_key(b) or bool(_seo_near_duplicates([tag_key(a), tag_key(b)]))


def _clash(tag: str, existing: Sequence[str]) -> bool:
    """Whether `tag` would burn a second slot on a search `existing` already holds."""
    return any(_near(tag, other) for other in existing) or _too_similar(tag, list(existing))


def _core_words(seed: Seed, product: _Product) -> list[str]:
    """The concept's words, without the product noun it already names."""
    tokens = _tokens(seed.text)
    skip: set[int] = set()
    if product.in_concept and product.family:
        skip = {i for start, end, family, _d in _nouns(tokens) if family == product.family
                for i in range(start, end)}
    return [t for i, t in enumerate(tokens) if i not in skip]


def _is_design_word(word: str) -> bool:
    return vocab.is_design_word(word) and word not in vocab.COLOURS


def _windows(core: Sequence[str]) -> list[tuple[str, float]]:
    """Runs of up to three of the concept's words that name something, each with how much
    of what the design is about it holds (1.0 holds all of it).

    "mediterranean lemon" gives "lemon" and "mediterranean lemon" (a colour or a room
    alone, "pastel" or "nursery", is not something a design is about). A run that ends on
    the concept's last word, which in English is what the design is ("air balloon", not
    "hot air"), counts more: that is where the product noun goes.
    """
    motifs = [w for w in core if _is_design_word(w)]
    found: dict[str, float] = {}
    for size in range(min(3, len(core)), 0, -1):
        for start in range(len(core) - size + 1):
            run = core[start:start + size]
            if run[0] in STOPWORDS or run[-1] in STOPWORDS:
                continue
            held = [w for w in run if _is_design_word(w)]
            if not held:
                continue
            cover = len(held) / max(1, len(motifs))
            bonus = 0.15 if start + size == len(core) else 0.0
            found.setdefault(" ".join(run), cover + bonus + 0.01 * size)
    return sorted(found.items(), key=lambda kv: (-kv[1], kv[0]))


_ROOM_NAMES = {"kitchen", "bedroom", "nursery", "bathroom", "office", "playroom", "entryway",
               "hallway", "pantry", "closet", "laundry", "mudroom", "nook", "dorm", "sunroom",
               "basement", "porch"}


def _market_rooms(report: MarketReport | None, taken: set[str]) -> list[str]:
    """The rooms the market's tags and titles name, most used first."""
    counts: dict[str, int] = {}
    if _has_market(report):
        assert report is not None
        for rows in (report.tags, report.phrases):
            for text, count in _rows(rows):
                for token in _tokens(text):
                    key = _key(token)
                    if key in _ROOM_NAMES and key not in taken:
                        counts[key] = max(counts.get(key, 0), count)
    return [room for room, _n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def _tag_candidates(seed: Seed, report: MarketReport | None, plan: _Plan,
                    hint: Hint) -> list[_Cand]:
    """Every tag worth considering for this design, each with a prior.

    In order of worth: the concept itself; the design's own phrases with the product
    ("lemon wallpaper", "lemon mural", "dog dad shirt") and with a room the market names
    ("lemon kitchen"); the market's own tags that carry a word of the design; its other
    phrases; the concept's own word pairs and single words, last. A tag with no word of
    the design (the template's own tags, and the market's most used) is `generic`.
    """
    product = plan.product
    shares = _shares(report)
    core = _core_words(seed, product)
    design_keys = {_key(w) for w in core if _is_design_word(w)} or {_key(w) for w in core}
    noun = _TAG_NOUNS.get(product.display, product.display.lower()) if product.family else ""
    forms = ([noun, *_TAG_FORMS.get(product.family or "", ())]) if noun else []
    seller_title = hint.title if isinstance(hint, SellerText) else ""
    template = product_tags(product.template_tags, seller_title, seed, hint=hint)
    template_keys = {tag_key(t) for t in product.template_tags}
    sized = sorted(((tag_key(t), c) for t, c in _rows(report.tags)),
                   key=lambda kv: (-kv[1], kv[0])) if _has_market(report) else []
    market_generic = {k for k, count in sized[:GENERIC_TOP]
                      if _share(count, report) >= GENERIC_SHARE}

    def shape(tag: str) -> bool:
        """Made of format, material, product and marketplace words only."""
        return all(w in _SHAPE_WORDS or vocab.stem(w) in _SHAPE_WORDS
                   for w in (_key(t) for t in _tokens(tag)))

    def generic(tag: str) -> bool:
        if {_key(t) for t in _tokens(tag)} & design_keys:
            return False
        key = tag_key(tag)
        return key in template_keys or key in market_generic or shape(tag)

    found: dict[str, _Cand] = {}

    def offer(raw: str, prior: float, *, as_generic: bool | None = None) -> None:
        tag = clean_tag(raw)
        if not tag:
            return
        key = tag_key(tag)
        share = shares.get(key, 0.0)
        wide = generic(tag) if as_generic is None else as_generic
        if wide and len(_tokens(tag)) < 2:
            # "wallpaper", "gift": one word of the shop's is no shop-wide tag worth a slot,
            # only a last resort.
            wide, prior = False, min(prior, 0.04)
        cand = _Cand(tag, prior, share, wide)
        known = found.get(key)
        if known is None or cand.prior > known.prior:
            found[key] = cand

    offer(seed.text, 1.0, as_generic=False)
    windows = _windows(core)
    for window, cover in windows:
        for index, form in enumerate(forms):
            offer(f"{window} {form}", 0.6 + 0.25 * min(cover, 1.0) - 0.05 * index,
                  as_generic=False)
    for position, (window, _cover) in enumerate(windows[:2]):
        for room in _market_rooms(report, set(core)):
            offer(f"{window} {room}", 0.56 - 0.02 * position, as_generic=False)
            if noun:
                offer(f"{window} {room} {noun}", 0.54 - 0.02 * position, as_generic=False)

    for phrase in sorted(plan.phrases, key=lambda p: p.text):
        rooted = bool(set(phrase.keys) & design_keys)
        prior = 0.85 if rooted else 0.40
        prior += 0.05 * phrase.tagged + 0.10 * any(phrase is c for c in plan.chosen)
        if len(phrase.text) <= MAX_TAG_LEN:
            offer(phrase.text, prior)
        elif not generic(phrase.text):
            for piece in _tag_pieces(phrase):
                offer(piece, 0.45)

    for tag in template:
        # The template's tag about its own look or room ("coastal wallpaper", "cottage
        # kitchen") is the template's, not the shop's: only the format and material tags
        # that every draft of the shop may carry are kept.
        if shape(tag):
            offer(tag, 0.30, as_generic=True)
    # The concept's own word pairs and words, and the product alone: what is left when
    # nothing better is.
    for index in range(len(core) - 1):
        offer(f"{core[index]} {core[index + 1]}", 0.25, as_generic=False)
    for word in core:
        if word not in STOPWORDS:
            offer(word, 0.08, as_generic=False)
    if noun:
        offer(noun, 0.02, as_generic=False)
    return list(found.values())


def build_tags(seed: Seed, report: MarketReport | None = None, *,
               product_hint: Hint = None, taken: Sequence[Sequence[str]] = ()) -> list[str]:
    """Thirteen tags at most, each within Etsy's length and character rules.

    Mostly about this design, a little about the shop. The design's own phrases lead:
    the concept, the concept with the product ("lemon wallpaper"), with a word that is a
    search of its own ("lemon mural"), with a room the market names, and the market's own
    tags that carry a word of the design, ranked by the share of the sampled listings that
    use them. At most GENERIC_SLOTS tags are the shop-wide kind (the template's own tags,
    and the market's most used ones that no design word is in): "removable wallpaper"
    on every draft is what made a shop's listings compete with each other.

    `taken` is the tags of the drafts made before this one (this run's, and the upload
    history's): a tag one of them already holds is worth less, and no tag is taken that
    would put MAX_SHARED_TAGS + 1 tags in common with any one of them. Two tags that are
    one search (`seo._near_duplicates`, "gift"/"gifts") never both go in. The claims the
    template does not make for the whole listing ("removable" on a template that also
    sells paste-up paper) are never tags. Nothing here depends on the order of the
    market's rows or on anything but the arguments.
    """
    plan = _plan(seed, report, product_hint)
    taken_sets = [{tag_key(t) for t in tags if str(t).strip()} for tags in taken]
    used: dict[str, int] = {}
    for tags in taken_sets:
        for key in tags:
            used[key] = used.get(key, 0) + 1
    shared = [0] * len(taken_sets)
    candidates = _tag_candidates(seed, report, plan, product_hint)
    template_rank = {tag_key(t): i for i, t in enumerate(plan.product.template_tags)}

    def score(cand: _Cand) -> float:
        value = cand.prior + 0.6 * min(cand.share, 0.5)
        if not cand.generic:
            value -= 0.35 * min(used.get(tag_key(cand.text), 0), 2)
        return value

    specific = sorted((c for c in candidates if not c.generic), key=lambda c: (-score(c), c.text))
    shop_wide = sorted(
        (c for c in candidates if c.generic),
        key=lambda c: (tag_key(c.text) not in template_rank,
                       template_rank.get(tag_key(c.text), 0), -c.share, c.text))
    own: list[str] = []  # about the design
    wide: list[str] = []  # shop-wide

    def room(tag: str, *, strict: bool) -> bool:
        if len(own) + len(wide) >= MAX_TAGS or _clash(tag, [*own, *wide]):
            return False
        key = tag_key(tag)
        return not (strict and any(key in held and count >= MAX_SHARED_TAGS
                                   for held, count in zip(taken_sets, shared)))

    def keep(tag: str, into: list[str]) -> None:
        into.append(tag)
        key = tag_key(tag)
        for index, held in enumerate(taken_sets):
            if key in held:
                shared[index] += 1

    for cand in shop_wide:
        if len(wide) < GENERIC_SLOTS and room(cand.text, strict=True):
            keep(cand.text, wide)
    for strict in (True, False):
        for cand in specific:
            if len(own) + len(wide) < MAX_TAGS and cand.text not in own and room(
                    cand.text, strict=strict):
                keep(cand.text, own)
    # Nothing about the design is left to say: a shop-wide tag is still worth more than an
    # empty slot.
    for cand in shop_wide:
        if cand.text not in wide and room(cand.text, strict=False):
            keep(cand.text, wide)
    return [*own, *wide][:MAX_TAGS]


def fill_tags(tags: list[str], extra: Sequence[str]) -> int:
    """Add `extra` tags to the free slots, skipping near-duplicates; how many were added."""
    added = 0
    for raw in extra:
        if len(tags) >= MAX_TAGS:
            break
        tag = clean_tag(raw)
        if tag and not _clash(tag, tags):
            tags.append(tag)
            added += 1
    return added


def suggest_additions(
    report: MarketReport | None, *, existing: Sequence[str] = (), text: Hint = None,
    title: str = "", shop: Sequence[Sequence[str]] = (), wide: Sequence[str] = (),
) -> list[tuple[str, int]]:
    """The market's tags worth adding to a listing that is already live, best first.

    The same rules a draft's tags follow, for a seller's existing listing (the SEO
    page's "Düzelt" and `etsyotomasyon seo suggest`):

    - a tag the listing already has, or that is one search with one it has
      (`seo._near_duplicates`), is never offered;
    - nor is an adhesive claim the listing does not make for the whole of itself (`text`:
      its own title, tags, materials and description): "removable" on a listing that sells
      paste-up paper too, or that never says it is removable;
    - at most GENERIC_SLOTS of the listing's tags may be the shop's generic ones (`wide`:
      the tags on at least half of the shop's listings; and the market's most used ones
      that hold no word of the listing's `title`), so a listing that has that many is
      offered none;
    - `shop` is the other listings' tags: a tag they use ranks lower, and none is offered
      that would put SHARED_TAGS_MIN of this listing's tags on one of them.

    Rows are (tag, listings using it), most used first.
    """
    if not _has_market(report):
        return []
    assert report is not None
    have = [t for t in (clean_tag(t) for t in existing) if t]
    have_keys = {tag_key(t) for t in have}
    allowed = _allowed_adhesive(text)
    design = {_key(w) for w in vocab.design_words(_tokens(title))}
    wide_keys = {tag_key(t) for t in wide}
    rows = sorted(_rows(report.tags), key=lambda row: (-row[1], tag_key(row[0])))
    # With no design word in the title, the market's most used tags cannot be told apart
    # from the design's own: only the shop's own (`wide`) and the plainly generic count.
    market_generic = {tag_key(t) for t, count in rows[:GENERIC_TOP]
                      if design and _share(count, report) >= GENERIC_SHARE}

    def generic(tag: str) -> bool:
        words = [_key(t) for t in _tokens(tag)]
        key = tag_key(tag)
        if design & set(words) or (len(words) < 2 and key not in wide_keys):
            return False  # about the design; or one word, which is never a shop-wide tag
        return (key in wide_keys or key in market_generic
                or all(w in _SHAPE_WORDS or vocab.stem(w) in _SHAPE_WORDS for w in words))

    others = [{tag_key(t) for t in tags if str(t).strip()} for tags in shop]
    used: dict[str, int] = {}
    for tags in others:
        for key in tags:
            used[key] = used.get(key, 0) + 1
    shared = [len(have_keys & tags) for tags in others]

    ranked: list[tuple[float, str, int]] = []
    for raw, count in rows:
        tag = clean_tag(raw)
        if not tag or tag_key(tag) in have_keys or _clash(tag, have):
            continue
        if vocab.adhesive_claims(_tokens(tag)) - allowed:
            continue
        ranked.append((-count * 0.6 ** used.get(tag_key(tag), 0), tag, count))
    room = max(0, GENERIC_SLOTS - sum(1 for t in have if generic(t)))

    out: list[tuple[str, int]] = []
    for _weight, tag, count in sorted(ranked):
        key = tag_key(tag)
        if _clash(tag, [*have, *(t for t, _c in out)]):
            continue
        if any(key in tags and shared[i] + 1 >= SHARED_TAGS_MIN
               for i, tags in enumerate(others)):
            continue
        if generic(tag):
            if room <= 0:
                continue
            room -= 1
        for i, tags in enumerate(others):
            if key in tags:
                shared[i] += 1
        out.append((tag, count))
    return out


def build_description(seed: Seed, template_description: str, title: str, *,
                      source_title: str = "", description_template: str | None = None) -> str:
    """The seller's own description for this draft, never one written here.

    A description is prose. It cannot be measured out of n-grams, and inventing one
    would be the tool writing marketing copy it has no basis for. So the seller's own
    wording carries over: their saved description template ({başlık} and {tasarım}
    filled in), else the template listing's description with its own title
    (`source_title`) replaced by this draft's and that title as the opening line.
    See drop.description.
    """
    from . import description

    return description.build(seed, title=title, description=template_description,
                             source_title=source_title, template_text=description_template)


def generate(
    seed: Seed,
    report: MarketReport | None = None,
    *,
    template_description: str = "",
    fallback_tags: list[str] | None = None,
    template_title: str = "",
    description_template: str | None = None,
    product_words: Sequence[str] = (),
    template_materials: Sequence[str] = (),
    taken_tags: Sequence[Sequence[str]] = (),
) -> Generated:
    """Produce the copy for one product, and say honestly how much evidence backed it.

    The template's title, tags, materials and description tell the builders what the
    product is and which claims about it (size, material, brand, how it goes on) are the
    seller's own. With no `description_template` saved, a description that still holds
    sentences about the template's own design says so in the warnings (`product_words`:
    the template's category names, see description.design_words). `taken_tags` are the
    tags of the drafts made before this one (see `build_tags`).
    """
    warnings: list[str] = []
    sources: list[str] = []

    if not seed:
        # Nothing usable came out of the filename. Refusing beats inventing a
        # confident title and putting the wrong listing in someone's shop.
        return Generated(
            title="",
            tags=[],
            warnings=[seed.reason or "no product concept could be derived from the filename"],
        )

    hint = hint_from(template_title, fallback_tags, template_description, template_materials)
    # What was searched: the concept, with the product when it does not say it.
    searched = str(getattr(report, "keyword", "") or "") or research_keyword(seed, hint)
    if report and not report.empty:
        sources.append(f"{report.sampled} listings ranking for {searched!r}")
        if report.sampled < 20:
            warnings.append(
                f"only {report.sampled} listing(s) rank for {searched!r} — too thin a "
                "sample to draw tags from, so this is mostly your own words"
            )
    else:
        warnings.append(
            f"no market data for {searched!r}; tags come from the filename and your "
            "template only"
        )

    title = build_title(seed, report, product_hint=hint)
    tags = build_tags(seed, report, product_hint=hint, taken=taken_tags)

    # A few of the template's own tags that suit any design of its product (never the
    # ones about its own design, product_tags) are in the tags already; free slots take
    # more of them.
    template_keys = {tag_key(t) for t in fallback_tags or []}
    fill_tags(tags, product_tags(fallback_tags, template_title, seed, hint=hint))
    if any(tag_key(t) in template_keys for t in tags):
        sources.append("your template listing's tags")

    if len(tags) < MAX_TAGS:
        warnings.append(f"{len(tags)}/{MAX_TAGS} tags — the rest could not be filled honestly")

    has_template = bool(description_template and description_template.strip())
    if not has_template:
        from . import description

        left = description.leftover(template_description, template_title, fallback_tags,
                                    seed=seed, title=title, product_words=product_words)
        if left:
            warnings.append(description.leftover_message(left))

    return Generated(
        title=title,
        tags=tags,
        description=build_description(seed, template_description, title,
                                      source_title=template_title,
                                      description_template=description_template),
        sources=sources,
        warnings=warnings,
    )
