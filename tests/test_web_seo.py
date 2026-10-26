"""The SEO screen's API: the audit list, tag research, the fix and the CSV report."""

from __future__ import annotations

import csv
import io
import urllib.parse

import httpx
import pytest
from web_helpers import ETSY_SHOP_ID, use_fake_etsy

from etsyotomasyon.client import EtsyClient
from etsyotomasyon.web.api import seo as seo_api

LISTINGS_PATH = f"/shops/{ETSY_SHOP_ID}/listings"
DESCRIPTION = (
    "A sturdy stoneware coffee mug, glazed by hand in small batches. Holds 12 oz, safe in the "
    "dishwasher and the microwave. Every mug is a little different, which is the whole point."
)


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(EtsyClient, "_backoff", staticmethod(lambda attempt: 0.0))


def _listing(listing_id, title, tags, *, description=DESCRIPTION, materials=("stoneware",),
             state="active"):
    return {
        "listing_id": listing_id,
        "title": title,
        "description": description,
        "tags": list(tags),
        "materials": list(materials),
        "state": state,
        "url": f"https://www.etsy.com/listing/{listing_id}",
        "should_auto_renew": True,
        # Ten photos with alt text and a large main one: nothing for the photo checks to say.
        "images": [
            {"rank": n, "alt_text": "mug on a table", "full_width": 3000, "full_height": 3000,
             "url_170x135": f"https://i.example.test/{listing_id}-{n}.jpg"}
            for n in range(10, 0, -1)
        ],
    }


GOOD_TAGS = [
    "ceramic coffee mug", "stoneware mug", "handmade mug", "pottery mug", "coffee lover gift",
    "tea cup", "blue glaze mug", "kitchen gift", "rustic mug", "coffee cup", "mug for dad",
    "clay mug", "housewarming gift",
]

MUG = _listing(1000001, "Mug", ["mug", "gift", "gifts", "coffee"], description="Nice mug.",
               materials=())
FLORAL = _listing(
    1000002, "Floral Print Poster, Floral Wall Art, Floral Print",
    ["floral print", "wall art", "poster", "floral poster", "flower art", "botanical print",
     "floralprint", "boho decor", "gift"],
)
READY = _listing(
    1000003, "Handmade Ceramic Coffee Mug, Blue Glaze Stoneware Tea Cup", GOOD_TAGS,
)


def _listings_route(active, draft=()):
    def respond(request: httpx.Request):
        state = request.url.params.get("state")
        rows = list(active if state == "active" else draft)
        limit = int(request.url.params.get("limit", 25))
        offset = int(request.url.params.get("offset", 0))
        return {"count": len(rows), "results": rows[offset:offset + limit]}
    return respond


def _connected(web, active=(MUG, FLORAL, READY), draft=()):
    fake = use_fake_etsy(web)
    fake.add("GET", LISTINGS_PATH, _listings_route(active, draft))
    return fake


def _search_results(n=300):
    """A marketplace sample: tag i appears in a known share of the listings."""
    rows = []
    for i in range(n):
        tags = ["coffee lover gift"] if i < int(n * 0.62) else []
        if i < int(n * 0.48):
            tags.append("handmade mug")
        if i < int(n * 0.41):
            tags.append("pottery mug")
        if i < int(n * 0.9):
            tags.append("mug")  # the listing already has this one
        if i < int(n * 0.3):
            tags.append("a tag that is far too long for etsy")
        rows.append({"listing_id": 2000000 + i, "title": f"Stoneware mug {i}", "tags": tags,
                     "price": {"amount": 2400, "divisor": 100, "currency_code": "USD"},
                     "num_favorers": i})
    return rows


def _search_route(rows):
    def respond(request: httpx.Request):
        limit = int(request.url.params.get("limit", 25))
        offset = int(request.url.params.get("offset", 0))
        return {"count": len(rows), "results": rows[offset:offset + limit]}
    return respond


# --- setup states --------------------------------------------------------------------------


def test_audit_and_research_need_setup_first(web):
    resp = web.client.get("/api/seo/audit")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "setup_needed"
    assert resp.json()["error"]["params"] == {"step": "keys"}
    resp = web.client.get("/api/seo/research", params={"keyword": "mug"})
    assert resp.status_code == 409 and resp.json()["error"]["params"] == {"step": "keys"}


