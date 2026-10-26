"""The title and tag builders against what a seller audit found wrong with them.

A shop's tags were 44% shop-wide material tags that every listing carried, so its listings
competed with each other; its titles opened with a generic room phrase and promised
"removable, self adhesive" for a product that also comes as paste-up paper. These tests
build real-looking cases (a nursery wallpaper, a T-shirt, a Turkish-named design) through
the real research and check what a seller would see: where the design's own phrase lands,
which claims are written, how many tags are the shop's, how alike two drafts are.
"""

from __future__ import annotations

import re
from itertools import combinations
from pathlib import Path

import pytest
from copy_helpers import (
    SHIRT_TEMPLATE_TAGS,
    SHIRT_TITLE,
    WALLPAPER_BOTH,
    WALLPAPER_PEEL_ONLY,
    WALLPAPER_TAGS,
    WALLPAPER_TITLE,
    cat_shirt,
    jungle_wallpaper,
    paw_shirt,
    wallpaper_market,
)

from etsyotomasyon import seo, vocab
from etsyotomasyon.config import MAX_TAG_LEN, MAX_TAGS, MAX_TITLE_LEN
from etsyotomasyon.drop import generate, seeds
from etsyotomasyon.listings import title_problems, validate_tags
from etsyotomasyon.seo import MarketReport, tag_key

# The template listings: a wallpaper shop that sells peel and stick only, one that also sells
# a paste-up paper, one that makes no claim about how it goes on, and a T-shirt shop.
PEEL_ONLY = generate.hint_from(WALLPAPER_TITLE, WALLPAPER_TAGS, WALLPAPER_PEEL_ONLY,
                               ["Peel and Stick Vinyl"])
BOTH = generate.hint_from(WALLPAPER_TITLE, WALLPAPER_TAGS, WALLPAPER_BOTH,
                          ["Peel and Stick", "Traditional"])
NO_CLAIM = generate.hint_from("Kitchen Wallpaper | Citrus Grove Lemon Botanical | Wall Mural",
                              ["wallpaper mural", "kitchen wallpaper", "citrus decor"],
                              "A printed wallpaper.", ["Vinyl"])
SHIRT = generate.hint_from(SHIRT_TITLE, SHIRT_TEMPLATE_TAGS, "A soft cotton shirt.", ["cotton"])
# A shop in Turkish: the word "Tişört" says so, "Shirt" names the product.
TURKISH = generate.hint_from("Sevimli Kedi Tişört, Komik Hediye Shirt",
                             ["graphic tee", "unisex tshirt", "comfort colors", "gift for her"],
                             "Yumuşak pamuklu tişört.")

CLAIM_WORDS = {"peel", "stick", "removable", "adhesive", "renter", "renters", "temporary"}


@pytest.fixture(scope="module")
def jungle():
    return jungle_wallpaper()


@pytest.fixture(scope="module")
def cats():
    return cat_shirt()


def _seed(filename: str) -> seeds.Seed:
    return seeds.derive(Path(filename))


def _words(text: str) -> list[str]:
    return seo.words(text)


def _segments(title: str) -> list[str]:
    return [s.strip() for s in title.split(",")]


def _generic_by_the_spec(tag: str, report: MarketReport, template_tags, design: set[str]) -> bool:
    """The task's own definition: on the template's tag list, or among the market's ten
    most used tags at a quarter of the sample or more, and holding no word of the design."""
    if set(_words(tag)) & design:
        return False
    top = sorted(report.tags, key=lambda row: (-row[1], row[0]))[:10]
    market = {t for t, count in top if count / report.sampled >= 0.25}
    return tag in {tag_key(t) for t in template_tags} or tag in market


# --- the title ------------------------------------------------------------------------------


