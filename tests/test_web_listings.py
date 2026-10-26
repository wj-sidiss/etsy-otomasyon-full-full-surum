"""İlanlar and the listing detail page: /api/listings*.

Everything runs against the FakeEtsy transport; all data is invented (ExampleShop,
shop 12345678, listing ids from 1000001).
"""

from __future__ import annotations

import csv
import io
import json
import urllib.parse

import httpx
import pytest
from PIL import Image
from web_helpers import ETSY_SHOP_ID, read_events, use_fake_etsy, wait_for_job

from etsyotomasyon import seo
from etsyotomasyon.web.api import listings as listings_api

LISTINGS_PATH = f"/shops/{ETSY_SHOP_ID}/listings"
TAGS = [
    "retro sunset shirt", "mountain tshirt", "hiking shirt", "nature lover gift", "camping tee",
    "outdoor shirt", "vintage mountain", "adventure shirt", "hiker gift", "national park tee",
    "sunset tshirt", "gift for hiker", "wanderlust shirt",
]
DESCRIPTION = (
    "Retro Mountain Sunset Shirt, printed to order on a soft cotton tee. Made for hikers, "
    "campers and anyone who loves a sunset over the mountains. Wash cold, inside out."
)
TAXONOMY = [
    {"id": 1, "name": "Clothing", "children": [
        {"id": 11, "name": "Tops & Tees", "children": [{"id": 482, "name": "T-shirts", "children": []}]},
    ]},
    {"id": 2, "name": "Home & Living", "children": [{"id": 1633, "name": "Mugs", "children": []}]},
]


@pytest.fixture(autouse=True)
def fresh_cache():
    """The listing and taxonomy caches are module-wide: start every test empty."""
    listings_api.invalidate()
    listings_api._taxonomy.update(map=None, failed_at=0.0)
    listings_api._imports.clear()
    yield
    listings_api.invalidate()
    listings_api._taxonomy.update(map=None, failed_at=0.0)


def make(i: int, state: str = "draft", **fields):
    listing_id = 1000000 + i
    listing = {
        "listing_id": listing_id,
        "shop_id": ETSY_SHOP_ID,
        "title": f"Retro Mountain Sunset Shirt {i}, Vintage Hiking T-Shirt, Nature Lover Gift",
        "description": DESCRIPTION,
        "state": state,
        "tags": list(TAGS),
        "materials": ["cotton"],
        "taxonomy_id": 482,
        "price": {"amount": 2490, "divisor": 100, "currency_code": "USD"},
        "updated_timestamp": 1_700_000_000 + i,
        "url": f"https://www.etsy.com/listing/{listing_id}/example",
        "should_auto_renew": True,
        "images": [{
            "listing_id": listing_id, "listing_image_id": listing_id * 10 + 1, "rank": 1,
            "url_75x75": f"https://img.example.test/{listing_id}_75.jpg",
            "url_170x135": f"https://img.example.test/{listing_id}_170.jpg",
            "url_570xN": f"https://img.example.test/{listing_id}_570.jpg",
            "url_fullxfull": f"https://img.example.test/{listing_id}_full.jpg",
            "full_width": 2000, "full_height": 2000, "alt_text": "tshirt white",
        }],
    }
    listing.update(fields)
    return listing


class Shop:
    """Invented listings behind the fake Etsy, with the endpoints the page uses."""

    def __init__(self, web, listings):
        self.fake = use_fake_etsy(web)
        self.listings = listings
        self.by_id = {x["listing_id"]: x for x in listings}
        self.fake.add("GET", LISTINGS_PATH, self.shop_listings)
        self.fake.add("GET", "/seller-taxonomy/nodes", {"count": 2, "results": TAXONOMY})
        self.fake.add("GET", "/listings/batch", self.batch)
        for listing_id in self.by_id:
            self.fake.add("GET", f"/listings/{listing_id}/images", self.images(listing_id))
            self.fake.add("PATCH", f"{LISTINGS_PATH}/{listing_id}", self.patch(listing_id))

    def shop_listings(self, request: httpx.Request):
        q = dict(request.url.params)
        rows = [x for x in self.listings if x["state"] == q.get("state", "active")]
        offset, limit = int(q.get("offset", 0)), int(q.get("limit", 25))
        page = rows[offset:offset + limit]
        if "Images" not in q.get("includes", ""):
            page = [{k: v for k, v in x.items() if k != "images"} for x in page]
        return {"count": len(rows), "results": page}

    def batch(self, request: httpx.Request):
        return {"count": 0, "results": []}  # as if Etsy left drafts out

    def images(self, listing_id):
        return lambda _req: {"count": 1, "results": self.by_id[listing_id]["images"]}

    def patch(self, listing_id):
        def handler(request: httpx.Request):
            form = dict(urllib.parse.parse_qsl(request.content.decode()))
            item = self.by_id[listing_id]
            for key, value in form.items():
                item[key] = value.split(",") if key == "tags" else value
            return {k: v for k, v in item.items() if k != "images"}
        return handler

    def calls(self, method: str, path: str) -> int:
        return sum(1 for call in self.fake.calls if call == (method, path))

    def forms(self, listing_id):
        return [
            dict(urllib.parse.parse_qsl(r.content.decode()))
            for r in self.fake.requests
            if r.method == "PATCH" and r.url.path.endswith(f"/listings/{listing_id}")
        ]


