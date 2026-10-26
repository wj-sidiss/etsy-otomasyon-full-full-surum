"""Seeds and generated copy: junk file names refused, titles that read like Etsy titles.

Every market here is invented: listings are built from phrase pools and run through the
real `seo.research`, so the phrases and tags have exactly the shape of a live sample:
phrases of one to four words taken within the comma-separated parts of each title, the
most used first.
"""

from __future__ import annotations

import random
import unicodedata
from pathlib import Path

import pytest

from etsyotomasyon.config import MAX_TAG_LEN, MAX_TAGS, MAX_TITLE_LEN
from etsyotomasyon.drop import generate, seeds
from etsyotomasyon.drop.seeds import Seed
from etsyotomasyon.listings import validate_tags
from etsyotomasyon.seo import MarketReport, research

# --- seeds: default names from apps and phones, in Turkish and English --------------------


@pytest.mark.parametrize(
    "filename",
    [
        # Canva, Turkish and English, with and without the copy counter.
        "Adsız tasarım.png", "Adsız Tasarım (3).png", "ADSIZ TASARIM (12).png",
        "Untitled design.png", "Untitled design (4).png", "Copy of Untitled design.png",
        "Design.png", "Tasarım.png", "Tasarım1.png", "Canva Design DAFx7Kq2Lm8.png",
        # Photoshop, Illustrator, Procreate, Figma.
        "Untitled-1.png", "Untitled-1 copy.png", "Adsız-1.psd", "Untitled Artwork 3.png",
        "Adsız Çizim.png", "Çalışma Yüzeyi 1.png", "Artboard 1 copy.png", "Frame 12.png",
        "Layer 1.png", "New Project (3).png", "Yeni Proje.png",
        # Phones, screenshots and messaging apps.
        "IMG_1234.png", "IMG_20260901_101010.jpg", "PXL_20260901_101010123.jpg",
        "Screenshot 2026-09-01 at 10.10.10.png", "Screen Shot 2026-09-01 at 10.10.10 AM.png",
        "Screenshot (12).png", "Screenshot_20260901-101010_Instagram.jpg",
        "Ekran Resmi 2026-09-01 10.10.10.png", "Ekran görüntüsü (12).png",
        "Ekran Görüntüsü 2026-09-01 101010.png", "Ekran Alıntısı.png",
        "WhatsApp Image 2026-09-01 at 10.10.10.jpeg",
        "WhatsApp Görsel 2026-09-01 saat 10.10.10_ab12cd34.jpg",
        "photo_2026-09-01_10-10-10.jpg",
        # Browsers and downloads.
        "image.png", "image (1).png", "images.jpg", "indir.png", "indir (2).png",
        "download (3).png", "unnamed (1).png", "Resim1.png", "Görsel 3.png",
        # Hashes and generated ids.
        "a3f9c2e1b7d4.png", "8f14e45fceea167a5a36dedd4bea2543.png",
        "1f0e3dad-9990-4a5c-8b1a-2c3d4e5f6a7b.png",
    ],
)
def test_a_default_or_device_name_is_junk(filename):
    seed = seeds.derive(Path(filename), folder_fallback=False)
    assert not seed, f"{filename!r} became the concept {seed.text!r}"
    assert seed.reason


