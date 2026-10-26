"""Words the SEO audit and the title/tag generator have to agree on.

Both ask the same question from two sides: *which words of a title or a tag are about
this one design, and which are the shop's, the product's or the marketplace's?* The
generator spends its slots on the first kind and never lets a claim about the product
("removable", "self adhesive") into a listing that does not make it for the whole
listing; the audit flags the listings whose opening is all the second kind. Keeping the
word lists here, in one place, is what makes those two agree.

Nothing in here measures anything. These are plain lists, and a word that is missing
from them is treated as a design word, which is the safe side for both users.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

# --- products ---------------------------------------------------------------------------

# Product nouns, as tokens -> (family, how a title spells it). The family keeps a mug's
# title from borrowing "Poster" from a concept search that also returned posters.
PRODUCTS: dict[tuple[str, ...], tuple[str, str]] = {
    ("tshirt",): ("shirt", "T-Shirt"),
    ("shirt",): ("shirt", "Shirt"),
    ("tee",): ("shirt", "Tee"),
    ("tank", "top"): ("tank", "Tank Top"),
    ("tank",): ("tank", "Tank"),
    ("sweatshirt",): ("sweatshirt", "Sweatshirt"),
    ("crewneck",): ("sweatshirt", "Crewneck"),
    ("sweater",): ("sweatshirt", "Sweater"),
    ("pullover",): ("sweatshirt", "Pullover"),
    ("hoodie",): ("hoodie", "Hoodie"),
    ("onesie",): ("baby", "Onesie"),
    ("bodysuit",): ("baby", "Bodysuit"),
    ("mug",): ("mug", "Mug"),
    ("cup",): ("mug", "Cup"),
    ("tumbler",): ("tumbler", "Tumbler"),
    ("water", "bottle"): ("bottle", "Water Bottle"),
    ("poster",): ("wallart", "Poster"),
    ("print",): ("wallart", "Print"),
    ("wall", "art"): ("wallart", "Wall Art"),
    ("art", "print"): ("wallart", "Art Print"),
    ("wall", "decor"): ("wallart", "Wall Decor"),
    ("canvas", "print"): ("wallart", "Canvas Print"),
    ("art",): ("wallart", "Art"),
    ("wallpaper",): ("wallpaper", "Wallpaper"),
    ("wall", "mural"): ("wallpaper", "Wall Mural"),
    ("mural",): ("wallpaper", "Mural"),
    ("home", "decor"): ("decor", "Home Decor"),
    ("decor",): ("decor", "Decor"),
    ("sticker",): ("sticker", "Sticker"),
    ("decal",): ("sticker", "Decal"),
    ("tote", "bag"): ("tote", "Tote Bag"),
    ("tote",): ("tote", "Tote"),
    ("bag",): ("tote", "Bag"),
    ("phone", "case"): ("phonecase", "Phone Case"),
    ("iphone", "case"): ("phonecase", "iPhone Case"),
    ("pillow", "case"): ("pillow", "Pillow Case"),
    ("pencil", "case"): ("pencilcase", "Pencil Case"),
    ("case",): ("phonecase", "Case"),
    ("pillow",): ("pillow", "Pillow"),
    ("cushion",): ("pillow", "Cushion"),
    ("blanket",): ("blanket", "Blanket"),
    ("ornament",): ("ornament", "Ornament"),
    ("magnet",): ("magnet", "Magnet"),
    ("keychain",): ("keychain", "Keychain"),
    ("hat",): ("hat", "Hat"),
    ("cap",): ("hat", "Cap"),
    ("beanie",): ("hat", "Beanie"),
    ("sock",): ("socks", "Socks"),
    ("apron",): ("apron", "Apron"),
    ("coaster",): ("coaster", "Coaster"),
    ("notebook",): ("notebook", "Notebook"),
    ("journal",): ("notebook", "Journal"),
    ("card",): ("card", "Card"),
    ("bookmark",): ("bookmark", "Bookmark"),
    ("puzzle",): ("puzzle", "Puzzle"),
    ("towel",): ("towel", "Towel"),
    ("flag",): ("flag", "Flag"),
    ("garden", "stake"): ("stake", "Garden Stake"),
    ("necklace",): ("jewelry", "Necklace"),
    ("earring",): ("jewelry", "Earrings"),
    ("bracelet",): ("jewelry", "Bracelet"),
    ("svg",): ("digital", "SVG"),
    ("png",): ("digital", "PNG"),
    ("clipart",): ("digital", "Clipart"),
    ("digital", "download"): ("digital", "Digital Download"),
}
PRODUCT_WORDS = {word for key in PRODUCTS for word in key}

# --- words that are not about one design ------------------------------------------------

# Words that carry no search of their own.
GENERIC = {
    "gift", "gifts", "present", "presents", "idea", "ideas", "best", "perfect", "unique",
    "new", "sale", "awesome", "great", "clothing", "apparel", "clothes", "outfit", "item",
    "items", "design", "designs", "top", "quality", "premium", "shop", "store", "style",
    "stuff",
}
# Real searches, but marketplace-wide ones.
WEAK = {
    "graphic", "cute", "funny", "trendy", "aesthetic", "birthday", "vintage", "retro",
    "cool", "modern", "classic", "simple", "basic", "stocking", "stuffer", "christmas",
    "holiday", "summer", "everyday", "casual", "soft", "comfy",
}
AUDIENCE_WOMEN = {"her", "women", "womens", "woman", "ladies", "lady"}
AUDIENCE_MEN = {"him", "men", "mens", "man", "guys", "guy"}
AUDIENCE = AUDIENCE_WOMEN | AUDIENCE_MEN | {
    "unisex", "kids", "kid", "boys", "girls", "boy", "girl", "toddler", "toddlers", "youth",
    "adult", "adults", "teens", "teen",
}
# The room a product goes in: a buyer's search, but one every design in the shop shares.
ROOMS = {
    "kitchen", "bedroom", "nursery", "bathroom", "bath", "dining", "living", "livingroom",
    "office", "playroom", "entryway", "hallway", "foyer", "pantry", "closet", "laundry",
    "mudroom", "nook", "den", "study", "powder", "dorm", "sunroom", "basement", "garage",
    "porch", "patio", "loft", "lounge", "classroom", "homeschool", "room", "rooms",
    "bedrooms", "kitchens", "nurseries", "baby", "children", "childrens",
}
# A look, rather than a motif: what a title's "style or room" phrase is made of.
STYLES = {
    "boho", "bohemian", "farmhouse", "cottagecore", "cottage", "scandinavian", "scandi",
    "nordic", "modern", "minimalist", "minimal", "rustic", "vintage", "retro", "coastal",
    "midcentury", "japandi", "deco", "maximalist", "whimsical", "gothic", "industrial",
    "shabby", "chic", "country", "contemporary", "classic", "elegant", "luxury", "moody",
    "neutral", "earthy", "hygge", "mediterranean", "victorian", "chinoiserie", "zen",
    "glam", "eclectic", "academia", "hamptons", "transitional",
}
# What the product is made of or how it goes on: a claim about the item, not a design.
ADHESIVE_WORDS = {
    "peel", "peelable", "stick", "sticky", "adhesive", "selfadhesive", "removable",
    "repositionable", "temporary", "renter", "renters", "friendly", "traditional", "paste",
    "pasteable", "pasted", "prepasted", "unpasted", "nonadhesive", "vinyl", "paper", "roll",
    "rolls", "sample", "samples", "self", "washable", "waterproof", "custom", "personalized",
    "personalised", "handmade", "size", "sizes", "sized",
}
# Padding around a product: "Wall Decor", "Home Decor", "Wall Art".
FILLER = {
    "wall", "walls", "decor", "decoration", "decorations", "decorative", "home", "house",
    "accent", "interior", "space", "pattern", "patterns", "art", "artwork", "mural", "murals",
    "wallpaper", "wallpapers", "backdrop", "covering", "treatment",
}
# Joining words that never make a phrase about one design.
MINOR = {"a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on", "or", "the",
         "to", "with", "vs"}
# A few Turkish equivalents: a Turkish seller's "Hediye" is no more a design than "Gift".
_TURKISH_GENERIC = {
    "hediye", "hediyelik", "hediyeler", "tisort", "tişört", "kupa", "bardak", "tablo",
    "duvar", "kagidi", "kağıdı", "dekor", "dekorasyon", "ev", "oda", "mutfak", "yatak",
    "cocuk", "çocuk", "bebek", "kadin", "kadın", "erkek", "en", "iyi", "ozel", "özel",
    "ucuz", "yeni", "kalite", "kaliteli",
}

# Not about one design, whichever of the lists above a word is on.
NON_DESIGN = (GENERIC | WEAK | AUDIENCE | ROOMS | ADHESIVE_WORDS | FILLER | MINOR
              | PRODUCT_WORDS | _TURKISH_GENERIC)

# A colour or a plain adjective in front of the motif: "sage", "pastel", "dark".
COLOURS = {
    "pastel", "navy", "sage", "cream", "blue", "green", "pink", "red", "black", "white",
    "grey", "gray", "beige", "brown", "yellow", "orange", "purple", "lavender", "teal",
    "mint", "blush", "coral", "gold", "golden", "silver", "terracotta", "olive", "emerald",
    "burgundy", "maroon", "charcoal", "ivory", "tan", "rust", "mustard", "peach", "lilac",
    "turquoise", "aqua", "dark", "light", "bright", "muted", "colorful", "colourful",
    "multicolor", "monochrome", "neutral",
}


def stem(word: str) -> str:
    """A word without its plural "s", for looking it up in the lists above."""
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def is_design_word(word: str) -> bool:
    """Whether a lower-case word of a title says what this design is about.

    A word on none of the lists is a design word ("lemon", "moth", "dala", "paw"): a
    list that is too short only makes the audit a little more forgiving.
    """
    word = word.lower()
    if len(word) < 3 or word.isdigit() or not any(ch.isalpha() for ch in word):
        return False
    return word not in NON_DESIGN and stem(word) not in NON_DESIGN


def design_words(words: Iterable[str]) -> list[str]:
    """The design words among these (lower-case) words, in order."""
    return [w for w in words if is_design_word(w)]


# --- what a template claims about the product --------------------------------------------

# The claims that are true of a peel-and-stick product only.
PEEL_AND_STICK = "peel and stick"
ADHESIVE = "adhesive"
REMOVABLE = "removable"
TEMPORARY = "temporary"
RENTER = "renter friendly"
_ADHESIVE_TOKENS = {
    "adhesive": ADHESIVE, "selfadhesive": ADHESIVE, "removable": REMOVABLE,
    "repositionable": REMOVABLE, "temporary": TEMPORARY, "renter": RENTER, "renters": RENTER,
}


def adhesive_claims(tokens: Sequence[str]) -> set[str]:
    """The adhesive claims in a run of lower-case words, by canonical name.

    "peel and stick" / "peel & stick" / "peel stick" -> "peel and stick"; "self
    adhesive" -> "adhesive"; "removable" and "repositionable" -> "removable";
    "temporary"; "renter friendly" / "renters". A lone "peel" or "stick" is not a claim
    ("lemon peel", "stick figure").
    """
    words = [t.lower() for t in tokens]
    found: set[str] = set()
    for index, word in enumerate(words):
        if word in ("peel", "peelable") and "stick" in words[index + 1:index + 3]:
            found.add(PEEL_AND_STICK)
        elif word in _ADHESIVE_TOKENS:
            found.add(_ADHESIVE_TOKENS[word])
    return found


# Words of a title, a tag or a material that say the product is pasted on.
_PASTE_WORDS = {"traditional", "paste", "pasteable", "pasted", "prepasted", "unpasted",
                "nonadhesive"}
# A description is prose: "traditional" there is as likely a style ("traditional floral"),
# so it counts only when it names a kind of paper.
_PASTE_PROSE = re.compile(
    r"\b(?:non[- ]?adhesive|unpasted|pre[- ]?pasted|pasteable|paste[- ]up|wheat[- ]paste"
    r"|(?:wall)?paper paste|paste\s+(?:the|to the)\s+(?:wall|paper|wallpaper)"
    r"|(?:needs?|requires?|use|using|with|apply)\s+(?:the\s+)?(?:wall(?:paper)?\s+)?paste"
    r"|paste\s+(?:is\s+)?(?:required|needed|not included|sold separately)"
    r"|traditional\s+(?:wall\s*paper|paper|paste|option|material|vinyl|non[- ]?woven|"
    r"installation|application|wallpaper))\b",
    re.IGNORECASE,
)


def mentions_paste(texts: Iterable[str], prose: Iterable[str] = ()) -> bool:
    """Whether a listing's own words say some of it is pasted, not peeled and stuck.

    `texts` are its title, tags and materials (any of the words in _PASTE_WORDS counts);
    `prose` its description (a phrase that names the paper, see _PASTE_PROSE). A
    template that says both "removable" and "traditional" sells both, so a claim that is
    only true of the first cannot be written for the whole listing.
    """
    word = re.compile(r"[^\W\d_]+", re.UNICODE)
    for text in texts:
        found = [w.lower() for w in word.findall(str(text or ""))]
        if _PASTE_WORDS & set(found):
            return True
        if any(a == "non" and b == "adhesive" for a, b in zip(found, found[1:])):
            return True
        if any(a == "pre" and b == "pasted" for a, b in zip(found, found[1:])):
            return True
    return any(_PASTE_PROSE.search(str(text or "")) for text in prose)