def test_audit_needs_a_connected_shop_but_research_only_the_key(web):
    fake = use_fake_etsy(web, connected=False)
    fake.add("GET", "/listings/active", _search_route(_search_results(40)))
    resp = web.client.get("/api/seo/audit")
    assert resp.status_code == 409 and resp.json()["error"]["params"] == {"step": "connect"}
    resp = web.client.get("/api/seo/research", params={"keyword": "stoneware mug"})
    assert resp.status_code == 200
    assert resp.json()["sampled"] == 40
    assert "authorization" not in fake.requests[-1].headers


def test_bad_state_is_refused(web):
    _connected(web)
    resp = web.client.get("/api/seo/audit", params={"state": "sold"})
    assert resp.status_code == 422 and resp.json()["error"]["params"] == {"field": "state"}


def test_offline(web):
    fake = _connected(web)
    fake.offline = True
    assert web.client.get("/api/seo/audit").json()["error"]["code"] == "offline"


# --- the audit ------------------------------------------------------------------------------


def test_audit_sorts_worst_first_with_numbers_for_every_issue(web):
    fake = _connected(web, draft=[_listing(1000009, "Draft", ["x"], state="draft")])
    data = web.client.get("/api/seo/audit", params={"state": "active"}).json()

    assert [i["listing_id"] for i in data["items"]] == [1000001, 1000002, 1000003]
    scores = [i["score"] for i in data["items"]]
    assert scores == sorted(scores)
    assert data["scanned"] == 3 and data["needs_fix"] == 2
    assert data["counts"] == {"active": 3, "draft": 1}
    assert data["cached"] is False and data["truncated"] is False

    mug = data["items"][0]
    issues = {i["code"]: i for i in mug["issues"]}
    assert issues["title.too_short"]["params"] == {"title_len": 3, "min": 40}
    assert issues["tags.unused"]["params"] == {"tags_used": 4, "max": 13, "free_slots": 9}
    assert issues["tags.near_duplicate"]["params"]["a"] == "gift"
    assert issues["tags.near_duplicate"]["params"]["b"] == "gifts"
    assert issues["description.thin"]["params"] == {"description_len": 9, "min": 160}
    assert "materials.missing" in issues
    # heaviest first: warnings before infos
    severities = [i["severity"] for i in mug["issues"]]
    assert severities.index("info") > max(n for n, s in enumerate(severities) if s == "warn")
    assert mug["thumb"] == "https://i.example.test/1000001-1.jpg"  # lowest rank wins
    assert mug["concept"] == "mug"
    assert mug["ready"] is False

    floral = data["items"][1]
    rep = next(i for i in floral["issues"] if i["code"] == "title.repetition")
    assert rep["params"]["words"] == ["floral"]

    ready = data["items"][2]
    assert ready["ready"] is True and ready["grade"] == "good"

    first = next(r for r in fake.requests if r.url.path.endswith(LISTINGS_PATH))
    assert first.url.params["state"] == "active"
    assert first.url.params["includes"] == "Images"
    assert first.url.params["limit"] == "100"


def test_audit_is_cached_until_a_rescan(web):
    fake = _connected(web)
    web.client.get("/api/seo/audit")
    calls = len(fake.calls)
    again = web.client.get("/api/seo/audit").json()
    assert again["cached"] is True and len(fake.calls) == calls
    fresh = web.client.get("/api/seo/audit", params={"refresh": 1}).json()
    assert fresh["cached"] is False and len(fake.calls) > calls


def test_shop_wide_issues_are_reported(web):
    same = ["wall art", "poster", "print", "decor", "boho"]
    rows = [_listing(1000100 + n, f"Boho Wall Art Poster Print {n}", same) for n in range(4)]
    _connected(web, active=rows)
    data = web.client.get("/api/seo/audit").json()
    codes = {i["code"]: i for i in data["shop_issues"]}
    assert "shop.tag_overlap" in codes
    overlap = codes["shop.tag_overlap"]["params"]
    assert overlap["listings"] == 4 and overlap["tags"] == 5
    assert {"tag": "boho", "count": 4} in overlap["top"]


# --- the fix proposal --------------------------------------------------------------------------