@pytest.mark.parametrize("hint", [PEEL_ONLY, BOTH, NO_CLAIM], ids=["peel", "both", "no claim"])
def test_a_wallpaper_title_leads_with_the_design_not_the_room_or_the_material(jungle, hint):
    seed = _seed("jungle-animals.png")
    title = generate.build_title(seed, jungle, product_hint=hint)
    segments = _segments(title)

    assert segments[0] == "Jungle Animals Wallpaper", title
    # The design and the product land inside the first forty characters a buyer reads.
    assert title.index("Wallpaper") + len("Wallpaper") <= 40
    assert title_problems(title) == [] and len(title) <= MAX_TITLE_LEN
    assert seo.generic_opening(title) is None, "the audit's own opening check passes it"
    # Up to two phrases of what it is, one of a room or look: head + 3 market phrases.
    market = segments[1:-1] if hint is not NO_CLAIM else segments[1:]
    assert 2 <= len(market) <= 3, title
    assert any(set(_words(s)) & vocab.ROOMS | set(_words(s)) & vocab.STYLES
               for s in market), f"a room or look closes the market phrases: {title}"
    # Every phrase adds a word: nothing is said twice, and no filler.
    seen: set[str] = set()
    for segment in segments[1:]:
        fresh = {w for w in _words(segment) if w not in seen}
        assert fresh - {"and", "or"}, f"{segment!r} adds nothing to {title}"
        seen |= set(_words(segment))
    assert "Wall Decor" not in title and "Home Decor" not in title
    assert title.lower().count("mural") <= 1
    counts = {w: _words(title).count(w) for w in set(_words(title))}
    assert max(counts[w] for w in counts if w not in ("wallpaper", "and")) <= 2, counts
    assert counts.get("wallpaper", 0) <= 2


def test_the_material_qualifier_is_the_sellers_own_and_goes_last(jungle):
    seed = _seed("jungle-animals.png")
    peel = generate.build_title(seed, jungle, product_hint=PEEL_ONLY)
    both = generate.build_title(seed, jungle, product_hint=BOTH)
    none = generate.build_title(seed, jungle, product_hint=NO_CLAIM)
    assert peel.endswith(", Peel and Stick")
    assert both.endswith(", Peel and Stick or Traditional")
    assert not _words(none) or not CLAIM_WORDS & set(_words(none)), none
    # The qualifier is said once, and only there: no phrase in front of it repeats a claim.
    for title in (peel, both):
        body = title.rsplit(", ", 1)[0]
        assert not CLAIM_WORDS & set(_words(body)), body


@pytest.mark.parametrize("filename", ["jungle-animals.png", "pastel-woodland-nursery.png",
                                      "fig-tree.png"])
def test_a_paste_up_product_is_never_called_removable_or_self_adhesive(jungle, filename):
    seed = _seed(filename)
    title = generate.build_title(seed, jungle, product_hint=BOTH)
    tags = generate.build_tags(seed, jungle, product_hint=BOTH)
    body = title.rsplit(", Peel and Stick or Traditional", 1)[0]
    assert not CLAIM_WORDS & set(_words(body)), title
    for tag in tags:
        assert not vocab.adhesive_claims(_words(tag)), f"{tag!r} claims what only peel and stick is"
    # The claims the market makes are the seller's to make, and this template does not.
    assert not any(t in tags for t in ("removable wallpaper", "self adhesive", "renter friendly",
                                       "temporary wallpaper", "peel and stick"))


def test_a_peel_and_stick_only_template_may_tag_what_it_sells(jungle):
    tags = generate.build_tags(_seed("jungle-animals.png"), jungle, product_hint=PEEL_ONLY)
    claims = [t for t in tags if vocab.adhesive_claims(_words(t))]
    assert 1 <= len(claims) <= generate.GENERIC_SLOTS, tags


def test_no_claim_is_written_that_the_template_does_not_make(jungle):
    seed = _seed("jungle-animals.png")
    for hint in (NO_CLAIM, None, "Wallpaper"):
        tags = generate.build_tags(seed, jungle, product_hint=hint)
        title = generate.build_title(seed, jungle, product_hint=hint)
        assert not any(vocab.adhesive_claims(_words(t)) for t in tags), (hint, tags)
        assert not CLAIM_WORDS & set(_words(title)), (hint, title)