def test_a_mac_decomposed_turkish_name_is_still_recognised():
    # macOS hands file names over decomposed (NFD): "ş" is "s" plus a combining cedilla.
    name = unicodedata.normalize("NFD", "Adsız Tasarım (3).png")
    assert not seeds.derive(Path(name), folder_fallback=False)
    good = seeds.derive(Path(unicodedata.normalize("NFD", "çiçek-düğme.png")))
    assert good.text == "çiçek düğme"


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        # Copy marks from browsers, Windows, macOS (EN and TR) and Google Drive.
        ("Retro Sunset (2).png", "retro sunset"),
        ("lucky-clover (1).png", "lucky clover"),
        ("Retro Sunset - Copy.png", "retro sunset"),
        ("Retro Sunset - Copy (2).png", "retro sunset"),
        ("Retro Sunset - Kopya (2).png", "retro sunset"),
        ("Retro Sunset copy 2.png", "retro sunset"),
        ("retro_sunset_copy.png", "retro sunset"),
        ("Retro Sunset kopyası.png", "retro sunset"),
        ("Retro Sunset kopyası 2.png", "retro sunset"),
        ("Copy of Retro Sunset.png", "retro sunset"),
        ("Copy (2) of Retro Sunset.png", "retro sunset"),
        # Dates, pixel sizes and tool suffixes describe the file, not the product.
        ("Retro Sunset 2026-09-01.png", "retro sunset"),
        ("Retro Sunset 20260901.png", "retro sunset"),
        ("national-park-pine-4500x5400.png", "national park pine"),
        ("retro-sunset-removebg-preview.png", "retro sunset"),
        ("Mountain Sunset Final v3 (1).png", "mountain sunset"),
        ("Retro Sunset T-Shirt Design (1).png", "retro sunset tshirt"),
        # What stays: a year that is the theme, a print size, numbers in the name.
        ("class-of-2026.png", "class of 2026"),
        ("8x10-botanical.png", "8x10 botanical"),
        ("route-66-poster.png", "route 66 poster"),
        ("new-york-skyline.png", "new york skyline"),
        ("İstanbul Skyline.png", "istanbul skyline"),
    ],
)
def test_copy_marks_dates_and_sizes_come_off_a_good_name(filename, expected):
    assert seeds.derive(Path(filename), folder_fallback=False).text == expected


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        # "print", "son", "design" and "fix" are part of the design when the word before
        # makes a phrase with them: a paw print is not a print file.
        ("dog-dad-paw-print.png", "dog dad paw print"),
        ("Dog Dad Paw Print Final.png", "dog dad paw print"),
        ("dog_dad_paw_print_v2.png", "dog dad paw print"),
        ("dog-dad-paw-print-print-ready.png", "dog dad paw print"),
        ("dog-dad-paw-print-ready.png", "dog dad paw print"),
        ("Paw Print (2).png", "paw print"),
        ("leopard-print-mama-copy.png", "leopard print mama"),
        ("Cheetah Print Lightning Bolt - Copy.png", "cheetah print lightning bolt"),
        ("cow-print.png", "cow print"),
        ("baby-hand-print-final-final.png", "baby hand print"),
        ("like-father-like-son.png", "like father like son"),
        ("Mother and Son Final.png", "mother and son"),
        ("interior-design-edit.png", "interior design"),
        ("caffeine-fix.png", "caffeine fix"),
        # ...while real revision markers still come off, alone or stacked.
        ("retro-sunset-print.png", "retro sunset"),
        ("boeing-747-print.png", "boeing 747"),
        ("retro-sunset-print-ready.png", "retro sunset"),
        ("retro sunset ready to print.png", "retro sunset"),
        ("retro_sunset_printready.png", "retro sunset"),
        ("retro-sunset-rev2.png", "retro sunset"),
        ("Retro Sunset Final v3 high-res.png", "retro sunset"),
        ("retro-sunset-fix.png", "retro sunset"),
        ("retro-sunset-son.png", "retro sunset"),
        ("retro-sunset-backup.png", "retro sunset"),
        # Turkish: "son" and "son hali" mean final, "baskıya hazır" print-ready.
        ("Kedi Pati İzi Son.png", "kedi pati izi"),
        ("Kedi Pati İzi son hali.png", "kedi pati izi"),
        ("Retro Güneş Baskıya Hazır.png", "retro güneş"),
        ("Retro Güneş baskı için.png", "retro güneş"),
        ("Retro Güneş - Kopya revize.png", "retro güneş"),
        ("Leopar Desenli Kupa Final.png", "leopar desenli kupa"),
        ("Anne Oğul Son.png", "anne oğul"),
        ("Yeni Yıl Hediyesi yedek.png", "yeni yıl hediyesi"),
    ],
)
def test_only_revision_markers_come_off_the_end_of_a_name(filename, expected):
    assert seeds.derive(Path(filename), folder_fallback=False).text == expected