def test_fix_proposal_drops_exact_and_near_duplicates_keeping_the_first():
    proposal = seo_api.tag_proposal(
        ["gift", "Gifts", "GIFT", "coffee mug", "coffeemug", "tea", "x" * 21, "mug!"]
    )
    assert proposal["keep"] == ["gift", "coffee mug", "tea"]
    assert proposal["remove"] == [
        {"tag": "Gifts", "reason": "near_duplicate", "of": "gift"},
        {"tag": "GIFT", "reason": "duplicate", "of": "gift"},
        {"tag": "coffeemug", "reason": "near_duplicate", "of": "coffee mug"},
        {"tag": "x" * 21, "reason": "too_long"},
        {"tag": "mug!", "reason": "bad_chars"},
    ]
    assert proposal["free_slots"] == 10


def test_fix_proposal_never_keeps_more_than_thirteen():
    proposal = seo_api.tag_proposal([f"tag number {n}" for n in range(15)])
    assert len(proposal["keep"]) == 13 and proposal["free_slots"] == 0
    assert [r["reason"] for r in proposal["remove"]] == ["too_many", "too_many"]


def test_title_repetition_fix_drops_only_whole_repeated_phrases():
    assert seo_api.dedupe_title("Floral Print Poster, Floral Wall Art, Floral Print") == (
        "Floral Print Poster, Floral Wall Art", ["Floral Print"]
    )
    assert seo_api.dedupe_title("Boho Art - Boho Print | Boho") == ("Boho Art - Boho Print", ["Boho"])
    # A phrase with a word of its own keeps every word: nothing is cut out of it.
    for title in (
        "Cat Mom Club Tote Bag, Cute Cat Lover Canvas Carryall, Cat Mom Gift",
        "But First Coffee Mug, Funny Coffee Lover Coffee Cup, Gift",
        "Mug Mug Mug Coffee Mug",
        "Nothing repeated here",
    ):
        assert seo_api.dedupe_title(title) == (title, [])
    # A hyphen, "&" or "/" inside a phrase is no boundary.
    assert seo_api.dedupe_title("Retro Shirt, Vintage T-Shirt") == ("Retro Shirt, Vintage T-Shirt", [])
    assert seo_api.dedupe_title("Pepper Mill, Salt & Pepper") == ("Pepper Mill, Salt & Pepper", [])


def test_a_title_change_that_would_still_repeat_a_word_is_not_offered():
    # Dropping "Floral Print" still leaves "floral" three times: no half fix is offered.
    title = "Floral Print Poster, Floral Wall Art, Floral Print, Botanical Floral Decor"
    assert seo_api.dedupe_title(title)[1] == ["Floral Print"]
    proposal = seo_api.proposal({"title": title, "tags": ["floral"]}, {"title.repetition"})
    assert proposal["title"] is None and proposal["manual"][0] == "title.repetition"
    # One that ends the repetition is offered, and not also listed as manual work.
    title = "Floral Print Poster, Floral Wall Art, Floral Print"
    proposal = seo_api.proposal({"title": title, "tags": ["floral"]}, {"title.repetition"})
    assert proposal["title"]["after"] == "Floral Print Poster, Floral Wall Art"
    assert "title.repetition" not in proposal["manual"]


def test_a_title_that_repeats_inside_its_phrases_is_left_to_the_seller():
    listing = {"title": "Cat Mom Tote Bag, Cute Cat Lover Tote, Cat Mom Tote Gift", "tags": ["cat"]}
    proposal = seo_api.proposal(listing, {"title.repetition"})
    assert proposal["title"] is None
    assert "title.repetition" in proposal["manual"]


def test_audit_items_carry_the_proposal(web):
    _connected(web)
    items = {i["listing_id"]: i for i in web.client.get("/api/seo/audit").json()["items"]}
    mug = items[1000001]["fix"]
    assert mug["keep"] == ["mug", "gift", "coffee"]
    assert mug["remove"] == [{"tag": "gifts", "reason": "near_duplicate", "of": "gift"}]
    assert mug["free_slots"] == 10 and mug["materials"] is True
    assert mug["title"] is None
    assert "title.too_short" in mug["manual"] and "description.thin" in mug["manual"]
    floral = items[1000002]["fix"]
    assert floral["title"]["after"] == "Floral Print Poster, Floral Wall Art"
    assert floral["title"]["phrases"] == ["Floral Print"]
    assert "title.repetition" not in floral["manual"]
    assert {"tag": "floralprint", "reason": "near_duplicate", "of": "floral print"} in floral["remove"]
    assert items[1000003]["fix"]["remove"] == [] and items[1000003]["fix"]["free_slots"] == 0