def shop_with(web, drafts=20, active=5, inactive=1):
    listings = [make(i) for i in range(1, drafts + 1)]
    listings += [make(100 + i, "active") for i in range(1, active + 1)]
    listings += [make(200 + i, "inactive") for i in range(1, inactive + 1)]
    return Shop(web, listings)


def history(web, entries):
    root = web.ctx.workspace().root
    (root / "upload-history.json").write_text(json.dumps({str(ETSY_SHOP_ID): entries}), encoding="utf-8")
    return root


# --- the table ---------------------------------------------------------------------------


def test_needs_setup(web):
    resp = web.client.get("/api/listings")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "setup_needed"
    assert resp.json()["error"]["params"] == {"step": "keys"}
    use_fake_etsy(web, connected=False)
    resp = web.client.get("/api/listings/1000001")
    assert resp.status_code == 409 and resp.json()["error"]["params"] == {"step": "connect"}


def test_tabs_counts_and_pages(web):
    shop = shop_with(web, drafts=20, active=5, inactive=1)
    data = web.client.get("/api/listings", params={"tab": "draft", "per_page": 8}).json()
    assert data["counts"] == {"draft": 20, "active": 5, "inactive": 1, "sold_out": 0, "expired": 0,
                              "all": 26, "new_drafts": 0}
    assert (data["total"], data["pages"], data["page"], len(data["items"])) == (20, 3, 1, 8)
    assert (data["start"], data["end"]) == (1, 8)
    assert len(data["ids"]) == 20 and data["currency"] == "USD"
    # The other states were counted with one limit=1 request each, not loaded.
    counted = [dict(r.url.params) for r in shop.fake.requests if r.url.path.endswith(LISTINGS_PATH)]
    assert {"state": "active", "limit": "1", "offset": "0"} in counted
    last = web.client.get("/api/listings", params={"tab": "draft", "per_page": 8, "page": 3}).json()
    assert (last["page"], last["start"], last["end"], len(last["items"])) == (3, 17, 20, 4)
    beyond = web.client.get("/api/listings", params={"tab": "draft", "per_page": 8, "page": 9}).json()
    assert beyond["page"] == 3
    everything = web.client.get("/api/listings", params={"tab": "all", "per_page": 100}).json()
    assert everything["total"] == 26
    assert {r["state"] for r in everything["items"]} == {"draft", "active", "inactive"}
    active = web.client.get("/api/listings", params={"tab": "active"}).json()
    assert {r["state"] for r in active["items"]} == {"active"} and active["total"] == 5


def test_only_the_latest_runs_drafts_are_new(web):
    # Two runs: drafts 1-3 from an older one, 4-5 from the latest (its own review.csv).
    # Draft 5 was published since, and draft 6 was made by hand.
    shop = shop_with(web, drafts=6, active=1, inactive=0)
    shop.by_id[1000005]["state"] = "active"
    old = "C:/Etsy Studio/3-DRAFTS/2026-09-01_1000/review.csv"
    new = "C:/Etsy Studio/3-DRAFTS/2026-09-20_0900/review.csv"
    history(web, {
        **{f"old-{n}.png": {"status": "ok", "listing_id": 1000000 + n, "review_csv": old}
           for n in (1, 2, 3)},
        "failed.png": {"status": "error", "listing_id": None, "review_csv": new},
        **{f"new-{n}.png": {"status": "ok", "listing_id": 1000000 + n, "review_csv": new}
           for n in (4, 5)},
    })
    for tab in ("draft", "active", "all"):
        data = web.client.get("/api/listings", params={"tab": tab}).json()
        assert data["counts"]["new_drafts"] <= data["counts"]["draft"], tab
    data = web.client.get("/api/listings", params={"tab": "draft"}).json()
    assert (data["counts"]["draft"], data["counts"]["new_drafts"]) == (5, 1)
    history(web, {})
    listings_api.invalidate()
    assert web.client.get("/api/listings").json()["counts"]["new_drafts"] == 0


def test_bad_parameters_are_refused(web):
    shop_with(web)
    for params in ({"tab": "sold"}, {"sort": "random"}, {"seo": "great"}, {"page": "x"},
                   {"per_page": "500"}):
        resp = web.client.get("/api/listings", params=params)
        assert resp.status_code == 422, params
        assert resp.json()["error"]["code"] == "invalid"


