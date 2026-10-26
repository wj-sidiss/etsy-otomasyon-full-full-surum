"""Round 4: one tag key everywhere tags are compared, and sizes a tag cannot hold."""

from __future__ import annotations

import httpx
import pytest
from web_helpers import ETSY_SHOP_ID, use_fake_etsy

from etsyotomasyon import seo
from etsyotomasyon.client import EtsyClient
from etsyotomasyon.drop import generate, seeds
from etsyotomasyon.listings import validate_tags

LISTINGS_PATH = f"/shops/{ETSY_SHOP_ID}/listings"


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(EtsyClient, "_backoff", staticmethod(lambda attempt: 0.0))


class _Search:
    def __init__(self, listings):
        self.listings = listings

    def search_active_listings(self, **kw):
        return iter(self.listings[: kw.get("max_items") or 999])


def _route(rows):
    def respond(request: httpx.Request):
        limit = int(request.url.params.get("limit", 25))
        offset = int(request.url.params.get("offset", 0))
        return {"count": len(rows), "results": rows[offset:offset + limit]}
    return respond


# --- a capital dotted I is the same tag as a plain i ----------------------------------------


def test_tag_key_folds_case_spacing_and_the_turkish_capital_i():
    assert seo.tag_key("İstanbul  Poster ") == "istanbul poster"
    assert seo.tag_key("İzmir print") == seo.tag_key("izmir print")
    assert "̇" not in seo.tag_key("İ")


def test_suggest_tags_knows_a_tag_the_listing_has_with_a_capital_dotted_i():
    rows = [{"listing_id": i, "title": "İzmir Print", "tags": ["İzmir print", "wall art"]}
            for i in range(10)]
    report = seo.research(_Search(rows), "izmir print", sample=10)
    assert ("izmir print", 10) in report.tags
    got = seo.suggest_tags(report, existing=["İzmir print"])
    assert "izmir print" not in got.add_now + got.needs_a_swap
    assert got.add_now == ["wall art"] and got.used_slots == 1


def test_the_seo_page_does_not_offer_a_turkish_tag_the_listing_already_has(web):
    mine = {
        "listing_id": 1000001, "title": "İstanbul Poster, Galata Kulesi Duvar Sanatı",
        "description": "x" * 200, "tags": ["İstanbul poster", "galata"], "materials": ["kağıt"],
        "state": "active", "url": "https://www.etsy.com/listing/1000001",
        "should_auto_renew": True, "images": [],
    }
    fake = use_fake_etsy(web)
    fake.add("GET", LISTINGS_PATH, _route([mine]))
    market = [{"listing_id": 2000000 + i, "title": f"İstanbul Poster {i}",
               "tags": ["İstanbul poster", "duvar sanatı"], "num_favorers": i,
               "price": {"amount": 1000, "divisor": 100, "currency_code": "TRY"}}
              for i in range(40)]
    fake.add("GET", "/listings/active", _route(market))
    web.client.get("/api/seo/audit")
    data = web.client.get("/api/seo/research",
                          params={"keyword": "istanbul poster", "listing_id": 1000001}).json()
    assert "istanbul poster" not in [row["tag"] for row in data["tags"]]
    assert "istanbul poster" not in data["add_now"] + data["needs_a_swap"]
    assert "duvar sanatı" in data["add_now"]


def test_validate_tags_calls_the_two_spellings_a_duplicate():
    problems = validate_tags(["İstanbul poster", "istanbul poster"])
    assert any("duplicate tags: istanbul poster" in p for p in problems), problems
    assert validate_tags(["İstanbul poster", "galata"]) == []


# --- a size joined by a point, a comma or a slash never reaches a title or a tag -------------


def test_a_decimal_size_is_not_split_into_the_title_and_tags():
    titles = (["Mountain Sunset 8.5x11 Wall Art Print, Nature Poster, Hiking Gift"] * 30
              + ["Mountain Sunset Print 8.5x11, Landscape Wall Art"] * 20
              + ["Retro Mountain Poster, Cabin Decor"] * 50)
    rows = [{"listing_id": i, "title": t, "tags": []} for i, t in enumerate(titles)]
    report = seo.research(_Search(rows), "mountain sunset", sample=200)
    assert ("8.5x11", 50) in report.phrases  # research keeps the size whole
    seed = seeds.Seed("mountain sunset", "mountain-sunset.png")
    hint = generate.hint_from("Custom 8.5x11 Art Print, Wall Decor", ["8.5x11 print"], "")
    title = generate.build_title(seed, report, product_hint=hint)
    tags = generate.build_tags(seed, report, product_hint=hint)
    assert "8 5x11" not in title and "5x11" not in title, title
    assert not any("5x11" in tag for tag in tags), tags
    assert title.startswith("Mountain Sunset")


def test_a_three_quarter_sleeve_phrase_is_not_split_into_3_4():
    titles = (["Baseball Mom Shirt, 3/4 Sleeve Raglan, Mid-Century Baseball Tee"] * 40
              + ["Baseball Mom Raglan, Game Day Shirt"] * 60)
    rows = [{"listing_id": i, "title": t, "tags": ["1,000 piece puzzle", "game day shirt"]}
            for i, t in enumerate(titles)]
    report = seo.research(_Search(rows), "baseball mom", sample=100)
    seed = seeds.Seed("baseball mom", "baseball-mom.png")
    hint = generate.hint_from("Baseball Shirt, Raglan Tee", ["raglan"], "")
    title = generate.build_title(seed, report, product_hint=hint)
    tags = generate.build_tags(seed, report, product_hint=hint)
    assert "3 4" not in title, title
    assert not any(tag.startswith(("3 4", "1 000")) for tag in tags), tags
    assert "game day shirt" in tags
