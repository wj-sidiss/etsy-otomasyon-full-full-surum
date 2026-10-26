"""Problems that only exist between listings.

`audit_listing` judges one listing at a time, which makes a whole class of problem
invisible: a shop where every listing carries the same tags scores a perfect 100 on
every listing while they all compete with each other for the same query. These tests
pin the check that catches it — it was found by hand on a real shop that the
per-listing audit had just declared flawless.
"""

import pytest

from etsyotomasyon.seo import (
    SHARED_TAGS_MIN,
    audit_listing,
    audit_shop,
    generic_opening,
    overlapping_pairs,
    shared_tags,
)


def _listing(listing_id, tags, title="Soy Candle"):
    return {"listing_id": listing_id, "title": title, "tags": tags, "description": "x" * 300}


# Deliberately generic fixture vocabulary.
SHARED = ["soy wax candle", "hand poured candle", "scented candle", "gift for her",
          "home fragrance", "candle gift set", "vegan candle"]


def _shop(count, distinctive_each):
    return [
        _listing(i, SHARED + [f"unique {i}-{n}" for n in range(distinctive_each)])
        for i in range(count)
    ]


def test_a_shop_where_every_listing_says_the_same_thing_is_flagged():
    listings = [_listing(i, SHARED) for i in range(10)]
    shop = audit_shop(listings)
    codes = {i.code for i in shop.issues}
    assert "shop.tag_overlap" in codes
    assert "shop.too_alike" in codes


def test_the_per_listing_audit_still_calls_those_listings_perfect():
    # This is the blind spot, stated as a test so it cannot quietly come back:
    # a listing can be flawless on its own terms while the shop it sits in is not.
    title = "Soy Wax Candle Gift Set | Hand Poured Lavender Scented Vegan Candle"
    listing = {
        "listing_id": 1,
        "title": title,
        "description": (
            f"{title}. A soy wax candle gift set, hand poured with lavender scent, "
            "a vegan candle made for home fragrance and given as a gift. "
        ) * 3,
        "tags": [
            "soy wax candle", "hand poured candle", "scented candle", "lavender candle",
            "vegan candle", "candle gift set", "soy candle gift", "poured candle",
            "lavender scented", "candle set", "wax candle gift", "hand poured wax",
            "lavender gift",
        ],
        "materials": ["soy wax"],
        "should_auto_renew": True,
        "state": "active",
    }
    audit = audit_listing(listing)
    assert audit.score == 100, audit.summary()

    shop = audit_shop([dict(listing, listing_id=i) for i in range(10)])
    assert shop.issues, "the shop-level check must see what the listing-level one cannot"
    assert "shop.tag_overlap" in {i.code for i in shop.issues}


def test_a_varied_shop_raises_nothing():
    listings = [_listing(i, [f"tag {i}-{n}" for n in range(13)]) for i in range(10)]
    assert audit_shop(listings).issues == []


def test_shop_wide_tags_are_counted_and_ordered():
    listings = _shop(10, distinctive_each=6)
    shop = audit_shop(listings)
    names = [tag for tag, _n in shop.shop_wide]
    assert set(names) == set(SHARED)
    assert shop.shop_wide[0][1] == 10


def test_distinctive_counts_exclude_the_shared_tags():
    shop = audit_shop(_shop(10, distinctive_each=6))
    assert set(shop.distinctive_per_listing.values()) == {6}
    assert shop.median_distinctive == 6


def test_listings_with_too_little_of_their_own_are_named():
    listings = _shop(6, distinctive_each=6) + [_listing(99, SHARED + ["only one"])]
    shop = audit_shop(listings)
    crowded_ids = {row[0] for row in shop.crowded}
    assert 99 in crowded_ids
    assert all(row[0] == 99 for row in shop.crowded), "the varied listings are fine"


def test_the_worst_listing_sorts_first():
    listings = _shop(6, distinctive_each=6) + [
        _listing(98, SHARED + ["a", "b", "c"]), _listing(99, SHARED)
    ]
    crowded = audit_shop(listings).crowded
    assert crowded[0][0] == 99 and crowded[0][2] == 0


def test_a_single_listing_shop_cannot_cannibalise_itself():
    assert audit_shop([_listing(1, SHARED)]).issues == []
    assert audit_shop([]).issues == []


# --- the competing pairs --------------------------------------------------------


def test_identical_listings_are_reported_as_fully_overlapping():
    pairs = overlapping_pairs([_listing(1, SHARED, "A"), _listing(2, SHARED, "B")])
    assert pairs and pairs[0][2] == pytest.approx(1.0)