def test_row_fields_seo_type_and_source(web):
    listings = [
        make(1),
        make(2, tags=["coffee mug", "funny mug"], taxonomy_id=1633,
             title="But First Coffee Mug, Funny Coffee Lover Gift, 11oz Ceramic Mug"),
        make(3, taxonomy_id=999999, title="Wildflower Botanical Print, Cottagecore Wall Art Poster"),
    ]
    shop = Shop(web, listings)
    history(web, {"retro-mountain-sunset.png": {"status": "ok", "listing_id": 1000001},
                  "but-first-coffee.png": {"status": "ok", "listing_id": 1000002}})
    items = {r["id"]: r for r in web.client.get("/api/listings", params={"per_page": 10}).json()["items"]}
    first = items[1000001]
    assert first["seo"] == seo.audit_listing(shop.by_id[1000001]).score
    assert first["tags"] == 13 and first["price"] == 24.9 and first["currency"] == "USD"
    assert first["type"] == "tshirt" and first["type_name"] == "T-shirts"
    assert first["source"] == "retro-mountain-sunset.png"
    assert first["thumb"] == "https://img.example.test/1000001_170.jpg"
    assert first["images"] == 1 and first["updated"] == 1_700_000_001
    mug = items[1000002]
    assert (mug["type"], mug["type_name"], mug["tags"]) == ("mug", "Mugs", 2)
    assert mug["seo"] < first["seo"]  # 2 of 13 tags used
    assert mug["source"] == "but-first-coffee.png"
    # Unknown taxonomy: the title still tells a poster.
    assert (items[1000003]["type"], items[1000003]["source"]) == ("poster", None)
    assert "_tags" not in first


def test_search_sort_and_filters(web):
    listings = [
        make(1, title="Retro Mountain Sunset Shirt, Hiking Tee", updated_timestamp=10),
        make(2, title="Açık Hava Tişörtü, Kamp Hediyesi", updated_timestamp=30,
             price={"amount": 1500, "divisor": 100, "currency_code": "USD"}),
        make(3, title="But First Coffee Mug, Coffee Lover Gift", taxonomy_id=1633, tags=[],
             description="", updated_timestamp=20,
             price={"amount": 1850, "divisor": 100, "currency_code": "USD"}),
    ]
    Shop(web, listings)
    history(web, {"ocean-waves.png": {"status": "ok", "listing_id": 1000001}})

    def ids(**params):
        return [r["id"] for r in web.client.get("/api/listings", params=params).json()["items"]]

    assert ids() == [1000002, 1000003, 1000001]  # most recently updated first
    assert ids(q="COFFEE") == [1000003]
    assert ids(q="camping tee") == [1000002, 1000001]  # a tag
    assert ids(q="acik hava") == [1000002]  # Turkish letters fold
    assert ids(q="ocean-waves") == [1000001]  # the source file name
    assert ids(q="1000003") == [1000003]
    assert ids(sort="title") == [1000002, 1000003, 1000001]
    assert ids(sort="price") == [1000002, 1000003, 1000001]
    by_seo = web.client.get("/api/listings", params={"sort": "seo"}).json()["items"]
    assert [r["seo"] for r in by_seo] == sorted(r["seo"] for r in by_seo)
    assert ids(type="mug") == [1000003]
    low = {r["id"] for r in by_seo if r["seo"] < 60}
    assert low and set(ids(seo="low")) == low
    assert set(ids(seo="high")) == {r["id"] for r in by_seo if r["seo"] >= 80}
    data = web.client.get("/api/listings", params={"type": "mug"}).json()
    assert {t["id"]: t["count"] for t in data["types"]} == {"tshirt": 2, "mug": 1}


def test_listings_are_cached_and_refreshed(web):
    shop = shop_with(web, drafts=3, active=1, inactive=0)
    web.client.get("/api/listings")
    before = shop.calls("GET", LISTINGS_PATH)
    web.client.get("/api/listings", params={"sort": "title"})
    assert shop.calls("GET", LISTINGS_PATH) == before  # served from the cache
    web.client.get("/api/listings", params={"refresh": 1})
    assert shop.calls("GET", LISTINGS_PATH) > before
    # A finished Tasarım Yükle run means new drafts: the cache is dropped.
    shop.listings.append(make(50))
    shop.by_id[1000050] = shop.listings[-1]
    job = web.ctx.jobs.start("designs", "designs:job.title", lambda job: None)
    assert job.wait(5)
    assert web.client.get("/api/listings").json()["counts"]["draft"] == 4


def test_offline_and_etsy_errors(web):
    shop = shop_with(web)
    shop.fake.offline = True
    assert web.client.get("/api/listings").json()["error"]["code"] == "offline"
    shop.fake.offline = False
    shop.fake.error("GET", LISTINGS_PATH, 500, "boom")
    resp = web.client.get("/api/listings")
    assert resp.status_code == 502 and resp.json()["error"]["code"] == "etsy_error"
    assert resp.json()["error"]["params"] == {"status": 500}


# --- the detail page ------------------------------------------------------------------------------


