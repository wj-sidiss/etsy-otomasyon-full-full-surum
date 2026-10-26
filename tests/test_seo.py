from __future__ import annotations

import hashlib
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

import pytest

from etsyotomasyon.config import MAX_TAGS, MAX_TITLE_LEN, cache_dir
from etsyotomasyon.drop import cache, generate, seeds
from etsyotomasyon.listings import validate_tags
from etsyotomasyon.seo import (
    PHRASE_ROWS,
    SINGLE_WORD_ROWS,
    Issue,
    MarketReport,
    audit_listing,
    content_words,
    ngrams,
    research,
    suggest_tags,
    title_phrases,
    title_segments,
)

ROOT = Path(__file__).resolve().parents[1]


def make_listing(**overrides):
    listing = {
        "listing_id": 1,
        "title": "Handmade Ceramic Coffee Mug Minimalist Stoneware Cup Gift for Coffee Lover",
        "description": "A wheel-thrown ceramic coffee mug in matte cream glaze. " * 4,
        "tags": [f"tag {i}" for i in range(13)],
        "materials": ["stoneware"],
        "should_auto_renew": True,
        "state": "active",
    }
    listing.update(overrides)
    return listing


def codes(listing):
    return {i.code for i in audit_listing(listing).issues}


def test_clean_listing_scores_well():
    listing = make_listing(tags=["ceramic mug"] + [f"coffee gift {i}" for i in range(12)])
    audit = audit_listing(listing)
    assert audit.score >= 60


def test_empty_tags_is_an_error():
    assert "tags.missing" in codes(make_listing(tags=[]))


def test_unused_tag_slots_flagged():
    assert "tags.unused" in codes(make_listing(tags=["mug", "cup"]))


def test_duplicate_tags_flagged():
    assert "tags.duplicate" in codes(make_listing(tags=["mug"] * 13))


def test_near_duplicate_tags_flagged():
    issues = codes(make_listing(tags=["gift", "gifts"] + [f"other {i}" for i in range(11)]))
    assert "tags.near_duplicate" in issues


def test_long_tag_is_an_error():
    assert "tags.too_long" in codes(make_listing(tags=["a" * 21] + ["b c"] * 12))


def test_short_title_flagged():
    assert "title.too_short" in codes(make_listing(title="Mug"))


def test_over_length_title_is_an_error():
    assert "title.too_long" in codes(make_listing(title="x" * 141))


def test_repeated_word_in_title_flagged():
    assert "title.repetition" in codes(make_listing(title="Mug mug mug ceramic coffee cup gift set"))


def test_thin_description_flagged():
    assert "description.thin" in codes(make_listing(description="Short."))


def test_expired_listing_flagged():
    assert "state.expired" in codes(make_listing(state="expired"))


def test_empty_listing_is_graded_poor():
    audit = audit_listing({"listing_id": 2, "title": "", "description": "", "tags": []})
    assert {"title.missing", "tags.missing", "description.missing"} <= codes(
        {"listing_id": 2, "title": "", "description": "", "tags": []}
    )
    assert audit.grade == "poor"


def test_score_is_clamped_at_zero():
    audit = audit_listing({"listing_id": 3, "title": "", "description": "", "tags": []})
    audit.issues.extend(Issue("x", "error", "synthetic") for _ in range(10))
    assert audit.score == 0


def test_content_words_drops_stopwords_and_keeps_unicode():
    assert content_words("Gift for the Çiçek düğme") == ["gift", "çiçek", "düğme"]


def test_ngrams_skip_windows_containing_stopwords():
    assert list(ngrams(["gift", "for", "her"], 2)) == []
    assert list(ngrams(["ceramic", "coffee", "mug"], 2)) == ["ceramic coffee", "coffee mug"]


def test_suggest_tags_excludes_what_you_already_have():
    report = MarketReport(
        keyword="mug", sampled=100,
        tags=[("ceramic mug", 60), ("coffee gift", 40), ("stoneware", 30)],
        phrases=[], price_min=None, price_median=None, price_max=None,
        currency="USD", median_favorers=None, top_listings=[],
    )
    assert suggest_tags(report, existing=["Ceramic Mug"]).add_now == ["coffee gift", "stoneware"]


# --- market research: phrases a title and tags can be built from -----------------------
#
# Every market here is invented (listing ids from 1000001, no real shop or listing).


class _Search:
    def __init__(self, listings):
        self.listings = listings

    def search_active_listings(self, *, keywords, max_items=100, **_filters):
        return iter(self.listings[:max_items])