@pytest.mark.parametrize(
    "filename",
    ["print ready.png", "ready-to-print.png", "Baskıya Hazır.png", "Tasarım yedek.png",
     "Adsız tasarım son hali.png", "Design Final rev3.png"],
)
def test_a_name_of_markers_only_is_junk(filename):
    seed = seeds.derive(Path(filename), folder_fallback=False)
    assert not seed, f"{filename!r} became the concept {seed.text!r}"


def test_a_paw_print_and_its_revisions_share_one_concept():
    names = ("Dog Dad Paw Print.png", "dog-dad-paw-print-final.png",
             "dog_dad_paw_print_print_ready (2).png")
    assert list(seeds.group([seeds.derive(Path(n)) for n in names])) == ["dog dad paw print"]
    seed = seeds.derive(Path("dog-dad-paw-print.png"))
    assert generate.build_title(seed, None, product_hint="Retro Sunset Shirt") == (
        "Dog Dad Paw Print Shirt")


def test_a_name_keeps_only_characters_a_title_and_a_tag_accept():
    assert seeds.derive(Path("salt & pepper & co.png")).text == "salt and pepper and co"
    assert seeds.derive(Path("cat 🐱 mom $$.png")).text == "cat mom"


def test_a_default_folder_name_does_not_rescue_a_camera_file():
    assert not seeds.derive(Path("Adsız klasör/IMG_0001.jpg"))
    assert not seeds.derive(Path("untitled folder/IMG_0001.jpg"))
    assert seeds.derive(Path("retro mountain sunset/IMG_0001.jpg")).text == "retro mountain sunset"


def test_a_copy_and_its_original_share_one_concept():
    grouped = seeds.group([seeds.derive(Path(n)) for n in
                           ("Retro Sunset.png", "Retro Sunset (2).png", "Retro Sunset - Copy.png")])
    assert list(grouped) == ["retro sunset"]


# --- an invented market, run through the real research --------------------------------------


class _Search:
    def __init__(self, listings):
        self.listings = listings

    def search_active_listings(self, *, keywords, max_items=100, **_filters):
        return self.listings[:max_items]


def _pick(rng, pool, k):
    """k distinct texts from [(text, weight)], weighted; seeded, so always the same."""
    items, out = list(pool), []
    for _ in range(min(k, len(items))):
        roll = rng.random() * sum(w for _, w in items)
        for i, (_text, weight) in enumerate(items):
            roll -= weight
            if roll <= 0:
                out.append(items.pop(i)[0])
                break
    return out


def _group(groups, roll):
    """The product group a listing belongs to: [(share, heads, phrases, tags)]."""
    acc = 0.0
    for group in groups:
        acc += group[0]
        if roll <= acc:
            return group
    return groups[-1]


def _market(keyword, groups, n=200):
    rng = random.Random(keyword)
    listings = []
    for i in range(n):
        _share, heads, phrases, tags = _group(groups, rng.random())
        title = ", ".join([rng.choice(heads), *_pick(rng, phrases, rng.randint(3, 5))])
        listings.append({"listing_id": 4000000 + i, "title": title, "tags": _pick(rng, tags, 13),
                         "price": {"amount": 2000, "divisor": 100, "currency_code": "USD"},
                         "num_favorers": i})
    return research(_Search(listings), keyword, sample=n)


_SHIRT_TAGS = [("graphic tee", 9), ("gift for him", 6), ("gift for her", 7),
               ("unisex tshirt", 6), ("comfort colors", 7), ("vintage shirt", 6),
               ("birthday gift", 4), ("retro shirt", 4)]
_STICKERS = (0.15, ["Mountain Sticker", "Sunset Vinyl Sticker"],
             [("Waterproof Vinyl Sticker", 5), ("Laptop Decal", 4), ("Water Bottle Sticker", 4)],
             [("vinyl sticker", 8), ("laptop sticker", 7), ("waterproof sticker", 5),
              ("sticker", 4), ("stocking stuffer", 2)])