def test_detail_of_a_draft(web):
    shop = shop_with(web, drafts=2, active=1)
    root = history(web, {"retro-mountain-sunset.png": {"status": "ok", "listing_id": 1000001}})
    Image.new("RGBA", (64, 64), (0, 0, 0, 0)).save(root / "2-PRODUCTS" / "retro-mountain-sunset.png")
    data = web.client.get("/api/listings/1000001").json()
    listing = data["listing"]
    assert listing["id"] == 1000001 and listing["state"] == "draft"
    assert listing["tags"] == TAGS and listing["description"] == DESCRIPTION
    assert (listing["price"], listing["currency"], listing["type"]) == (24.9, "USD", "tshirt")
    assert data["category"] == {"path": "Clothing > Tops & Tees > T-shirts", "short": "Tops & Tees › T-shirts"}
    assert data["etsy_url"] == "https://www.etsy.com/your/shops/me/listing-editor/edit/1000001"
    assert data["limits"] == {"title": 140, "tags": 13, "tag_len": 20, "images": 20}
    image = data["images"][0]
    assert image["url"].endswith("_570.jpg") and image["full"].endswith("_full.jpg")
    assert (image["type"], image["color"], image["color_key"]) == ("tshirt", "Beyaz", "beyaz")
    assert data["audit"]["score"] == seo.audit_listing(shop.by_id[1000001]).score
    source = data["source"]
    assert source["name"] == "retro-mountain-sunset.png" and source["exists"] is True
    assert source["rel"] == "2-PRODUCTS/retro-mountain-sunset.png" and source["transparent"] is True
    assert ("GET", "/listings/1000001/images") in shop.fake.calls


def test_detail_of_an_active_listing_and_a_missing_one(web):
    shop_with(web, drafts=1, active=1)
    data = web.client.get("/api/listings/1000101").json()
    assert data["listing"]["state"] == "active"
    assert data["etsy_url"] == "https://www.etsy.com/listing/1000101/example"
    assert data["source"] is None
    resp = web.client.get("/api/listings/1009999")
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "not_found"


def test_source_folder_and_missing_file(web):
    shop_with(web, drafts=2, active=0, inactive=0)
    root = history(web, {"camp-set": {"status": "ok", "listing_id": 1000001},
                         "gone.png": {"status": "ok", "listing_id": 1000002}})
    folder = root / "2-PRODUCTS" / "camp-set"
    folder.mkdir()
    for name in ("01.jpg", "02.jpg"):
        Image.new("RGB", (32, 32), "white").save(folder / name)
    source = web.client.get("/api/listings/1000001").json()["source"]
    assert (source["kind"], source["files"], source["rel"]) == ("folder", 2, "2-PRODUCTS/camp-set/01.jpg")
    gone = web.client.get("/api/listings/1000002").json()["source"]
    assert (gone["exists"], gone["rel"]) == (False, None)