def _listing(i, title, tags=()):
    return {"listing_id": 1000001 + i, "title": title, "tags": list(tags),
            "price": {"amount": 2000, "divisor": 100, "currency_code": "USD"},
            "num_favorers": i}


_HEADS = ["Retro Sunset T-Shirt", "Retro Sunset Shirt", "Vintage Sunset Tee"]
_POOL = ["Nature Lover Gift", "Hiking Shirt for Women", "Vintage Graphic Tee",
         "Mother’s Day Gift", "11oz Coffee Mug", "Camping Tee", "Outdoor Adventure Shirt",
         "Unisex Comfort Colors Tee", "Gift for Him", "St. Patrick's Day Shirt",
         "Trendy Graphic Tee", "Boho Sunset Shirt", "Road Trip Shirt", "Beach Vacation Tee",
         "Summer Camp Shirt", "Birthday Gift for Her", "Oversized Summer Shirt",
         "Aesthetic Clothing", "Desert Sunset Tee", "Sunset Lover Gift", "Mountain Adventure Tee",
         "Family Reunion Shirt"]
_SEPARATORS = [", ", " | ", " - ", " – ", " / "]
_TAG_POOL = ["hiking shirt", "nature lover gift", "graphic tee", "gift for her",
             "retro shirt", "camping tee", "Retro  Sunset", "sunset shirt"]


def _market_listings(n=200, seed=7):
    """Shirts whose titles chain the pool's phrases with , | - – and /."""
    rng = random.Random(seed)
    listings = []
    for i in range(n):
        parts = [rng.choice(_HEADS), *rng.sample(_POOL, rng.randint(2, 4))]
        title = parts[0]
        for part in parts[1:]:
            title += rng.choice(_SEPARATORS) + part
        listings.append(_listing(i, title, rng.sample(_TAG_POOL, 4)))
    return listings


def _report(listings, keyword="retro sunset"):
    return research(_Search(listings), keyword, sample=len(listings))


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Retro Sunset T-Shirt, Mother’s Day Gift | 11oz Mug",
         [["retro", "sunset", "t-shirt"], ["mother's", "day", "gift"], ["11oz", "mug"]]),
        ("Hiking Shirt - Camping Tee – Outdoor Gift — Nature Lover",
         [["hiking", "shirt"], ["camping", "tee"], ["outdoor", "gift"], ["nature", "lover"]]),
        ("St. Patrick's Day Shirt / Lucky Tee",
         [["st", "patrick's", "day", "shirt"], ["lucky", "tee"]]),
        ('Funny "Dad Joke" Mug, #1 Dad Gift',
         [["funny", "dad", "joke", "mug"], ["1", "dad", "gift"]]),
        ("Salt & Pepper Shakers (Set of 2): Kitchen Decor 🌿 Housewarming Gift",
         [["salt"], ["pepper", "shakers"], ["set", "of", "2"], ["kitchen", "decor"],
          ["housewarming", "gift"]]),
        ("8.5x11 Print, 3/4 Sleeve Raglan, Mid-Century Modern Art",
         [["8.5x11", "print"], ["3/4", "sleeve", "raglan"], ["mid-century", "modern", "art"]]),
        ("İSTANBUL Skyline Poster™ Travel Gift", [["istanbul", "skyline", "poster", "travel",
                                                   "gift"]]),
        ("", []),
    ],
)
def test_a_title_splits_into_its_own_phrases(title, expected):
    assert title_segments(title) == expected


def test_phrases_stay_inside_one_phrase_of_the_title():
    found = title_phrases("Vintage Hiking T-Shirt Gift, Nature Lover Gift for Him")
    assert {"vintage hiking t-shirt gift", "vintage hiking t-shirt", "hiking t-shirt",
            "nature lover gift", "t-shirt"} <= found
    assert not [p for p in found if "gift nature" in p], "ran across the comma"
    assert "gift for him" not in found and "t shirt" not in found
    assert all(len(p.split()) <= 4 for p in found)


def test_a_phrase_does_not_stutter_where_a_comma_is_missing():
    found = title_phrases("Ceramic Mug Ceramic Coffee Cup")
    assert {"ceramic mug", "ceramic coffee cup", "mug ceramic"} <= found
    assert not [p for p in found if p.split().count("ceramic") > 1]
    assert "ho ho ho" in title_phrases("Ho Ho Ho Christmas Sweater")