# --- research ---------------------------------------------------------------------------------


def test_research_lists_tags_the_listing_lacks_with_their_share(web):
    fake = _connected(web)
    fake.add("GET", "/listings/active", _search_route(_search_results(300)))
    web.client.get("/api/seo/audit")  # the listing's tags come from the audit
    resp = web.client.get("/api/seo/research",
                          params={"keyword": "  Stoneware   MUG ", "listing_id": 1000001})
    assert resp.status_code == 200
    data = resp.json()
    assert data["keyword"] == "stoneware mug"
    assert data["sampled"] == 300 and data["sample"] == 300
    assert data["listing_id"] == 1000001
    assert [t["tag"] for t in data["tags"]] == ["coffee lover gift", "handmade mug", "pottery mug"]
    assert data["tags"][0] == {"tag": "coffee lover gift", "count": 186, "share": 0.62}
    assert data["free_slots"] == 9 and data["used_slots"] == 4
    assert data["add_now"] == ["coffee lover gift", "handmade mug", "pottery mug"]
    assert data["cached"] is False
    # The competitor view beyond the tags: price band, favourites, shared phrases, top five.
    market = data["market"]
    assert market["currency"] == "USD" and market["price"]["sample"] == 300
    assert market["price"]["min"] == market["price"]["median"] == market["price"]["max"] == 24.0
    assert market["median_favorers"] == 149.5
    assert [row["favorers"] for row in market["top"]] == [299, 298, 297, 296, 295]
    assert market["phrases"] and "stoneware mug" in {p["phrase"] for p in market["phrases"]}

    searches = [r for r in fake.requests if r.url.path.endswith("/listings/active")]
    assert len(searches) == 3  # 300 listings, 100 per page
    params = searches[0].url.params
    assert params["keywords"] == "stoneware mug" and params["sort_on"] == "score"
    assert params["limit"] == "100" and params["offset"] == "0"
    assert searches[-1].url.params["offset"] == "200"

    again = web.client.get("/api/seo/research", params={"keyword": "stoneware mug"}).json()
    assert again["cached"] is True
    assert len([r for r in fake.requests if r.url.path.endswith("/listings/active")]) == 3
    # without a listing nothing is excluded
    assert "mug" in [t["tag"] for t in again["tags"]]
    assert again["listing_id"] is None

    web.client.get("/api/seo/research", params={"keyword": "stoneware mug", "refresh": 1})
    assert len([r for r in fake.requests if r.url.path.endswith("/listings/active")]) == 6


def test_research_reads_an_unaudited_listing_itself(web):
    fake = _connected(web)
    fake.add("GET", "/listings/active", _search_route(_search_results(20)))
    fake.add("GET", "/listings/batch", {"count": 1, "results": [MUG]})
    data = web.client.get("/api/seo/research",
                          params={"keyword": "mug", "listing_id": 1000001}).json()
    assert data["listing_id"] == 1000001
    assert "mug" not in [t["tag"] for t in data["tags"]]
    batch = next(r for r in fake.requests if r.url.path.endswith("/listings/batch"))
    assert batch.url.params["listing_ids"] == "1000001"


def test_research_needs_a_keyword(web):
    _connected(web)
    resp = web.client.get("/api/seo/research", params={"keyword": "   "})
    assert resp.status_code == 422 and resp.json()["error"]["params"] == {"field": "keyword"}


def test_research_with_no_results(web):
    fake = _connected(web)
    fake.add("GET", "/listings/active", {"count": 0, "results": []})
    data = web.client.get("/api/seo/research", params={"keyword": "nothing like this"}).json()
    assert data["sampled"] == 0 and data["tags"] == []


# --- applying a fix ----------------------------------------------------------------------------


def _patch_route(store):
    def respond(request: httpx.Request):
        form = dict(urllib.parse.parse_qsl(request.content.decode("utf-8")))
        store.append(form)
        updated = dict(MUG)
        updated.pop("images")
        if "tags" in form:
            updated["tags"] = form["tags"].split(",")
        if "title" in form:
            updated["title"] = form["title"]
        if "materials" in form:
            updated["materials"] = form["materials"].split(",")
        return updated
    return respond