def test_the_qualifier_follows_what_a_template_says_in_each_place():
    # Title, tags or materials make the claim; title, tags, materials or description say paste.
    def qualifier(**parts):
        return generate._seller(generate.hint_from(**parts)).qualifier

    assert qualifier(title="Floral Wallpaper, Self Adhesive") == "Peel and Stick"
    assert qualifier(title="Floral Wallpaper", tags=["removable wallpaper"]) == "Removable"
    assert qualifier(title="Floral Wallpaper", materials=["Peel and Stick"]) == "Peel and Stick"
    assert qualifier(title="Floral Wallpaper, Peel and Stick",
                     materials=["Traditional"]) == "Peel and Stick or Traditional"
    assert qualifier(title="Floral Wallpaper, Peel and Stick",
                     tags=["traditional wallpaper"]) == "Peel and Stick or Traditional"
    assert qualifier(title="Floral Wallpaper, Peel and Stick",
                     description="Or pre-pasted: just add water.") == "Peel and Stick or Traditional"
    assert qualifier(title="Floral Wallpaper, Peel and Stick",
                     description="A traditional floral pattern.") == "Peel and Stick"
    assert qualifier(title="Floral Wallpaper", description="Removable, no glue.") == ""
    assert qualifier(title="Traditional Floral Wallpaper") == ""
    assert qualifier(title="Lemon Print Shirt") == ""


def test_a_t_shirt_title_keeps_its_shape_and_makes_no_wallpaper_claim(cats):
    seed = _seed("cat-mom-life.png")
    title = generate.build_title(seed, cats, product_hint=SHIRT)
    segments = _segments(title)
    assert segments[0] == "Cat Mom Life Shirt"
    assert title.index("Shirt") + len("Shirt") <= 40
    assert title_problems(title) == [] and len(title) <= MAX_TITLE_LEN
    assert 3 <= len(segments) <= 5, title
    assert title.endswith(("for Men and Women", "for Women", "for Men")), title
    assert not {"mug", "coffee"} & set(_words(title)), "the mugs in the market are not this shirt"
    assert not CLAIM_WORDS & set(_words(title))
    assert seo.generic_opening(title) is None


def test_a_turkish_named_design_is_cased_and_led_in_turkish():
    seed = _seed("kedi-pati-izi.png")
    market = paw_shirt(generate.research_keyword(seed, TURKISH))
    title = generate.build_title(seed, market, product_hint=TURKISH)
    assert title.startswith("Kedi Pati İzi Shirt, "), title
    assert title.index("Shirt") + len("Shirt") <= 40
    assert title_problems(title) == [] and len(title) <= MAX_TITLE_LEN


def test_a_long_concept_still_leads_and_the_qualifier_always_fits(jungle):
    seed = _seed("a-very-long-name-for-an-enchanted-forest-of-woodland-creatures-and-mushrooms.png")
    title = generate.build_title(seed, jungle, product_hint=BOTH)
    assert len(title) <= MAX_TITLE_LEN
    assert title.startswith("A Very Long Name")
    assert title.endswith("Peel and Stick or Traditional") or title.count(",") == 0


# --- the tags ------------------------------------------------------------------------------


@pytest.mark.parametrize("hint", [PEEL_ONLY, BOTH, NO_CLAIM], ids=["peel", "both", "no claim"])
def test_a_wallpaper_gets_thirteen_tags_and_at_most_three_are_the_shops(jungle, hint):
    seed = _seed("jungle-animals.png")
    tags = generate.build_tags(seed, jungle, product_hint=hint)
    template_tags = hint.tags
    design = {"jungle", "animals", "animal"}

    assert len(tags) == MAX_TAGS and validate_tags(tags) == []
    assert all(len(t) <= MAX_TAG_LEN for t in tags)
    assert len({tag_key(t) for t in tags}) == MAX_TAGS
    shop_wide = [t for t in tags if _generic_by_the_spec(t, jungle, template_tags, design)]
    assert len(shop_wide) <= generate.GENERIC_SLOTS, shop_wide
    about_the_design = [t for t in tags if set(_words(t)) & design]
    assert len(about_the_design) >= 8, tags
    assert "jungle wallpaper" in tags and "jungle mural" in tags
    assert tags[0] == "jungle animals" or "jungle animals" in tags