def test_source_names_read_the_history_through_its_shared_reader(web, monkeypatch):
    # automation.read_history holds the lock a Tasarım Yükle run writes under.
    from etsyotomasyon.drop import automation

    shop_with(web, drafts=2, active=0, inactive=0)
    root = history(web, {"camp-set": {"status": "ok", "listing_id": 1000001}})
    calls = []
    real = automation.read_history

    def spy(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(automation, "read_history", spy)
    items = web.client.get("/api/listings", params={"tab": "draft"}).json()["items"]
    assert {r["id"]: r["source"] for r in items}[1000001] == "camp-set"
    assert root in calls
    (root / "upload-history.json").write_text("{not json", encoding="utf-8")
    items = web.client.get("/api/listings", params={"tab": "draft", "refresh": 1}).json()["items"]
    assert all(r["source"] is None for r in items)


def test_edit_a_draft(web):
    shop = shop_with(web, drafts=2, active=0, inactive=0)
    web.client.get("/api/listings")
    new_tags = TAGS[:12] + ["gift for her"]
    resp = web.client.patch("/api/listings/1000001", json={
        "title": "  Retro   Mountain Sunset Shirt, Hiking Tee  ", "tags": new_tags,
        "description": "A new description.",
    })
    assert resp.status_code == 200, resp.text
    form = shop.forms(1000001)[-1]
    assert form == {"title": "Retro Mountain Sunset Shirt, Hiking Tee", "tags": ",".join(new_tags),
                    "description": "A new description."}
    assert resp.json()["listing"]["title"] == "Retro Mountain Sunset Shirt, Hiking Tee"
    # The table shows the edit without asking Etsy again.
    calls = shop.calls("GET", LISTINGS_PATH)
    row = next(r for r in web.client.get("/api/listings").json()["items"] if r["id"] == 1000001)
    assert row["title"] == "Retro Mountain Sunset Shirt, Hiking Tee"
    assert shop.calls("GET", LISTINGS_PATH) == calls


@pytest.mark.parametrize("body, code", [
    ({"title": "x" * 141}, "invalid"),
    ({"title": "Cat & Dog & Bird Tote"}, "invalid"),
    ({"title": "Sunset Shirt \U0001f305"}, "invalid"),
    ({"title": "   "}, "title_empty"),
    ({"tags": [f"tag {i}" for i in range(14)]}, "invalid"),
    ({"tags": ["this tag is far too long"]}, "invalid"),
    ({"tags": ["bad|tag"]}, "invalid"),
    ({"tags": ["same", "Same"]}, "invalid"),
    ({"tags": []}, "tags_empty"),
    ({"tags": "not a list"}, "invalid"),
    ({"description": ""}, "description_empty"),
    ({}, "invalid"),
])
def test_edit_is_validated_before_etsy(web, body, code):
    shop = shop_with(web, drafts=1, active=0, inactive=0)
    resp = web.client.patch("/api/listings/1000001", json=body)
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == code
    assert not shop.forms(1000001)


def test_editing_a_live_listing_needs_confirm(web):
    shop = shop_with(web, drafts=0, active=1, inactive=0)
    resp = web.client.patch("/api/listings/1000101", json={"title": "Retro Mountain Sunset Shirt"})
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "confirm_required"
    assert not shop.forms(1000101)
    resp = web.client.patch("/api/listings/1000101", json={"title": "Retro Mountain Sunset Shirt",
                                                          "confirm": True})
    assert resp.status_code == 200
    assert shop.forms(1000101) == [{"title": "Retro Mountain Sunset Shirt"}]


def test_title_rules():
    assert listings_api.title_problems("Retro Sunset Shirt, Hiking Tee: 100% Cotton") == []
    assert listings_api.title_problems("Çiçekli Kupa ™ – Hediye") == []
    assert len(listings_api.title_problems("a" * 141)) == 1
    assert listings_api.title_problems("50% off, 100% cotton")[0].endswith("Etsy allows it once")
    assert "does not accept" in listings_api.title_problems("Sunset ★ Shirt")[0]


def test_title_rules_are_the_csv_push_rule():
    # One rule for every screen: the listing editor, the SEO fix and a CSV push.
    from etsyotomasyon import listings as listings_mod

    for title in ("Sunset ★ Shirt", "Salt & Pepper & Co", "a: b: c", "Plain Mug, Gift",
                  "Çiçekli Kupa ™ – Hediye", "Mug 😀"):
        assert listings_api.title_problems(title) == listings_mod.title_problems(title)
    assert listings_api.title_problems("a" * 141) == ["title is 141 chars, max 140"]


# --- publishing -------------------------------------------------------------------------------------


def test_publish_one(web):
    shop = shop_with(web, drafts=2, active=1, inactive=0)
    resp = web.client.post("/api/listings/1000001/publish", json={})
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "confirm_required"
    assert not shop.forms(1000001)
    resp = web.client.post("/api/listings/1000001/publish", json={"confirm": True})
    assert resp.status_code == 200, resp.text
    assert shop.forms(1000001) == [{"state": "active"}]
    assert resp.json()["listing"]["state"] == "active"
    # The tabs are recounted: one draft less.
    counts = web.client.get("/api/listings").json()["counts"]
    assert (counts["draft"], counts["active"]) == (1, 2)
    already = web.client.post("/api/listings/1000101/publish", json={"confirm": True})
    assert already.status_code == 409 and already.json()["error"]["code"] == "already_active"


def test_publish_one_refused_by_etsy(web):
    shop = shop_with(web, drafts=1, active=0, inactive=0)
    shop.fake.error("PATCH", f"{LISTINGS_PATH}/1000001", 400,
                    "A shipping profile is required to activate this listing.")
    resp = web.client.post("/api/listings/1000001/publish", json={"confirm": True})
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "publish_refused"
    assert error["params"]["reason"] == "A shipping profile is required to activate this listing."
    assert error["params"]["status"] == 400


def test_publish_many_as_a_job(web):
    shop = shop_with(web, drafts=3, active=1, inactive=0)
    web.client.get("/api/listings", params={"tab": "all"})  # the page has seen the states
    shop.fake.error("PATCH", f"{LISTINGS_PATH}/1000002", 400, "A shipping profile is required.")
    ids = [1000001, 1000002, 1000101]
    events = read_events(
        web,
        lambda evs: any(t == "job" and d["kind"] == "publish" and d["status"] == "done" for t, d in evs),
        after_connect=lambda: web.client.post("/api/listings/publish", json={"ids": ids, "confirm": True}),
    )
    job_id = next(d["id"] for t, d in events if t == "job" and d["kind"] == "publish")
    job = wait_for_job(web, job_id)
    result = job["result"]
    assert (result["published"], result["failed"], result["skipped"]) == (1, 1, 1)
    by_id = {r["id"]: r for r in result["results"]}
    assert by_id[1000001]["status"] == "ok"
    assert by_id[1000002] == {"id": 1000002, "status": "error", "message": "A shipping profile is required.",
                              "hint": by_id[1000002]["hint"], "code": 400}
    assert by_id[1000101]["status"] == "skipped"
    assert not shop.forms(1000101)  # already live: nothing sent
    rows = [d["data"] for t, d in events if t == "job-event" and d["type"] == "row"]
    assert [r["id"] for r in rows] == ids
    assert job["state"]["ok"] == 1 and job["title_key"] == "listings:job.publish"
    note = web.client.get("/api/notifications").json()["items"][0]
    assert (note["ns"], note["key"], note["tone"]) == ("listings", "notify.publish_partial", "warning")
    assert web.client.get("/api/listings").json()["counts"]["draft"] == 2


def test_publish_many_stops_when_offline(web, monkeypatch):
    from etsyotomasyon.client import EtsyClient

    monkeypatch.setattr(EtsyClient, "_backoff", staticmethod(lambda attempt: 0.0))
    shop = shop_with(web, drafts=2, active=0, inactive=0)
    shop.fake.offline = True
    job = web.client.post("/api/listings/publish", json={"ids": [1000001, 1000002], "confirm": True}).json()
    final = wait_for_job(web, job["id"])
    assert final["status"] == "error" and final["error"]["code"] == "offline"


@pytest.mark.parametrize("body, status", [
    ({"ids": [1000001]}, 409),
    ({"ids": [], "confirm": True}, 422),
    ({"ids": ["x"], "confirm": True}, 422),
    ({"ids": [True], "confirm": True}, 422),
    ({"ids": list(range(1, 502)), "confirm": True}, 422),
])
def test_publish_many_is_validated(web, body, status):
    shop_with(web, drafts=1, active=0, inactive=0)
    assert web.client.post("/api/listings/publish", json=body).status_code == status


# --- CSV ----------------------------------------------------------------------------------------------


def read_csv(content: bytes):
    assert content.startswith(b"\xef\xbb\xbf")  # Excel needs the BOM
    return list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))