def test_unrelated_listings_are_not_reported():
    listings = [_listing(1, ["a", "b", "c"], "A"), _listing(2, ["x", "y", "z"], "B")]
    assert overlapping_pairs(listings) == []


def test_pairs_come_back_worst_first():
    listings = [
        _listing(1, SHARED, "A"),
        _listing(2, SHARED, "B"),
        _listing(3, SHARED[:4] + ["p", "q", "r"], "C"),
    ]
    pairs = overlapping_pairs(listings, threshold=0.3)
    assert pairs[0][2] >= pairs[-1][2]


def test_the_pair_list_is_capped():
    listings = [_listing(i, SHARED, f"L{i}") for i in range(8)]
    assert len(overlapping_pairs(listings, limit=3)) == 3


# --- tags shared with another listing, a tag list of the shop's own, a generic opening ------------
#
# Found by hand on a shop whose per-listing audit averaged 99: on average a listing shared 7 or
# more of its 13 tags with 23 others, and 44% of its tag slots were the same dozen shop-wide tags.


def _ids(audit, code):
    return [i for i in audit.issues if i.code == code]


def _wallpaper(listing_id, tags, title="Fig Tree Wallpaper, Olive Branch Kitchen Wallpaper"):
    return {"listing_id": listing_id, "title": title, "tags": tags, "description": "x" * 300,
            "materials": ["vinyl"], "should_auto_renew": True, "state": "active"}


OWN = [f"fig tag {n}" for n in range(13)]


def _own(prefix, n):
    return [f"{prefix} tag {i}" for i in range(n)]


def test_two_listings_sharing_seven_tags_are_each_told_who_the_other_is():
    a = _wallpaper(101, OWN[:7] + _own("a", 6))
    b = _wallpaper(102, OWN[:7] + _own("b", 6))
    c = _wallpaper(103, _own("c", 13))
    shop = audit_shop([a, b, c])
    assert shop.shared[101].other == 102 and shop.shared[101].count == 7
    assert shop.shared[102].other == 101
    assert 103 not in shop.shared
    for listing, other in ((a, 102), (b, 101)):
        shared = _ids(audit_listing(listing, shop), "tags.shared")
        assert len(shared) == 1
        issue = shared[0]
        assert issue.severity == "info" and str(other) in issue.message
        assert issue.params["other"] == other and issue.params["shared"] == 7
        assert issue.params["tags"] == sorted(OWN[:7]) and issue.params["tags_used"] == 13
    assert _ids(audit_listing(c, shop), "tags.shared") == []


def test_sharing_nine_tags_is_a_warning_and_six_is_nothing():
    base = _wallpaper(1, OWN)
    nine = _wallpaper(2, OWN[:9] + _own("n", 4))
    six = _wallpaper(3, OWN[:6] + _own("s", 7))
    shop = audit_shop([base, nine, six])
    warn = _ids(audit_listing(base, shop), "tags.shared")
    # The worst partner is named: nine tags with listing 2, not six with listing 3.
    assert [(i.severity, i.params["other"], i.params["shared"]) for i in warn] == [("warn", 2, 9)]
    assert 3 not in shop.shared
    alone = audit_listing(base).score
    assert audit_listing(base, shop).score == alone - 10, "a warning costs what a warning costs"
    only_info = audit_shop([_wallpaper(1, OWN), _wallpaper(2, OWN[:7] + _own("n", 6))])
    assert audit_listing(base, only_info).score == alone - 3


def test_a_tie_goes_to_the_lowest_listing_id():
    shop = audit_shop([_wallpaper(5, OWN), _wallpaper(9, OWN[:8] + _own("x", 5)),
                       _wallpaper(7, OWN[:8] + _own("y", 5))])
    assert shop.shared[5].other == 7


def test_the_pairwise_search_agrees_with_comparing_every_pair():
    import random

    rng = random.Random(7)
    pool = [f"tag {n}" for n in range(40)]
    per_listing = {1000 + i: set(rng.sample(pool, rng.randint(5, 13))) for i in range(80)}
    found = shared_tags(per_listing)
    for listing_id, tags in per_listing.items():
        best = max(((len(tags & other), -oid) for oid, other in per_listing.items()
                    if oid != listing_id), default=(0, 0))
        if best[0] >= SHARED_TAGS_MIN:
            assert (found[listing_id].count, -found[listing_id].other) == best
        else:
            assert listing_id not in found