def test_no_two_tags_are_one_search(jungle, cats):
    for seed, market, hint in ((_seed("jungle-animals.png"), jungle, PEEL_ONLY),
                               (_seed("fig-tree.png"), jungle, BOTH),
                               (_seed("cat-mom-life.png"), cats, SHIRT)):
        tags = generate.build_tags(seed, market, product_hint=hint)
        assert seo._near_duplicates([t.lower() for t in tags]) == []
        assert len({generate._bag(t) for t in tags}) == len(tags), tags
        assert len({tag_key(t) for t in tags}) == len(tags)
        assert validate_tags(tags) == []


def test_the_templates_own_look_is_not_the_shops(jungle):
    # "cottage kitchen", "coastal wallpaper", "lemon mural" are about the template's design.
    tags = generate.build_tags(_seed("jungle-animals.png"), jungle, product_hint=BOTH)
    assert not {"cottage kitchen", "country kitchen", "coastal wallpaper", "citrus decor",
                "lemon mural"} & set(tags)


def test_a_t_shirt_takes_a_few_of_its_templates_tags_and_the_rest_are_its_own(cats):
    seed = _seed("cat-mom-life.png")
    tags = generate.build_tags(seed, cats, product_hint=SHIRT)
    assert len(tags) == MAX_TAGS and validate_tags(tags) == []
    shop = {"graphic tee", "unisex tshirt", "comfort colors", "gift for her", "oversized tee"}
    assert 1 <= len(shop & set(tags)) <= generate.GENERIC_SLOTS
    assert "retro mountain sun" not in tags, "a tag about the template's own design"
    assert sum(1 for t in tags if "cat" in _words(t)) >= 8, tags
    assert not any({"mug", "coffee"} & set(_words(t)) for t in tags)


def test_a_turkish_named_design_gets_valid_tags_led_by_its_own_name():
    seed = _seed("kedi-pati-izi.png")
    market = paw_shirt(generate.research_keyword(seed, TURKISH))
    tags = generate.build_tags(seed, market, product_hint=TURKISH)
    assert tags[0] == "kedi pati izi"
    assert "kedi pati izi shirt" in tags
    assert len(tags) == MAX_TAGS and validate_tags(tags) == []
    assert sum(1 for t in tags if {"graphic tee", "unisex tshirt", "comfort colors",
                                   "gift for her"} & {t}) <= generate.GENERIC_SLOTS
    # Without any market it is still a full, valid list.
    bare = generate.build_tags(seed, None, product_hint=TURKISH)
    assert len(bare) == MAX_TAGS and validate_tags(bare) == [] and bare[0] == "kedi pati izi"


def test_the_copy_is_deterministic_and_does_not_depend_on_the_order_of_the_rows(jungle):
    seed = _seed("jungle-animals.png")
    shuffled = MarketReport(**{**jungle.__dict__, "tags": jungle.tags[::-1],
                               "phrases": jungle.phrases[::-1]})
    for hint in (PEEL_ONLY, BOTH):
        assert (generate.build_title(seed, jungle, product_hint=hint)
                == generate.build_title(seed, jungle, product_hint=hint)
                == generate.build_title(seed, shuffled, product_hint=hint))
        assert (generate.build_tags(seed, jungle, product_hint=hint)
                == generate.build_tags(seed, jungle, product_hint=hint)
                == generate.build_tags(seed, shuffled, product_hint=hint))


# --- ten drafts in one shop ---------------------------------------------------------------

DESIGNS = ["jungle-animals", "dala-horse", "lemon-grove", "fig-tree", "gothic-moth",
           "mushroom-forest", "eucalyptus-leaves", "hot-air-balloon", "pastel-woodland-nursery",
           "ocean-waves"]


def _ten_drafts(hint=BOTH):
    drafts: list[list[str]] = []
    for name in DESIGNS:
        seed = _seed(f"{name}.png")
        market = wallpaper_market(" ".join(name.split("-")[:2]))
        drafts.append(generate.build_tags(seed, market, product_hint=hint, taken=drafts))
    return drafts


def test_ten_drafts_never_share_seven_tags():
    drafts = _ten_drafts()
    for a, b in combinations(range(len(drafts)), 2):
        both = set(drafts[a]) & set(drafts[b])
        assert len(both) < seo.SHARED_TAGS_MIN, (DESIGNS[a], DESIGNS[b], sorted(both))
    for tags in drafts:
        assert len(tags) == MAX_TAGS and validate_tags(tags) == []
    assert _ten_drafts() == drafts, "the same run gives the same tags"