_MUGS = (0.1, ["Mountain Mug", "Sunset Coffee Mug"],
         [("Funny Coffee Mug", 5), ("Coffee Lover Gift", 4), ("11oz Ceramic Mug", 2)],
         [("coffee mug", 8), ("ceramic mug", 5), ("coffee lover gift", 4), ("11oz mug", 2)])


@pytest.fixture(scope="module")
def mountain():
    """The video's example: a retro mountain sunset, mostly shirts, some stickers and mugs."""
    return _market("retro mountain sunset", [
        (0.75,
         ["Retro Mountain Sunset Shirt", "Mountain Sunset T-Shirt", "Vintage Mountain Tee"],
         [("Hiking Shirt", 9), ("Nature Lover Gift", 8), ("Camping Tee", 6),
          ("Outdoor Adventure Shirt", 6), ("Vintage Hiking T-Shirt", 5),
          ("National Park Shirt", 4), ("Shirt for Women", 4), ("Unisex Comfort Colors Tee", 5),
          ("Hiker Gift", 3), ("Camping Gift for Him", 2)],
         [("hiking shirt", 10), ("mountain shirt", 9), ("nature lover gift", 8),
          ("camping shirt", 7), ("outdoor shirt", 6), ("adventure shirt", 6),
          ("retro sunset shirt", 5), ("mountain tshirt", 5), ("hiker gift", 4),
          ("camping tee", 4), *_SHIRT_TAGS]),
        _STICKERS,
        _MUGS,
    ])


@pytest.fixture(scope="module")
def coffee():
    return _market("but first coffee", [
        (0.7, ["But First Coffee Mug", "Coffee Quote Mug"],
         [("Funny Coffee Mug", 8), ("Coffee Lover Gift", 9), ("Morning Coffee Cup", 6),
          ("Gift for Coworker", 4), ("11oz Ceramic Mug", 5), ("Coffee Addict Gift", 5),
          ("Office Mug", 4), ("Birthday Gift for Her", 4)],
         [("coffee mug", 10), ("coffee lover gift", 9), ("funny coffee mug", 7),
          ("ceramic mug", 6), ("coffee addict", 5), ("gift for coworker", 4),
          ("office mug", 4), ("11oz mug", 3), ("gift for her", 6), ("coffee cup", 5)]),
        (0.3, ["But First Coffee Shirt", "Coffee Lover T-Shirt"],
         [("Funny Coffee Shirt", 6), ("Coffee Lover Tee", 5)],
         [("coffee shirt", 8), ("coffee lover shirt", 6), *_SHIRT_TAGS]),
    ])


_TITLE_ONCE = "%:&+"


def _etsy_title_problems(title: str) -> list[str]:
    """OAS createDraftListing.title: letters, digits, punctuation, maths, spaces, ™©®."""
    problems = [ch for ch in title if not (
        unicodedata.category(ch)[0] in "LP" or unicodedata.category(ch) in ("Nd", "Sm", "Zs")
        or ch in "™©®")]
    problems += [f"{ch} twice" for ch in _TITLE_ONCE if title.count(ch) > 1]
    if len(title) > MAX_TITLE_LEN:
        problems.append("too long")
    return problems


def _segments(title: str) -> list[str]:
    return [s.strip() for s in title.split(",")]


_OTHER_PRODUCTS = {"sticker", "decal", "mug", "cup", "poster", "print", "tote", "case"}