def test_export_and_template(web):
    shop_with(web, drafts=3, active=2, inactive=0)
    resp = web.client.get("/api/listings/export.csv", params={"tab": "draft"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="etsyotomasyon-listings-draft-' in resp.headers["content-disposition"]
    rows = read_csv(resp.content)
    assert len(rows) == 3 and rows[0]["tags"] == "|".join(TAGS) and rows[0]["price"] == "24.9"
    assert list(rows[0])[:2] == ["listing_id", "title"] and "url" in rows[0]
    everything = read_csv(web.client.get("/api/listings/export.csv", params={"tab": "all"}).content)
    assert len(everything) == 5
    template = web.client.get("/api/listings/template.csv")
    assert template.status_code == 200
    header = template.content.decode("utf-8-sig").splitlines()
    assert header[0].startswith("listing_id,title,description,price") and len(header) == 1


def csv_body(rows) -> bytes:
    out = io.StringIO()
    columns = list(dict.fromkeys(key for row in rows for key in row))
    writer = csv.DictWriter(out, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode("utf-8-sig")


def test_import_check_sends_nothing(web):
    shop = shop_with(web, drafts=2, active=0, inactive=0)
    good = csv_body([
        {"listing_id": "1000001", "title": "Retro Mountain Sunset Shirt", "tags": "hiking shirt|camping tee"},
        {"listing_id": "", "title": "Lemon Summer Sticker", "description": "A citrus sticker.",
         "price": "4.50", "quantity": "10", "who_made": "i_did", "when_made": "made_to_order",
         "taxonomy_id": "482", "tags": "lemon sticker"},
    ])
    check = web.client.post("/api/listings/import", content=good).json()
    assert (check["rows"], check["creates"], check["updates"], check["errors"]) == (2, 1, 1, 0)
    assert check["token"] and [r["status"] for r in check["results"]] == ["dry-run", "dry-run"]
    bad = csv_body([
        {"listing_id": "1000001", "title": "x" * 141, "tags": ""},
        {"listing_id": "1000002", "title": "Fine title", "tags": "|".join(f"tag {i}" for i in range(14))},
    ])
    check = web.client.post("/api/listings/import", content=bad).json()
    assert check["errors"] == 2
    assert "141 chars" in check["results"][0]["message"] and "14 tags" in check["results"][1]["message"]
    assert not any(method in ("PATCH", "POST") for method, _ in shop.fake.calls)


@pytest.mark.parametrize("body, code", [
    (b"", "csv_empty"),
    (b"listing_id,title\n", "csv_empty"),
    ("listing_id,title\n1,Çiçek\n".encode("cp1254"), "csv_encoding"),
    (b"name,colour\nmug,white\n", "csv_columns"),
])
def test_import_refuses_bad_files(web, body, code):
    shop_with(web, drafts=1, active=0, inactive=0)
    resp = web.client.post("/api/listings/import", content=body)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == code


def test_import_apply(web):
    shop = shop_with(web, drafts=2, active=0, inactive=0)
    body = csv_body([{"listing_id": "1000001", "title": "Retro Mountain Sunset Shirt, Hiking Tee",
                      "tags": "hiking shirt|camping tee"}])
    token = web.client.post("/api/listings/import", content=body).json()["token"]
    resp = web.client.post("/api/listings/import/apply", json={"token": token})
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "confirm_required"
    gone = web.client.post("/api/listings/import/apply", json={"token": "nope", "confirm": True})
    assert gone.status_code == 410 and gone.json()["error"]["code"] == "import_expired"
    job = web.client.post("/api/listings/import/apply", json={"token": token, "confirm": True}).json()
    final = wait_for_job(web, job["id"])
    assert final["status"] == "done" and final["kind"] == "listings-import"
    assert (final["result"]["updated"], final["result"]["errors"]) == (1, 0)
    assert shop.forms(1000001) == [{"title": "Retro Mountain Sunset Shirt, Hiking Tee",
                                    "tags": "hiking shirt,camping tee"}]
    # A token is used once.
    again = web.client.post("/api/listings/import/apply", json={"token": token, "confirm": True})
    assert again.status_code == 410


# --- review fixes -------------------------------------------------------------------------------


def _escape(text):
    import html

    return html.escape(text, quote=True).replace("&#x27;", "&#39;")


def test_an_escaped_listing_is_shown_and_edited_as_plain_text(web):
    # Etsy sends "Mom&#39;s ...". Before the client decoded it, this title failed
    # "'&' may be used only once" and its literal "&#39;" would have gone back to Etsy.
    title = "Mom's Coffee Mug, Mother's Day Gift, \"Best Mom\" Cup"
    listing = make(1, title=_escape(title), tags=[_escape("mother's day"), "coffee mug"],
                   description=_escape("Mom's favourite mug & saucer."))
    shop = Shop(web, [listing])
    detail = web.client.get("/api/listings/1000001").json()["listing"]
    assert detail["title"] == title and detail["tags"] == ["mother's day", "coffee mug"]
    assert detail["description"] == "Mom's favourite mug & saucer."
    row = web.client.get("/api/listings").json()["items"][0]
    assert row["title"] == title
    new_title = "Mom's Coffee Mug & Mother's Day Gift"
    resp = web.client.patch("/api/listings/1000001", json={
        "title": new_title, "tags": detail["tags"] + ["gift for mom"],
    })
    assert resp.status_code == 200, resp.text
    assert shop.forms(1000001)[-1] == {
        "title": new_title, "tags": "mother's day,coffee mug,gift for mom",
    }
    # The PATCH answer comes back escaped (the fixture still holds Etsy's description).
    assert resp.json()["listing"]["description"] == "Mom's favourite mug & saucer."
    assert resp.json()["listing"]["title"] == new_title


def test_the_export_has_no_views_column(web):
    shop_with(web, drafts=1, active=0, inactive=0)
    rows = read_csv(web.client.get("/api/listings/export.csv", params={"tab": "draft"}).content)
    assert "views" not in rows[0] and "num_favorers" in rows[0] and "url" in rows[0]
    assert "views" not in listings_api.EXPORT_COLUMNS


def _png(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), "white").save(path)
    return path


def _import_row(images):
    return {"listing_id": "", "title": "Lemon Summer Sticker", "description": "A citrus sticker.",
            "price": "4.50", "quantity": "10", "who_made": "i_did", "when_made": "made_to_order",
            "taxonomy_id": "482", "tags": "lemon sticker", "images": images}


@pytest.mark.parametrize("value", [
    "OUTSIDE",  # replaced by the absolute path of a real image outside the folder
    "../outside.png",
    "2-PRODUCTS/../../outside.png",
    "~/outside.png",
    "\\\\127.0.0.1\\c$\\outside.png",
    "//127.0.0.1/c$/outside.png",
    "C:outside.png",
    "C:\\outside.png",
    "/outside.png",
    "2-PRODUCTS/design.png:stream",
])
def test_import_images_must_be_inside_the_workspace(web, monkeypatch, tmp_path, value):
    from pathlib import Path

    shop = shop_with(web, drafts=1, active=0, inactive=0)
    root = web.ctx.workspace().root
    outside = _png(tmp_path / "elsewhere" / "outside.png")
    if value == "OUTSIDE":
        value = str(outside)
    touched = []
    real_is_file, real_stat = Path.is_file, Path.stat

    def is_file(self):
        touched.append(str(self))
        return real_is_file(self)

    def stat(self, *args, **kwargs):
        touched.append(str(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_file", is_file)
    monkeypatch.setattr(Path, "stat", stat)
    check = web.client.post("/api/listings/import", content=csv_body([_import_row(value)])).json()
    monkeypatch.undo()
    result = check["results"][0]
    assert check["errors"] == 1 and result["status"] == "error", result
    assert "inside the etsyotomasyon folder" in result["message"]
    # Refused from the text alone: nothing outside the folder (or on the network) was looked at.
    assert not [p for p in touched if "outside" in p or "127.0.0.1" in p]
    apply = web.client.post("/api/listings/import/apply", json={"token": check["token"], "confirm": True})
    assert apply.status_code == 422 and apply.json()["error"]["code"] == "import_invalid"
    assert not any(method == "POST" for method, _ in shop.fake.calls)
    assert root.is_dir()


def test_import_images_inside_the_workspace_are_found(web):
    shop_with(web, drafts=1, active=0, inactive=0)
    root = web.ctx.workspace().root
    _png(root / "2-PRODUCTS" / "design.png")
    for value in ("2-PRODUCTS/design.png", "2-PRODUCTS\\design.png", "./2-PRODUCTS/design.png"):
        check = web.client.post("/api/listings/import",
                                content=csv_body([_import_row(value)])).json()
        result = check["results"][0]
        assert check["errors"] == 0 and result["status"] == "dry-run", (value, result)
        assert result["message"].endswith("1 image(s)")
    missing = web.client.post("/api/listings/import",
                              content=csv_body([_import_row("2-PRODUCTS/nope.png")])).json()
    assert "image not found" in missing["results"][0]["message"]


# --- print files ---------------------------------------------------------------------------


def png_bytes(size=(60, 30)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, "white").save(out, format="PNG")
    return out.getvalue()


def test_print_files_in_the_table_and_its_filter(web):
    listings = [make(1), make(2), make(3), make(4, type="download")]
    Shop(web, listings)
    root = history(web, {"retro-mountain-sunset.png": {"status": "ok", "listing_id": 1000001}})
    # A draft etsyotomasyon made before print files: its design is still in 2-PRODUCTS.
    Image.new("RGB", (32, 32), "white").save(root / "2-PRODUCTS" / "retro-mountain-sunset.png")
    data = web.client.get("/api/listings", params={"tab": "draft", "sort": "title"}).json()
    by_id = {r["id"]: r["print_file"] for r in data["items"]}
    assert by_id == {1000001: True, 1000002: False, 1000003: False, 1000004: None}
    assert (root / "print-files" / "1000001" / "retro-mountain-sunset.png").is_file()
    assert data["prints"] == {"missing": 2, "present": 1}
    missing = web.client.get("/api/listings", params={"tab": "draft", "print": "missing"}).json()
    assert sorted(r["id"] for r in missing["items"]) == [1000002, 1000003]
    present = web.client.get("/api/listings", params={"tab": "draft", "print": "present"}).json()
    assert [r["id"] for r in present["items"]] == [1000001]
    bad = web.client.get("/api/listings", params={"print": "maybe"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid"


def test_add_a_print_file_on_the_listing_page(web):
    shop = shop_with(web, drafts=1, active=1, inactive=0)
    detail = web.client.get("/api/listings/1000101").json()["print_files"]
    assert detail["needed"] is True and detail["files"] == []
    assert detail["folder"] == "print-files/1000101"
    data = png_bytes()
    resp = web.client.put("/api/listings/1000101/print-files", params={"name": "leopard tile.png"},
                          content=data)
    assert resp.status_code == 200
    body = resp.json()
    assert (body["file"], body["duplicate"]) == ("leopard tile.png", False)
    [kept] = body["print_files"]["files"]
    assert (kept["rel"], kept["width"], kept["height"], kept["thumb"]) == (
        "print-files/1000101/leopard tile.png", 60, 30, True)
    again = web.client.put("/api/listings/1000101/print-files", params={"name": "leopard tile.png"},
                           content=data).json()
    assert again["duplicate"] is True and len(again["print_files"]["files"]) == 1
    detail = web.client.get("/api/listings/1000101").json()["print_files"]
    assert [f["name"] for f in detail["files"]] == ["leopard tile.png"]
    row = web.client.get("/api/listings", params={"tab": "active"}).json()["items"][0]
    assert row["print_file"] is True
    # Nothing of this reaches Etsy.
    assert not [c for c in shop.fake.calls if c[0] in ("PUT", "POST", "PATCH")]


@pytest.mark.parametrize("name, content, code", [
    ("setup.exe", b"MZ", "not_print_file"),
    ("tile.png", b"", "invalid"),
])
def test_a_print_file_is_checked(web, name, content, code):
    shop_with(web, drafts=1, active=0, inactive=0)
    resp = web.client.put("/api/listings/1000001/print-files", params={"name": name}, content=content)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == code
    root = web.ctx.workspace().root
    assert not (root / "print-files" / "1000001").exists() or not any(
        (root / "print-files" / "1000001").iterdir())


def test_a_print_file_over_the_limit_is_refused(web, monkeypatch):
    shop_with(web, drafts=1, active=0, inactive=0)
    monkeypatch.setattr(listings_api, "PRINT_UPLOAD_MAX", 10)
    resp = web.client.put("/api/listings/1000001/print-files", params={"name": "tile.png"},
                          content=png_bytes())
    assert resp.status_code == 413 and resp.json()["error"]["code"] == "too_large"


def test_open_the_print_folder(web, monkeypatch):
    shop_with(web, drafts=1, active=0, inactive=0)
    opened = []
    monkeypatch.setattr(listings_api.files, "open_path", opened.append)
    resp = web.client.post("/api/listings/1000001/print-files/open")
    assert resp.status_code == 200
    root = web.ctx.workspace().root
    assert opened == [root / "print-files" / "1000001"] and opened[0].is_dir()