def test_fix_needs_confirmation(web):
    fake = _connected(web)
    resp = web.client.post("/api/seo/fix/1000001", json={"tags": ["mug"]})
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "confirm_required"
    assert not [c for c in fake.calls if c[0] == "PATCH"]


def test_fix_validates_before_sending(web):
    fake = _connected(web)
    cases = [
        {"tags": []},
        {"tags": 5},
        {"tags": ["mug", 7]},
        {"tags": ["mug", "MUG"]},
        {"tags": ["x" * 21]},
        {"tags": ["mug!"]},
        {"tags": [f"tag {n}" for n in range(14)]},
        {"tags": ["mug"], "title": "Mug & Cup & Saucer"},
        {"tags": ["mug"], "title": "x" * 141},
        # The character rule too, the same one the listing editor and a CSV push use.
        {"tags": ["mug"], "title": "Sunset ★ Mug"},
        {"tags": ["mug"], "title": "Coffee Mug 😀"},
        {"tags": ["mug"], "materials": ["clay, glaze!"]},
    ]
    for body in cases:
        resp = web.client.post("/api/seo/fix/1000001", json={**body, "confirm": True})
        assert resp.status_code == 422, body
        assert resp.json()["error"]["code"] == "invalid", body
    assert not [c for c in fake.calls if c[0] == "PATCH"]
    resp = web.client.post("/api/seo/fix/1000001",
                           json={"tags": ["mug"], "title": "Sunset ★ Mug", "confirm": True})
    error = resp.json()["error"]
    assert error["params"]["field"] == "title" and "does not accept" in error["message"]