def test_the_title_reads_like_an_etsy_title(mountain):
    seed = seeds.derive(Path("Retro Mountain Sunset (2).png"))
    title = generate.build_title(seed, mountain)
    segments = _segments(title)

    assert segments[0] == "Retro Mountain Sunset Shirt", "concept and product lead"
    assert 4 <= len(segments) <= 5, "the concept, then three or four market phrases"
    assert all(len(s.split()) >= 2 for s in segments), f"a dangling single word: {title}"
    assert 90 <= len(title) <= MAX_TITLE_LEN, f"{len(title)} chars: {title}"
    assert _etsy_title_problems(title) == []
    assert "(2)" not in title and " 2" not in title
    # Every segment adds a search: no segment repeats a theme word of the concept.
    for segment in segments[1:]:
        assert not {"Retro", "Mountain", "Sunset"} & set(segment.split()), segment
    words = [w.lower() for w in title.replace(",", " ").split()]
    for word in set(words) - {"shirt", "tee", "t-shirt", "gift", "for", "and"}:
        assert words.count(word) == 1, f"{word!r} repeated in {title}"
    assert not _OTHER_PRODUCTS & set(words), f"another product in {title}"
    # The market's blank brand is a claim this seller has not made.
    assert "Comfort" not in title and "Colors" not in title
    # Title Case, joining words small.
    for segment in segments:
        for i, word in enumerate(segment.split()):
            if i and word.lower() in ("for", "and", "of", "the"):
                assert word.islower()
            else:
                assert word[:1].isupper() or word[:1].isdigit(), word


def test_the_review_case_no_longer_strings_single_words(mountain):
    # "Hummingbird Garden, Shirt, Tee, Gift, Him" came from single words outranking phrases.
    for name in ("hummingbird-garden.png", "pickleball-queen.png", "lemon-summer-vibes.png"):
        title = generate.build_title(seeds.derive(Path(name)), mountain)
        segments = _segments(title)
        assert all(len(s.split()) >= 2 for s in segments), title
        assert "Him" not in title.split() and "Gift" not in segments


def test_titles_are_deterministic(mountain):
    seed = seeds.derive(Path("retro-mountain-sunset.png"))
    assert generate.build_title(seed, mountain) == generate.build_title(seed, mountain)
    assert generate.build_tags(seed, mountain) == generate.build_tags(seed, mountain)


def test_the_order_of_equally_common_phrases_does_not_matter(mountain):
    # seo.research orders ties alphabetically now, but a report from elsewhere (an old
    # cache entry, a hand-made one) may not: the copy must not depend on row order.
    shuffled = MarketReport(**{**mountain.__dict__, "tags": mountain.tags[::-1],
                               "phrases": mountain.phrases[::-1]})
    seed = seeds.derive(Path("retro-mountain-sunset.png"))
    assert generate.build_title(seed, shuffled) == generate.build_title(seed, mountain)
    assert generate.build_tags(seed, shuffled) == generate.build_tags(seed, mountain)


def test_the_audience_closes_an_apparel_title_when_the_market_names_it(mountain):
    title = generate.build_title(seeds.derive(Path("retro-mountain-sunset.png")), mountain)
    assert title.endswith(("for Men and Women", "for Women", "for Men")), title


def test_tags_are_thirteen_distinct_searches(mountain):
    seed = seeds.derive(Path("retro-mountain-sunset.png"))
    tags = generate.build_tags(seed, mountain)
    assert len(tags) == MAX_TAGS
    assert validate_tags(tags) == []
    assert all(len(t) <= MAX_TAG_LEN for t in tags)
    assert len({generate._bag(t) for t in tags}) == len(tags), f"near-duplicates in {tags}"
    assert sum(1 for t in tags if " " in t) >= 11, f"mostly single words: {tags}"
    assert not any(set(t.split()) & _OTHER_PRODUCTS for t in tags), tags
    assert "comfort colors" not in tags


def test_the_template_decides_the_product_in_a_mixed_market(coffee):
    seed = seeds.derive(Path("But First Coffee - Copy.png"))
    mug = generate.build_title(seed, coffee, product_hint="Ceramic Coffee Mug 11oz")
    assert mug.startswith("But First Coffee Mug"), mug
    assert "Shirt" not in mug and "Tee" not in mug
    shirt = generate.build_title(seed, coffee, product_hint="Retro Graphic Tee")
    assert shirt.startswith("But First Coffee Tee"), shirt
    assert "Mug" not in shirt and "Cup" not in shirt
    tags = generate.build_tags(seed, coffee, product_hint="Retro Graphic Tee")
    assert not any({"mug", "cup"} & set(t.split()) for t in tags), tags