def test_a_big_shop_of_near_copies_is_audited_quickly():
    import time

    generic = [f"generic tag {n}" for n in range(12)]
    listings = [_wallpaper(1000 + n, [*generic, f"unique {n}"]) for n in range(1500)]
    started = time.perf_counter()
    shop = audit_shop(listings)
    assert time.perf_counter() - started < 10
    assert len(shop.shared) == 1500 and shop.shared[1000].count == 12


GENERIC = [f"shop tag {n}" for n in range(8)]


def _generic_shop(per_listing_generic, count=10):
    """`count` listings that all carry the first `per_listing_generic` shop-wide tags."""
    return [_wallpaper(i, GENERIC[:per_listing_generic] + _own(f"own{i}", 13 - per_listing_generic))
            for i in range(1, count + 1)]


def test_a_tag_list_made_mostly_of_the_shops_generic_tags_is_a_warning():
    listings = _generic_shop(7)
    shop = audit_shop(listings)
    assert len(shop.generic_per_listing[1]) == 7
    issue = _ids(audit_listing(listings[0], shop), "tags.generic")[0]
    assert issue.severity == "warn"
    assert issue.params["generic"] == 7 and issue.params["tags_used"] == 13
    assert set(issue.params["tags"]) == set(GENERIC[:7])


def test_five_generic_tags_are_a_note_and_four_are_nothing():
    five = _generic_shop(5)
    shop = audit_shop(five)
    issue = _ids(audit_listing(five[0], shop), "tags.generic")[0]
    assert issue.severity == "info" and issue.params["generic"] == 5
    assert audit_listing(five[0], shop).score == audit_listing(five[0]).score - 3
    four = _generic_shop(4)
    assert _ids(audit_listing(four[0], audit_shop(four)), "tags.generic") == []


def test_a_listing_with_its_own_tags_is_not_flagged_for_the_shops():
    mixed = _generic_shop(7) + [_wallpaper(99, _own("mine", 13))]
    shop = audit_shop(mixed)
    assert _ids(audit_listing(mixed[-1], shop), "tags.generic") == []
    assert 99 not in shop.generic_per_listing


def test_a_small_shop_has_no_shop_wide_tags_to_overuse():
    few = _generic_shop(8, count=4)
    shop = audit_shop(few)
    assert shop.generic_per_listing == {}
    assert _ids(audit_listing(few[0], shop), "tags.generic") == []


def test_without_a_shop_a_listing_is_judged_exactly_as_before():
    listing = _wallpaper(1, OWN)
    shop = audit_shop([listing, _wallpaper(2, OWN)])
    plain = audit_listing(listing)
    assert {i.code for i in plain.issues}.isdisjoint({"tags.shared", "tags.generic"})
    assert plain.score == audit_listing(listing, None).score
    assert audit_listing(listing, shop).score < plain.score


# The opening of a title.


@pytest.mark.parametrize(("title", "level"), [
    ("Kitchen Wallpaper | Citrus Grove Lemon Botanical | Peel and Stick | Removable", "lead"),
    ("Peel and Stick Removable Self Adhesive Wallpaper Mural Wall Decor For Any Room", "window"),
    ("Bedroom Decor, Wall Art, Gift for Her", "window"),
    ("Lemon Wallpaper, Kitchen Wallpaper, Peel and Stick", None),
    ("Fig Tree Wallpaper | Olive Branch Botanical | Kitchen", None),
    ("Mediterranean Lemon Wallpaper Peel and Stick Removable", None),
])
def test_a_title_must_open_with_a_word_that_is_about_the_design(title, level):
    found = generic_opening(title)
    assert (found[0] if found else None) == level
    codes = [i for i in audit_listing(_wallpaper(1, OWN, title)).issues
             if i.code == "title.generic_opening"]
    assert [i.severity for i in codes] == {"lead": ["info"], "window": ["warn"], None: []}[level]
    if codes:
        assert codes[0].params["level"] == level and codes[0].params["visible"] == 40


def test_a_generic_opening_is_not_counted_twice_with_an_empty_front():
    # No keyword at all in the first 40 characters is title.front_empty's finding already.
    title = "of the and for the in on at to " * 2 + "Lemon Wallpaper Botanical Mural"
    codes = {i.code for i in audit_listing(_wallpaper(1, OWN, title)).issues}
    assert "title.front_empty" in codes and "title.generic_opening" not in codes


def test_titles_that_open_well_keep_their_scores():
    # Unchanged for every title the check does not fire on.
    title = "Soy Wax Candle Gift Set | Hand Poured Lavender Scented Vegan Candle"
    listing = _listing(1, SHARED + [f"candle {n}" for n in range(6)], title)
    assert generic_opening(title) is None
    assert not [i for i in audit_listing(listing).issues if i.code == "title.generic_opening"]