def test_fix_updates_the_live_listing_and_rescores_it(web):
    fake = _connected(web)
    sent: list[dict] = []
    fake.add("PATCH", f"{LISTINGS_PATH}/1000001", _patch_route(sent))
    before = web.client.get("/api/seo/audit").json()["items"][0]
    assert before["listing_id"] == 1000001

    tags = ["mug", "gift", "coffee", "coffee lover gift", "handmade mug", "pottery mug"]
    resp = web.client.post("/api/seo/fix/1000001", json={
        "tags": tags, "title": "  Handmade  Stoneware Coffee Mug ", "materials": ["stoneware", "glaze"],
        "confirm": True,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert sent == [{
        "tags": ",".join(tags),
        "title": "Handmade Stoneware Coffee Mug",
        "materials": "stoneware,glaze",
    }]
    assert data["before"] == before["score"]
    assert data["sent"] == ["materials", "tags", "title"]
    item = data["item"]
    assert item["tags"] == tags and item["title"] == "Handmade Stoneware Coffee Mug"
    assert item["score"] > before["score"]
    assert item["thumb"] == before["thumb"]  # kept from the audit, the PATCH answer has no images
    codes = {i["code"] for i in item["issues"]}
    assert "materials.missing" not in codes and "tags.near_duplicate" not in codes

    # the cached audit now shows the fixed listing, without asking Etsy again
    calls = len(fake.calls)
    again = web.client.get("/api/seo/audit").json()
    assert len(fake.calls) == calls
    fixed = next(i for i in again["items"] if i["listing_id"] == 1000001)
    assert fixed["score"] == item["score"]


def test_fix_sends_only_tags_when_nothing_else_changes(web):
    fake = _connected(web)
    sent: list[dict] = []
    fake.add("PATCH", f"{LISTINGS_PATH}/1000001", _patch_route(sent))
    resp = web.client.post("/api/seo/fix/1000001", json={"tags": ["mug", "gift"], "confirm": True})
    assert resp.status_code == 200
    assert sent == [{"tags": "mug,gift"}]
    assert resp.json()["before"] is None  # never audited


def test_fix_refuses_when_the_listing_changed_since_the_audit(web):
    fake = _connected(web)
    sent: list[dict] = []
    fake.add("PATCH", f"{LISTINGS_PATH}/1000001", _patch_route(sent))
    web.client.get("/api/seo/audit")
    changed = {**MUG, "tags": ["mug", "edited on etsy"]}
    fake.add("GET", "/listings/batch", {"count": 1, "results": [changed]})
    resp = web.client.post("/api/seo/fix/1000001", json={"tags": ["mug", "gift"], "confirm": True})
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "seo_stale"
    assert sent == []
    # the cached audit already shows what Etsy has now
    items = {i["listing_id"]: i for i in web.client.get("/api/seo/audit").json()["items"]}
    assert items[1000001]["tags"] == ["mug", "edited on etsy"]


def test_fix_goes_ahead_when_the_listing_is_unchanged(web):
    fake = _connected(web)
    sent: list[dict] = []
    fake.add("PATCH", f"{LISTINGS_PATH}/1000001", _patch_route(sent))
    web.client.get("/api/seo/audit")
    fake.add("GET", "/listings/batch", {"count": 1, "results": [dict(MUG)]})
    resp = web.client.post("/api/seo/fix/1000001", json={"tags": ["mug", "gift"], "confirm": True})
    assert resp.status_code == 200 and sent == [{"tags": "mug,gift"}]
    batch = next(r for r in fake.requests if r.url.path.endswith("/listings/batch"))
    assert batch.url.params["listing_ids"] == "1000001"


def test_forget_drops_the_cached_audit(web):
    fake = _connected(web)
    web.client.get("/api/seo/audit")
    seo_api.forget(web.ctx)
    calls = len(fake.calls)
    assert web.client.get("/api/seo/audit").json()["cached"] is False
    assert len(fake.calls) > calls


def test_fix_reports_etsy_errors(web):
    fake = _connected(web)
    fake.error("PATCH", f"{LISTINGS_PATH}/1000001", 400, "tags are invalid")
    resp = web.client.post("/api/seo/fix/1000001", json={"tags": ["mug"], "confirm": True})
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "etsy_error"
    assert resp.json()["error"]["params"] == {"status": 400}


# --- the CSV report ---------------------------------------------------------------------------


def test_export_csv_has_the_cli_columns_worst_first(web):
    _connected(web)
    resp = web.client.get("/api/seo/export.csv", params={"state": "active"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert resp.headers["content-disposition"].startswith('attachment; filename="seo-active-')
    assert resp.content.startswith("﻿".encode())
    rows = list(csv.DictReader(io.StringIO(resp.content.decode("utf-8-sig"))))
    assert list(rows[0]) == ["listing_id", "score", "grade", "title", "url", "issue_count",
                             "issues"]
    assert [r["listing_id"] for r in rows] == ["1000001", "1000002", "1000003"]
    assert rows[0]["grade"] == "poor" and "title is only 3 chars" in rows[0]["issues"]
    assert rows[2]["issue_count"] == "0" and rows[2]["issues"] == "no issues found"


# --- Etsy's escaped text ---------------------------------------------------------------------


def _escape(text):
    """What Etsy does to a seller's text on the way out."""
    import html

    return html.escape(text, quote=True).replace("&#x27;", "&#39;")


def test_the_audit_and_the_fix_work_on_plain_text(web):
    # Before the client decoded Etsy's entities, a title with two apostrophes could not be
    # fixed ("'&' only once"), and "mother&#39;s day" failed the tag character check.
    escaped = _listing(
        1000004, _escape("Mom's Coffee Mug, Mother's Day Gift, \"Best Mom\" Cup"),
        [_escape("mother's day"), _escape("mom's mug"), "coffee mug"],
        description=_escape("Mom's favourite mug & saucer. ") + DESCRIPTION,
    )
    fake = _connected(web, active=(escaped,))
    sent: list[dict] = []

    def patch(request: httpx.Request):
        form = dict(urllib.parse.parse_qsl(request.content.decode("utf-8")))
        sent.append(form)
        answer = {k: v for k, v in escaped.items() if k != "images"}
        answer["tags"] = [_escape(t) for t in form["tags"].split(",")]
        if "title" in form:
            answer["title"] = _escape(form["title"])
        return answer

    fake.add("PATCH", f"{LISTINGS_PATH}/1000004", patch)
    item = web.client.get("/api/seo/audit").json()["items"][0]
    assert item["title"] == "Mom's Coffee Mug, Mother's Day Gift, \"Best Mom\" Cup"
    assert item["tags"] == ["mother's day", "mom's mug", "coffee mug"]
    assert not {i["code"] for i in item["issues"]} & {"tags.bad_chars", "tags.invalid"}
    tags = ["mother's day", "mom's mug", "coffee mug", "gift for mom"]
    resp = web.client.post("/api/seo/fix/1000004", json={
        "tags": tags, "title": "Mom's Coffee Mug, Mother's Day Gift", "confirm": True})
    assert resp.status_code == 200, resp.text
    assert sent == [{"tags": ",".join(tags), "title": "Mom's Coffee Mug, Mother's Day Gift"}]
    fixed = resp.json()["item"]
    assert fixed["title"] == "Mom's Coffee Mug, Mother's Day Gift" and fixed["tags"] == tags


def test_research_reads_plain_words_from_escaped_titles(web):
    fake = _connected(web)
    rows = [{"listing_id": 2000000 + i, "title": _escape("Mother's Day Mug, Mom's Gift"),
             "tags": [_escape("mother's day"), _escape("mom's gift")]} for i in range(20)]
    fake.add("GET", "/listings/active", _search_route(rows))
    data = web.client.get("/api/seo/research", params={"keyword": "mug"}).json()
    tags = [t["tag"] for t in data["tags"]]
    assert "mother's day" in tags and "mom's gift" in tags
    assert not any("39" in t or "amp" in t or "&#" in t for t in tags)


# --- the checks that compare a listing with the others, and suggestions that follow the same rules ---

SHOP_WIDE = ["peel and stick", "removable wallpaper", "self adhesive", "renter friendly",
             "temporary wallpaper", "wallpaper mural", "self adhesive wall"]


def _wallpaper(listing_id, title, own):
    return _listing(listing_id, title, [*SHOP_WIDE, *own], materials=("vinyl",))


def _wallpaper_shop():
    fig = [f"fig {n}" for n in range(6)]
    return [
        _wallpaper(1000201, "Kitchen Wallpaper | Fig Tree Botanical | Peel and Stick", fig),
        _wallpaper(1000202, "Fig Tree Wallpaper, Olive Branch Kitchen Wallpaper", fig),
        _wallpaper(1000203, "Lemon Wallpaper, Citrus Kitchen", [f"lemon {n}" for n in range(6)]),
        _wallpaper(1000204, "Moth Wallpaper, Gothic Bedroom", [f"moth {n}" for n in range(6)]),
        _wallpaper(1000205, "Mushroom Wallpaper, Forest Bedroom", [f"fungi {n}" for n in range(6)]),
    ]


def _codes(item):
    return {i["code"]: i for i in item["issues"]}


def test_the_audit_flags_shared_and_generic_tags_and_a_generic_opening(web):
    _connected(web, active=_wallpaper_shop())
    data = web.client.get("/api/seo/audit").json()
    items = {i["listing_id"]: i for i in data["items"]}

    first = _codes(items[1000201])
    # Same six tags of its own as listing 1000202, plus the seven every listing has.
    assert first["tags.shared"]["severity"] == "warn"
    assert first["tags.shared"]["params"]["other"] == 1000202
    assert first["tags.shared"]["params"]["shared"] == 13
    assert first["tags.generic"]["severity"] == "warn"
    assert first["tags.generic"]["params"]["generic"] == 7
    assert first["title.generic_opening"]["severity"] == "info"
    assert first["title.generic_opening"]["params"]["lead"] == "Kitchen Wallpaper"
    assert not items[1000201]["ready"]
    assert {"tags.shared", "tags.generic", "title.generic_opening"} <= set(
        items[1000201]["fix"]["manual"])

    # The others only share the seven every listing carries, with the lowest id first.
    third = _codes(items[1000203])
    assert third["tags.shared"]["severity"] == "info"
    assert third["tags.shared"]["params"]["other"] == 1000201
    assert third["tags.shared"]["params"]["shared"] == 7
    assert "title.generic_opening" not in third

    # What the audit scored 100 before is no longer 100 for a shop of near copies.
    assert data["average"] < 90
    assert all(item["score"] < 100 for item in items.values())


def test_a_shop_with_a_listing_of_its_own_tags_keeps_that_listing_clean(web):
    own = _listing(1000301, "Lemon Wallpaper, Citrus Kitchen Wallpaper",
                   [f"lemon tag {n}" for n in range(13)], materials=("vinyl",))
    _connected(web, active=[*_wallpaper_shop(), own])
    items = {i["listing_id"]: i for i in web.client.get("/api/seo/audit").json()["items"]}
    codes = _codes(items[1000301])
    assert "tags.shared" not in codes and "tags.generic" not in codes
    assert items[1000301]["ready"] is True


def test_research_for_a_listing_leaves_out_the_generic_tags_it_has_too_many_of(web):
    fake = _connected(web, active=_wallpaper_shop())
    rows = []
    for i in range(100):
        tags = []
        if i < 62:
            tags.append("nursery wallpaper")  # the market's most used, none of this design's words
        if i < 50:
            tags.append("lemon wallpaper")
        if i < 30:
            tags.append("citrus mural")
        if i < 20:
            tags.append("removable wallpaper")  # already on the listing
        rows.append({"listing_id": 2000000 + i, "title": f"Lemon wallpaper {i}", "tags": tags,
                     "price": {"amount": 2400, "divisor": 100, "currency_code": "USD"},
                     "num_favorers": i})
    fake.add("GET", "/listings/active", _search_route(rows))
    web.client.get("/api/seo/audit")
    data = web.client.get("/api/seo/research",
                          params={"keyword": "lemon wallpaper", "listing_id": 1000203}).json()
    # Seven of its 13 tags are the shop's already: "nursery wallpaper" would be one more.
    assert [t["tag"] for t in data["tags"]] == ["lemon wallpaper", "citrus mural"]
    assert "nursery wallpaper" not in data["add_now"] + data["needs_a_swap"]
    # All thirteen slots are taken, so both are offered as swaps, not as additions.
    assert data["add_now"] == [] and data["needs_a_swap"] == ["lemon wallpaper", "citrus mural"]
    # Without a listing it is plain market research again: every tag, by use.
    plain = web.client.get("/api/seo/research", params={"keyword": "lemon wallpaper"}).json()
    assert [t["tag"] for t in plain["tags"]][:2] == ["nursery wallpaper", "lemon wallpaper"]


def test_a_fix_rescores_the_listing_against_the_shop_it_sits_in(web):
    fake = _connected(web, active=_wallpaper_shop())
    sent: list[dict] = []

    def patch(request: httpx.Request):
        form = dict(urllib.parse.parse_qsl(request.content.decode("utf-8")))
        sent.append(form)
        row = dict(_wallpaper_shop()[0])
        row.pop("images")
        row["tags"] = form["tags"].split(",")
        return row

    fake.add("PATCH", f"{LISTINGS_PATH}/1000201", patch)
    audit = web.client.get("/api/seo/audit").json()
    before = next(i for i in audit["items"] if i["listing_id"] == 1000201)
    assert "tags.shared" in _codes(before)

    tags = [f"unique fig {n}" for n in range(13)]
    resp = web.client.post("/api/seo/fix/1000201", json={"tags": tags, "confirm": True})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["before"] == before["score"], "scored against the shop it was audited in"
    codes = _codes(data["item"])
    assert "tags.shared" not in codes and "tags.generic" not in codes
    assert data["item"]["score"] > before["score"]
    # The other listing is no longer sharing anything with it.
    again = {i["listing_id"]: i for i in web.client.get("/api/seo/audit").json()["items"]}
    assert _codes(again[1000202])["tags.shared"]["params"]["other"] != 1000201


def test_the_csv_report_scores_listings_with_the_shop_around_them(web):
    _connected(web, active=_wallpaper_shop())
    text = web.client.get("/api/seo/export.csv").content.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    first = next(r for r in rows if r["listing_id"] == "1000201")
    assert "1000202" in first["issues"] and int(first["score"]) < 100


def test_every_issue_the_audit_can_raise_has_a_sentence_in_both_languages():
    import json
    import re
    from pathlib import Path

    from etsyotomasyon import seo as seo_module

    codes = set(re.findall(r'Issue\(\s*"([a-z_]+\.[a-z_]+)"',
                           Path(seo_module.__file__).read_text(encoding="utf-8")))
    assert {"tags.shared", "tags.generic", "title.generic_opening", "title.missing"} <= codes
    strings = json.loads((Path(seo_api.__file__).parents[1] / "static" / "i18n" / "seo.json")
                         .read_text(encoding="utf-8"))
    for code in sorted(codes):
        if code.startswith("shop."):
            continue
        for lang in ("tr", "en"):
            assert f"issue.{code}" in strings[lang], f"{code} has no {lang} sentence"
    for lang in ("tr", "en"):
        for key in ("issue.tags.shared", "issue.tags.generic"):
            assert "{other}" in strings[lang][key] or "{generic}" in strings[lang][key]