def test_sizes_and_materials_need_the_sellers_own_word(coffee):
    seed = seeds.derive(Path("but-first-coffee.png"))
    plain = generate.build_title(seed, coffee, product_hint="Coffee Mug")
    plain_tags = generate.build_tags(seed, coffee, product_hint="Coffee Mug")
    assert "Ceramic" not in plain and "11oz" not in plain
    assert "ceramic mug" not in plain_tags and "11oz mug" not in plain_tags
    claimed = generate.build_tags(seed, coffee, product_hint="Ceramic Coffee Mug 11oz")
    assert "ceramic mug" in claimed or "11oz mug" in claimed


def test_a_gift_phrase_repeating_the_niche_word_is_allowed_once(coffee):
    seed = seeds.derive(Path("but-first-coffee.png"))
    title = generate.build_title(seed, coffee, product_hint="Coffee Mug")
    assert "Coffee Lover Gift" in title, title
    assert title.lower().split().count("coffee") <= 2


def test_etsys_title_characters_hold_even_for_a_hand_made_seed(mountain):
    seed = Seed("salt & pepper & co 💥 $5 100% + 50% + tea: time: now", "x")
    title = generate.build_title(seed, mountain)
    assert _etsy_title_problems(title) == [], title


def test_a_very_long_concept_is_cut_at_a_word():
    seed = Seed(" ".join(["mountain"] * 40), "x")
    title = generate.build_title(seed, None)
    assert len(title) <= MAX_TITLE_LEN and title.endswith("Mountain")


def test_without_a_market_the_template_still_names_the_product():
    seed = seeds.derive(Path("lemon-summer-vibes.png"))
    assert generate.build_title(seed, None, product_hint="Retro Sunset Shirt") == (
        "Lemon Summer Vibes Shirt")
    assert generate.build_title(seed, None) == "Lemon Summer Vibes"
    tags = generate.build_tags(seed, None, product_hint="Retro Sunset Shirt")
    assert tags[0] == "lemon summer vibes" and "lemon summer vibes shirt" not in tags
    assert "lemon summer shirt" in tags and validate_tags(tags) == []
    # A single word another tag already holds is a wasted slot.
    assert not {"lemon", "summer", "vibes", "shirt"} & set(tags)


def test_a_cached_report_with_lists_for_tuples_works(mountain):
    # drop/cache stores the report as JSON, so every (phrase, count) comes back a list.
    cached = MarketReport(**{**mountain.__dict__,
                             "tags": [list(t) for t in mountain.tags],
                             "phrases": [list(p) for p in mountain.phrases]})
    seed = seeds.derive(Path("retro-mountain-sunset.png"))
    assert generate.build_title(seed, cached) == generate.build_title(seed, mountain)


def test_generate_uses_the_template_for_the_product_and_its_claims(coffee):
    seed = seeds.derive(Path("but-first-coffee.png"))
    result = generate.generate(seed, coffee, template_description="An 11oz ceramic mug.",
                               fallback_tags=["coffee mug", "gift for her"],
                               template_title="Retro Sunset Mug")
    assert result.title.startswith("But First Coffee Mug")
    assert len(result.tags) == MAX_TAGS and validate_tags(result.tags) == []
    assert not result.warnings


def test_a_junk_seed_still_produces_nothing():
    result = generate.generate(seeds.derive(Path("Adsız tasarım (3).png"), folder_fallback=False))
    assert result.title == "" and result.tags == [] and result.warnings


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("shirt for men and women", "Shirt for Men and Women"),
        ("mom's 11oz tshirt", "Mom's 11oz T-Shirt"),
        ("iphone case", "iPhone Case"),
        ("çiçek düğme", "Çiçek Düğme"),
    ],
)
def test_titlecase(text, expected):
    assert generate.titlecase(text) == expected


@pytest.mark.parametrize(
    ("candidate", "existing"),
    [("gifts", ["gift"]), ("mountain t-shirt", ["mountain shirt"]),
     ("sunset retro", ["retro sunset"]), ("hiking", ["hiking shirt"]),
     ("canvas tote bag", ["canvas tote"])],
)
def test_near_duplicate_tags_are_caught(candidate, existing):
    assert generate._too_similar(candidate, existing)