def test_a_bare_number_is_not_a_phrase_but_a_number_in_one_is():
    found = title_phrases("Class of 2026 Shirt, 11oz Mug")
    assert "2026" not in found and "2026 shirt" in found and "11oz mug" in found
    assert "11oz" in found


def test_the_report_is_mostly_whole_multi_word_phrases():
    report = _report(_market_listings())
    phrases = [p for p, _ in report.phrases]
    singles = [p for p in phrases if " " not in p]
    assert len(singles) <= SINGLE_WORD_ROWS and len(phrases) <= PHRASE_ROWS
    assert len(phrases) - len(singles) > len(singles), phrases
    # Whole phrases, of up to four words, spelled as sellers spell them.
    for whole in ("unisex comfort colors tee", "outdoor adventure shirt", "vintage graphic tee",
                  "retro sunset t-shirt", "mother's day gift", "11oz coffee mug",
                  "st patrick's day shirt", "nature lover gift", "camping tee"):
        assert whole in phrases, whole
    for split in ("t shirt", "11 oz", "mother s", "oz coffee mug"):
        assert not [p for p in phrases if f" {split} " in f" {p} "], split
    # Never across a separator: every phrase is a run of words of one pool phrase.
    runs = set()
    for text in _HEADS + _POOL:
        for words in title_segments(text):
            runs |= {" ".join(words[i:j]) for i in range(len(words))
                     for j in range(i + 1, len(words) + 1)}
    assert [p for p in phrases if p not in runs] == []


def test_a_piece_that_never_appears_alone_is_left_out():
    listings = [_listing(i, "Vintage Graphic Tee, Retro Shirt") for i in range(5)]
    listings += [_listing(5 + i, "Graphic Tee | Cute Gift") for i in range(3)]
    phrases = dict(_report(listings).phrases)
    assert phrases["vintage graphic tee"] == 5
    assert phrases["graphic tee"] == 8, "used on its own too, so it stays"
    assert "vintage graphic" not in phrases, "only ever part of vintage graphic tee"
    assert phrases["vintage"] == 5, "single words stay: they show product and audience"


def test_rows_are_ordered_by_count_then_alphabetically():
    listings = _market_listings()
    report = _report(listings)
    for rows in (report.phrases, report.tags):
        assert rows == sorted(rows, key=lambda row: (-row[1], row[0]))
    shuffled = list(listings)
    random.Random(3).shuffle(shuffled)
    again = _report(shuffled)
    assert again.phrases == report.phrases and again.tags == report.tags


def test_the_report_is_the_same_under_any_hash_seed():
    # Before, tied counts came out in set order, which changes with PYTHONHASHSEED.
    script = (
        "import json, sys; sys.path.insert(0, sys.argv[1]); import test_seo as t; "
        "r = t._report(t._market_listings()); print(json.dumps([r.tags, r.phrases]))"
    )
    outputs = set()
    for seed in ("0", "1", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": seed,
               "PYTHONPATH": os.pathsep.join([str(ROOT), os.environ.get("PYTHONPATH", "")])}
        done = subprocess.run([sys.executable, "-c", script, str(ROOT / "tests")], env=env,
                              capture_output=True, text=True, timeout=120, check=True)
        outputs.add(done.stdout)
    assert len(outputs) == 1


def test_tags_are_counted_whatever_their_spacing_or_case():
    listings = [_listing(0, "Mug", ["Gift  For Her"]), _listing(1, "Mug", ["gift for her "])]
    assert dict(_report(listings).tags)["gift for her"] == 2


def test_generate_builds_on_the_new_report():
    report = _report(_market_listings())
    seed = seeds.derive(Path("lemon-summer-vibes.png"))
    title = generate.build_title(seed, report, product_hint="Retro Sunset Shirt")
    assert title.startswith("Lemon Summer Vibes Shirt, ")
    assert len(title) <= MAX_TITLE_LEN
    assert all(len(part.split()) >= 2 for part in title.split(", ")), title
    assert "Oz" not in title.split() and "T Shirt" not in title
    tags = generate.build_tags(seed, report, product_hint="Retro Sunset Shirt")
    assert len(tags) == MAX_TAGS and validate_tags(tags) == []


# --- the research cache: a report of the old shape is fetched again -------------------


def _old_research_file(key, value):
    """What etsyotomasyon wrote before research v2: the plain key, hashed, as the file name."""
    path = cache_dir() / "research" / f"{hashlib.sha256(key.encode()).hexdigest()[:20]}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"key": key, "stored_at": time.time(), "value": value}),
                    encoding="utf-8")
    return path