def test_ten_drafts_are_not_ten_copies_even_when_the_market_is_the_same(jungle):
    # One market for ten different designs: the market's tags are all there is to borrow.
    drafts: list[list[str]] = []
    for name in DESIGNS:
        drafts.append(generate.build_tags(_seed(f"{name}.png"), jungle, product_hint=BOTH,
                                          taken=drafts))
    for a, b in combinations(drafts, 2):
        assert len(set(a) & set(b)) < seo.SHARED_TAGS_MIN
    shop_wide = [t for t in drafts[-1] if _generic_by_the_spec(t, jungle, BOTH.tags, set())]
    assert len(shop_wide) <= generate.GENERIC_SLOTS


def test_the_same_design_again_avoids_what_the_first_draft_took(jungle):
    seed = _seed("jungle-animals.png")
    first = generate.build_tags(seed, jungle, product_hint=BOTH)
    assert generate.build_tags(seed, jungle, product_hint=BOTH) == first
    second = generate.build_tags(seed, jungle, product_hint=BOTH, taken=[first])
    assert second != first and len(second) == MAX_TAGS
    assert len(set(first) & set(second)) < seo.SHARED_TAGS_MIN
    # The history may be older than the tags it holds: spacing and case do not matter.
    again = generate.build_tags(seed, jungle, product_hint=BOTH,
                                taken=[[t.upper() + " " for t in first]])
    assert again == second


def test_a_tag_used_before_is_still_used_when_nothing_else_is_left():
    # One word and no market: the design has five things to say, and a draft that is the
    # same design again says them again rather than leaving the slots empty.
    seed = _seed("moth.png")
    first = generate.build_tags(seed, None, product_hint=NO_CLAIM)
    second = generate.build_tags(seed, None, product_hint=NO_CLAIM, taken=[first])
    assert first[:2] == ["moth", "moth wallpaper"] and "moth mural" in first
    assert second == first and validate_tags(second) == []


def test_old_history_entries_without_tags_are_fine():
    from etsyotomasyon.drop import automation

    history = {"a.png": {"status": "ok"}, "b.png": {"status": "ok", "tags": ["x y", "z"]},
               "c.png": {"status": "error", "tags": "not a list"}, "d.png": {"status": "ok",
                                                                             "tags": [1, "", "w"]}}
    assert automation.used_tags(history) == [["x y", "z"], ["w"]]
    assert automation.used_tags({}) == []


# --- the SEO page's suggestions follow the same rules ------------------------------------------


def _report(tags, sampled=100) -> MarketReport:
    return MarketReport(keyword="k", sampled=sampled, tags=tags, phrases=[], price_min=None,
                        price_median=None, price_max=None, currency="USD", median_favorers=None,
                        top_listings=[])


def test_suggestions_skip_near_duplicates_and_claims_the_listing_does_not_make():
    report = _report([("fig wallpaper", 40), ("fig wallpapers", 38), ("removable wallpaper", 35),
                      ("peel and stick", 30), ("fig tree mural", 20), ("pear mural", 10),
                      ("fig  mural", 9)])
    existing = ["fig wallpaper", "pear tree"]
    plain = generate.suggest_additions(
        report, existing=existing, title="Fig Wallpaper",
        text=generate.hint_from("Fig Wallpaper", existing, "A printed wallpaper."))
    # "fig wallpapers" is "fig wallpaper"; the listing makes no claim about how it goes on;
    # "fig  mural" is "fig mural".
    assert [t for t, _n in plain] == ["fig tree mural", "pear mural", "fig mural"]
    mixed = generate.suggest_additions(
        report, existing=existing, title="Fig Wallpaper",
        text=generate.hint_from("Fig Wallpaper, Peel and Stick", existing,
                                "Peel and stick or traditional paper.", ["Traditional"]))
    assert not any(vocab.adhesive_claims(_words(t)) for t, _n in mixed)
    sticky = generate.suggest_additions(
        report, existing=existing, title="Fig Wallpaper",
        text=generate.hint_from("Fig Wallpaper, Removable, Peel and Stick", existing, ""))
    assert {"removable wallpaper", "peel and stick"} <= {t for t, _n in sticky}