def test_different_searches_are_not_near_duplicates():
    assert not generate._too_similar("iphone 15 case", ["iphone 14 case"])
    assert not generate._too_similar("hiking shirt", ["camping shirt"])


# --- Turkish casing -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "turkish", "expected"),
    [
        ("kedi pati izi", True, "Kedi Pati İzi"),
        ("kedi pati izi", None, "Kedi Pati Izi"),  # nothing in it says Turkish
        ("çiçek izi", None, "Çiçek İzi"),  # a Turkish letter does
        ("ice cream shirt", None, "Ice Cream Shirt"),
        ("ice cream shirt", False, "Ice Cream Shirt"),
        ("iphone case", True, "iPhone Case"),  # a word's own spelling still wins
        ("ılık süt", None, "Ilık Süt"),
    ],
)
def test_titlecase_turkish(text, turkish, expected):
    assert generate.titlecase(text, turkish=turkish) == expected


def test_a_turkish_template_cases_the_concept_in_turkish():
    seed = seeds.derive(Path("kedi-pati-izi.png"))
    turkish = generate.build_title(seed, None, product_hint="Pamuklu Tişört, Kedi Sever Hediyesi")
    assert turkish.startswith("Kedi Pati İzi")
    english = generate.build_title(seed, None, product_hint="Retro Sunset Shirt")
    assert english.startswith("Kedi Pati Izi Shirt")
    ice = generate.build_title(seeds.derive(Path("ice-cream-lover.png")), None,
                               product_hint="Retro Sunset Shirt")
    assert ice.startswith("Ice Cream Lover")


# --- the market search: the concept and the template's product ---------------------------


@pytest.mark.parametrize(
    ("filename", "hint", "expected"),
    [
        # "print" is a poster's noun: alone, the search returns posters for a shirt.
        ("dog-dad-paw-print.png", "Retro Mountain Sunset Shirt, Hiking Tee", "dog dad paw print shirt"),
        ("dog-dad-paw-print.png", "Dog Poster, Wall Art Print", "dog dad paw print"),
        ("retro-sunset-mug.png", "Retro Sunset Mug, Coffee Cup", "retro sunset mug"),
        ("teacher-appreciation.png", "Canvas Tote Bag", "teacher appreciation tote bag"),
        ("dog-dad-gift.png", "Retro Sunset Shirt", "dog dad gift"),
        ("dog-dad-paw-print.png", None, "dog dad paw print"),
        ("kedi-pati-izi.png", "Monstera Phone Case", "kedi pati izi phone case"),
    ],
)
def test_research_keyword(filename, hint, expected):
    assert generate.research_keyword(seeds.derive(Path(filename)), hint) == expected


def test_the_shirt_draft_of_a_paw_print_gets_a_shirt_title():
    # The round-3 demo: "dog dad paw print" on a shirt template was searched alone, found
    # posters, and the draft's title stopped at "Dog Dad Paw Print Shirt" (23 characters).
    seed = seeds.derive(Path("dog-dad-paw-print.png"))
    hint = generate.hint_from("Retro Mountain Sunset Shirt, Vintage Hiking Tee", ["graphic tee"])
    keyword = generate.research_keyword(seed, hint)
    shirts = _market(keyword, [
        (1.0, ["Dog Dad Paw Print Shirt", "Dog Dad T-Shirt"],
         [("Fathers Day Gift", 8), ("Dog Lover Gift", 7), ("Graphic Tee", 5)],
         [("dog dad shirt", 9), ("dog lover gift", 7), ("gift for dad", 6), *_SHIRT_TAGS]),
    ])
    title = generate.build_title(seed, shirts, product_hint=hint)
    assert title.startswith("Dog Dad Paw Print Shirt, ") and len(title) > 60
    assert _etsy_title_problems(title) == []
    result = generate.generate(seed, shirts, template_title=hint[0], fallback_tags=["graphic tee"])
    assert result.sources[0] == f"200 listings ranking for {keyword!r}"


# --- the template's own tags as filler ------------------------------------------------------