def _old_report(keyword):
    return {"keyword": keyword, "sampled": 2, "tags": [["old tag", 2]],
            "phrases": [["shirt hiking shirt", 2], ["t shirt", 2]], "price_min": None,
            "price_median": None, "price_max": None, "currency": "", "median_favorers": None,
            "top_listings": [], "price_sample": 0, "currency_count": 0}


def test_an_old_research_cache_entry_is_not_reused():
    old = _old_research_file("retro sunset|200", _old_report("retro sunset"))
    assert cache.load("retro sunset|200") is None
    cache.store("retro sunset|200", {"fresh": True})
    assert cache.load("retro sunset|200") == {"fresh": True}
    assert old.exists() and json.loads(old.read_text(encoding="utf-8"))["key"] == "retro sunset|200"
    assert len(list(old.parent.glob("*.json"))) == 2, "the new entry lives in its own file"


def test_other_cache_namespaces_keep_their_files():
    path = cache_dir() / "taxonomy" / (
        hashlib.sha256(b"seller-taxonomy-v1").hexdigest()[:20] + ".json")
    cache.store("seller-taxonomy-v1", {"1": "Art"}, namespace="taxonomy")
    assert path.exists()
    assert cache.load("seller-taxonomy-v1", namespace="taxonomy") == {"1": "Art"}


def test_the_drop_pipeline_researches_again_then_reuses_the_new_report():
    from etsyotomasyon.drop import pipeline

    _old_research_file("retro sunset|200", _old_report("retro sunset"))
    client = _Search(_market_listings())
    fresh, cached = pipeline._research_concept(client, "retro sunset", sample=200,
                                               use_cache=True)
    assert cached is False and fresh.sampled == 200
    again, cached = pipeline._research_concept(None, "retro sunset", sample=200,
                                               use_cache=True)
    assert cached is True
    assert [tuple(row) for row in again.phrases] == fresh.phrases
    assert [tuple(row) for row in again.tags] == fresh.tags
    seed = seeds.derive(Path("retro-sunset.png"))
    assert generate.build_title(seed, again) == generate.build_title(seed, fresh)
    assert generate.build_tags(seed, again) == generate.build_tags(seed, fresh)


def test_the_seo_page_researches_again_instead_of_showing_an_old_report(web):
    from web_helpers import use_fake_etsy

    rows = [_listing(i, "Stoneware Mug, Coffee Lover Gift", ["coffee lover gift", "pottery"])
            for i in range(300)]

    def search(request):
        offset = int(request.url.params.get("offset", 0))
        limit = int(request.url.params.get("limit", 25))
        return {"count": len(rows), "results": rows[offset:offset + limit]}

    fake = use_fake_etsy(web, connected=False)
    fake.add("GET", "/listings/active", search)
    _old_research_file("stoneware mug|300", _old_report("stoneware mug"))
    data = web.client.get("/api/seo/research", params={"keyword": "stoneware mug"}).json()
    assert data["cached"] is False and data["sampled"] == 300
    assert [t["tag"] for t in data["tags"]] == ["coffee lover gift", "pottery"]
    again = web.client.get("/api/seo/research", params={"keyword": "stoneware mug"}).json()
    assert again["cached"] is True and again["tags"] == data["tags"]


def test_photo_checks_need_the_listings_images_and_name_what_is_thin():
    from etsyotomasyon import seo
    base = {"listing_id": 1, "title": "Leopard Jungle Wallpaper, Peel and Stick", "tags": ["a b"],
            "description": "x" * 200}
    codes = lambda listing: {i.code for i in seo.audit_listing(listing).issues}  # noqa: E731
    assert not {c for c in codes(base) if c.startswith("images.")}  # read without images: silent
    photo = {"rank": 1, "alt_text": "", "full_width": 1200, "full_height": 1600}
    few = codes({**base, "images": [photo, photo]})
    assert {"images.few", "images.alt", "images.small"} <= few
    some = codes({**base, "images": [{**photo, "alt_text": "x", "full_width": 3000, "full_height": 3000}] * 7})
    assert "images.some" in some and "images.few" not in some and "images.alt" not in some
    good = codes({**base, "images": [{**photo, "alt_text": "x", "full_width": 3000, "full_height": 3000}] * 10})
    assert not {c for c in good if c.startswith("images.")}