def test_suggestions_never_add_a_fourth_generic_tag_or_a_seventh_shared_one():
    generic_rows = [("removable wallpaper", 60), ("wallpaper mural", 55), ("nursery wallpaper", 50),
                    ("fig wallpaper", 45), ("fig mural", 44), ("fig kitchen", 43),
                    ("fig pattern", 42)]
    report = _report(generic_rows)
    have = ["peel and stick", "self adhesive", "renter friendly", "fig tree"]
    text = generate.hint_from("Fig Wallpaper, Peel and Stick, Removable", have, "")
    # Already three generic tags: no more of those, the design's own are offered.
    got = [t for t, _n in generate.suggest_additions(
        report, existing=have, title="Fig Wallpaper", text=text,
        wide=["peel and stick", "self adhesive", "renter friendly"])]
    assert "removable wallpaper" not in got and "wallpaper mural" not in got
    assert got[:2] == ["fig wallpaper", "fig mural"]
    # Another listing holds six of this listing's tags: nothing from it is offered.
    mine = ["fig a", "fig b", "fig c", "fig d", "fig e", "fig f"]
    other = [*mine, "fig mural"]
    capped = [t for t, _n in generate.suggest_additions(
        report, existing=mine, title="Fig Wallpaper", shop=[other])]
    assert "fig mural" not in capped and "fig wallpaper" in capped


# --- the words ------------------------------------------------------------------------------------


@pytest.mark.parametrize(("words", "claims"), [
    (["peel", "and", "stick", "wallpaper"], {"peel and stick"}),
    (["peel", "stick"], {"peel and stick"}),
    (["self", "adhesive", "wall"], {"adhesive"}),
    (["removable"], {"removable"}), (["repositionable", "decals"], {"removable"}),
    (["renter", "friendly"], {"renter friendly"}), (["temporary", "wallpaper"], {"temporary"}),
    (["lemon", "peel", "wallpaper"], set()), (["stick", "figure"], set()), (["sticker"], set()),
])
def test_adhesive_claims(words, claims):
    assert vocab.adhesive_claims(words) == claims


@pytest.mark.parametrize(("title", "tags", "materials", "description", "paste"), [
    ("Wallpaper", [], [], "", False),
    ("Wallpaper, Traditional or Peel and Stick", [], [], "", True),
    ("Wallpaper", ["pre-pasted wallpaper"], [], "", True),
    ("Wallpaper", [], ["Traditional"], "", True),
    ("Wallpaper", [], [], "Paste the wall, not the paper.", True),
    ("Wallpaper", [], [], "Use wallpaper paste (not included).", True),
    ("Wallpaper", [], [], "A traditional damask pattern.", False),
    ("Wallpaper", [], [], "Non-adhesive backing.", True),
    ("Wallpaper", [], [], "Copy and paste the code.", False),
])
def test_a_template_that_sells_paste_is_told_by_every_place_it_can_say_so(
        title, tags, materials, description, paste):
    assert vocab.mentions_paste([title, *tags, *materials], [description]) is paste


@pytest.mark.parametrize(("word", "design"), [
    ("lemon", True), ("moth", True), ("dala", True), ("mediterranean", True), ("paw", True),
    ("kitchen", False), ("wallpaper", False), ("removable", False), ("gift", False),
    ("nursery", False), ("mural", False), ("decor", False), ("vintage", False), ("for", False),
    ("12", False), ("hediye", False),
])
def test_what_is_a_design_word(word, design):
    assert vocab.is_design_word(word) is design


def test_the_generator_and_the_audit_agree_on_what_leads():
    titles = [
        "Kitchen Wallpaper | Mediterranean Lemon Botanical | Peel and Stick",
        "Peel and Stick Removable Self Adhesive Wallpaper Mural",
        "Fig Tree Wallpaper, Olive Branch Kitchen Wallpaper, Peel and Stick",
    ]
    assert [seo.generic_opening(t) and seo.generic_opening(t)[0] for t in titles] == [
        "lead", "window", None]
    assert re.fullmatch(r"[\w .,|-]+", seo.title_lead(titles[0]))
    assert seo.title_lead(titles[0]) == "Kitchen Wallpaper"