_TEMPLATE_TITLE = "Retro Mountain Sunset Shirt, Vintage Mountain Tee, Hiking Gift, Graphic Top"
_TEMPLATE_TAGS = ["retro mountain sun", "mountain gift", "hiking gift", "graphic tee",
                  "vintage shirt", "gift for her", "unisex tshirt", "comfort colors",
                  "oversized tee", "Birthday Gift"]


def test_only_the_template_tags_generic_for_the_product_are_reused():
    paw = seeds.derive(Path("dog-dad-paw-print.png"))
    assert generate.product_tags(_TEMPLATE_TAGS, _TEMPLATE_TITLE, paw) == [
        "graphic tee", "vintage shirt", "gift for her", "unisex tshirt", "comfort colors",
        "oversized tee", "birthday gift",
    ]
    # A word the new design shares with the template is its own word too; the cut-off
    # "sun" still belongs to the template's "sunset".
    goat = seeds.derive(Path("mountain-goat-trail.png"))
    kept = generate.product_tags(_TEMPLATE_TAGS, _TEMPLATE_TITLE, goat)
    assert "mountain gift" in kept and "retro mountain sun" not in kept
    assert "hiking gift" not in kept
    # Without the template's title nothing is known to be its own design.
    assert generate.product_tags(["hiking gift", "Graphic Tee"], "", paw) == [
        "hiking gift", "graphic tee"]


def test_a_draft_without_market_data_gets_no_tags_of_the_template_design():
    seed = seeds.derive(Path("dog-dad-paw-print.png"))
    result = generate.generate(seed, None, template_title=_TEMPLATE_TITLE,
                               fallback_tags=_TEMPLATE_TAGS)
    assert not {"retro mountain sun", "mountain gift", "hiking gift"} & set(result.tags)
    # The template's own tags that suit any design are the shop-wide ones, and a draft takes
    # at most GENERIC_SLOTS of them: the rest of its thirteen are about this design.
    shop_wide = set(generate.product_tags(_TEMPLATE_TAGS, _TEMPLATE_TITLE, seed))
    assert "graphic tee" in result.tags
    assert len(shop_wide & set(result.tags)) == generate.GENERIC_SLOTS
    assert len(result.tags) == MAX_TAGS
    assert "your template listing's tags" in result.sources
    assert validate_tags(result.tags) == []


# --- a phrase too long for a tag still gives one ------------------------------------------------


def test_a_four_word_phrase_too_long_for_a_tag_leaves_a_shorter_one():
    # Research keeps "monstera leaf phone case" whole (24 characters, one too many words
    # for a tag) and drops the "leaf phone case" inside it, which the tags then lost.
    cases = _market("monstera leaf phone case", [
        (1.0, ["Monstera Leaf Phone Case", "Monstera Leaf iPhone Case"],
         [("Cute Phone Case", 8), ("Aesthetic Case", 5), ("Boho Case", 4), ("Plant Lover Gift", 4)],
         [("phone case", 9), ("cute phone case", 7), ("gift for her", 6), ("plant lover gift", 5)]),
    ])
    assert "leaf phone case" not in dict(cases.phrases)
    seed = seeds.derive(Path("monstera-leaf.png"))
    tags = generate.build_tags(seed, cases, product_hint="Floral Phone Case")
    assert "leaf phone case" in tags
    assert len(tags) <= MAX_TAGS and validate_tags(tags) == []
    assert generate.build_tags(seed, cases, product_hint="Floral Phone Case") == tags


def test_a_piece_never_splits_a_product_noun_or_leaves_half_a_phrase():
    def pieces(text):
        return generate._tag_pieces(generate._Phrase(tuple(generate._tokens(text)), 10))

    assert pieces("monstera leaf phone case") == ["leaf phone case"]  # not "case" alone
    assert pieces("retro mountain sunset shirt") == ["sunset shirt"]
    assert pieces("cute nature lover gift") == ["nature lover gift"]  # never "lover gift"
    assert pieces("iphone case holder stand") == ["holder stand"]  # never "case holder stand"
