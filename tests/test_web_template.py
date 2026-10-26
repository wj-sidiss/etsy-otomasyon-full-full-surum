"""Şablon İlan endpoints: /api/template*. Invented data only (ExampleShop, 1000001...)."""

from __future__ import annotations

import json

import httpx
from web_helpers import ETSY_SHOP_ID, use_fake_etsy

from etsyotomasyon.drop import template as template_mod
from etsyotomasyon.web.api import template as template_api

SHOP = f"/shops/{ETSY_SHOP_ID}"

TAXONOMY = {
    "count": 1,
    "results": [
        {
            "id": 1,
            "name": "Clothing",
            "children": [
                {
                    "id": 11,
                    "name": "Tops & Tees",
                    "children": [{"id": 482, "name": "T-shirts", "children": []}],
                }
            ],
        },
        {
            "id": 2,
            "name": "Home & Living",
            "children": [{"id": 1063, "name": "Mugs", "children": []}],
        },
    ],
}
SHIPPING = {
    "count": 2,
    "results": [
        {"shipping_profile_id": 501, "title": "Standart", "origin_country_iso": "US"},
        {"shipping_profile_id": 502, "title": "Express", "origin_country_iso": "TR"},
    ],
}
RETURNS = {
    "count": 2,
    "results": [
        {"return_policy_id": 701, "accepts_returns": True, "accepts_exchanges": True,
         "return_deadline": 30},
        {"return_policy_id": 702, "accepts_returns": False, "accepts_exchanges": False,
         "return_deadline": None},
    ],
}
READINESS = {
    "count": 1,
    "results": [
        {"readiness_state_id": 801, "readiness_state": "made_to_order",
         "min_processing_days": 1, "max_processing_days": 3,
         "processing_days_display_label": "1-3 days"},
    ],
}


def listing(listing_id: int, title: str, **extra) -> dict:
    record = {
        "listing_id": listing_id,
        "shop_id": ETSY_SHOP_ID,
        "title": title,
        "state": "active",
        "price": {"amount": 2490, "divisor": 100, "currency_code": "USD"},
        "num_favorers": 48,
        "taxonomy_id": 482,
        "shipping_profile_id": 501,
        "return_policy_id": 701,
        "readiness_state_id": 801,
        "who_made": "i_did",
        "when_made": "made_to_order",
        "listing_type": "physical",
        "quantity": 999,
        "processing_min": 1,
        "processing_max": 3,
        "is_supply": False,
        "has_variations": False,
        "tags": ["retro shirt", "hiking tee"],
        "materials": ["cotton"],
        "description": "An example description.",
        "images": [
            {"rank": 2, "url_170x135": "https://i.example.com/2.jpg"},
            {"rank": 1, "url_170x135": "https://i.example.com/1.jpg",
             "url_75x75": "https://i.example.com/1s.jpg"},
        ],
    }
    record.update(extra)
    return record


def shop_listings(records: list[dict]):
    def respond(request: httpx.Request) -> dict:
        offset = int(request.url.params.get("offset", "0"))
        limit = int(request.url.params.get("limit", "25"))
        return {"count": len(records), "results": records[offset: offset + limit]}
    return respond


def connected(web, records=None):
    fake = use_fake_etsy(web)
    records = records if records is not None else [
        listing(1000001, "Retro Mountain Sunset Shirt, Vintage Hiking T-Shirt"),
        listing(1000002, "But First Coffee Mug &amp; Gift", taxonomy_id=1063, num_favorers=31,
                price={"amount": 1850, "divisor": 100, "currency_code": "USD"},
                has_variations=True),
    ]
    fake.add("GET", f"{SHOP}/listings", shop_listings(records))
    fake.add("GET", "/seller-taxonomy/nodes", TAXONOMY)
    fake.add("GET", f"{SHOP}/shipping-profiles", SHIPPING)
    fake.add("GET", f"{SHOP}/policies/return", RETURNS)
    fake.add("GET", f"{SHOP}/readiness-state-definitions", READINESS)
    for record in records:
        fake.add("GET", f"/listings/{record['listing_id']}", record)
    return fake


def fields_by_key(summary: dict) -> dict:
    return {f["key"]: f for f in summary["fields"]}


# ------------------------------------------------------------------------ setup states


def test_no_keys_asks_for_setup(web):
    resp = web.client.get("/api/template/listings")
    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "setup_needed" and error["params"] == {"step": "keys"}
    assert web.client.get("/api/template/preview/1000001").json()["error"]["code"] == "setup_needed"
    resp = web.client.post("/api/template", json={"listing_id": 1000001})
    assert resp.status_code == 409


def test_keys_without_a_connection_asks_to_connect(web):
    use_fake_etsy(web, connected=False)
    resp = web.client.get("/api/template/listings")
    assert resp.status_code == 409
    assert resp.json()["error"]["params"] == {"step": "connect"}


def test_no_template_yet(web):
    resp = web.client.get("/api/template")
    assert resp.status_code == 200
    assert resp.json() == {"template": None, "problem": None}
    # Reading the template must not create the products folder.
    assert not (web.desktop / "Etsy Studio").exists()


def test_offline(web):
    fake = connected(web)
    fake.offline = True
    resp = web.client.get("/api/template/listings")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "offline"


# ------------------------------------------------------------------------ listings


def test_listings_are_mapped_for_the_page(web):
    fake = connected(web)
    data = web.client.get("/api/template/listings").json()
    assert data["count"] == 2 and data["truncated"] is False
    assert data["template_listing_id"] is None
    assert data["sales"] is None
    first, second = data["items"]
    assert first == {
        "listing_id": 1000001,
        "title": "Retro Mountain Sunset Shirt, Vintage Hiking T-Shirt",
        "price": 24.9,
        "currency": "USD",
        "thumb_url": "https://i.example.com/1.jpg",  # the rank-1 image
        "state": "active",
        "num_favorers": 48,
        "sold": None,  # Kâr-Zarar has read no month yet
        "product_type": "T-shirts",  # the taxonomy leaf
        "product_type_key": "tshirt",  # the page says it in the seller's language
        "has_variations": False,
        "listing_type": "physical",
    }
    assert second["title"] == "But First Coffee Mug & Gift"  # entities decoded
    assert second["product_type"] == "Mugs" and second["price"] == 18.5
    assert second["product_type_key"] == "mug"
    assert second["has_variations"] is True
    request = next(r for r in fake.requests if r.url.path.endswith(f"{SHOP}/listings"))
    assert request.url.params["state"] == "active"
    assert request.url.params["includes"] == "Images"


def test_listings_are_cached_until_refresh(web):
    fake = connected(web)
    web.client.get("/api/template/listings")
    web.client.get("/api/template/listings")
    assert fake.calls.count(("GET", f"{SHOP}/listings")) == 1
    web.client.get("/api/template/listings", params={"refresh": 1})
    assert fake.calls.count(("GET", f"{SHOP}/listings")) == 2
    # The taxonomy is fetched once, then read from memory (and disk).
    assert fake.calls.count(("GET", "/seller-taxonomy/nodes")) == 1


def test_only_https_images_reach_the_page(web):
    connected(web, [listing(1000001, "Example", images=[
        {"rank": 1, "url_170x135": "javascript:alert(1)", "url_570xN": "http://x.example/a.jpg"},
    ])])
    item = web.client.get("/api/template/listings").json()["items"][0]
    assert item["thumb_url"] is None


def test_taxonomy_is_cached_on_disk(web):
    fake = connected(web)
    web.client.get("/api/template/listings")
    assert fake.calls.count(("GET", "/seller-taxonomy/nodes")) == 1
    # A new app instance (fresh memory) reads the disk copy instead of asking Etsy.
    other = template_api.TemplateApi(web.ctx)
    paths = other._taxonomy_paths(None)
    assert paths[482] == ["Clothing", "Tops & Tees", "T-shirts"]
    assert fake.calls.count(("GET", "/seller-taxonomy/nodes")) == 1


def test_flatten_taxonomy_handles_odd_nodes():
    paths = template_api._flatten_taxonomy([
        {"id": 1, "name": "A", "children": [{"id": "2", "name": "B"}, "junk", {"name": "no id"}]},
        None,
    ])
    assert paths == {1: ["A"], 2: ["A", "B"]}


# ------------------------------------------------------------------------ preview


def test_preview_resolves_the_seven_fields(web):
    connected(web)
    resp = web.client.get("/api/template/preview/1000001")
    assert resp.status_code == 200
    summary = resp.json()
    assert summary["listing_id"] == 1000001
    assert summary["ok_count"] == 7 and summary["total"] == 7 and summary["resolved"] is True
    assert summary["is_current"] is False
    fields = fields_by_key(summary)
    assert [f["key"] for f in summary["fields"]] == list(template_api.FIELD_KEYS)
    assert fields["price"]["value"] == {"amount": 24.9, "currency": "USD"}
    assert fields["shipping"]["value"] == {
        "id": 501, "title": "Standart", "origin_country": "US", "digital": False,
    }
    assert fields["category"]["value"] == {
        "id": 482, "path": ["Clothing", "Tops & Tees", "T-shirts"],
    }
    assert fields["who_made"]["value"] == {"code": "i_did"}
    assert fields["when_made"]["value"] == {"code": "made_to_order"}
    assert fields["processing"]["value"] == {
        "readiness_state_id": 801, "readiness_state": "made_to_order", "min": 1, "max": 3,
        "label": "1-3 days", "digital": False,
    }
    assert summary["listing_type"] == "physical"
    assert fields["returns"]["value"] == {
        "id": 701, "accepts_returns": True, "accepts_exchanges": True, "deadline": 30,
    }


def test_preview_uses_the_cached_list_and_fetches_others(web):
    fake = connected(web)
    web.client.get("/api/template/listings")
    web.client.get("/api/template/preview/1000001")
    assert ("GET", "/listings/1000001") not in fake.calls
    # A draft (not in the active list) is fetched by its number.
    fake.add("GET", "/listings/1000009", listing(1000009, "A draft", state="draft"))
    summary = web.client.get("/api/template/preview/1000009").json()
    assert summary["state"] == "draft" and summary["title"] == "A draft"
    request = next(r for r in fake.requests if r.url.path.endswith("/listings/1000009"))
    assert request.headers["authorization"].startswith("Bearer ")


def test_missing_fields_are_flagged(web):
    record = listing(
        1000003, "Half set up",
        shipping_profile_id=None, return_policy_id=None, readiness_state_id=None,
        processing_min=None, processing_max=None, who_made=None, when_made="not_an_era",
    )
    connected(web, [record])
    summary = web.client.get("/api/template/preview/1000003").json()
    fields = fields_by_key(summary)
    assert summary["ok_count"] == 2
    assert fields["shipping"] == {
        "key": "shipping", "ok": False, "required": True,
        "value": {"id": None, "title": None, "origin_country": None, "digital": False},
    }
    assert fields["who_made"]["ok"] is False and fields["who_made"]["required"] is True
    assert fields["when_made"]["ok"] is False  # an unknown enum is dropped by capture()
    assert fields["processing"]["ok"] is False and fields["processing"]["required"] is False
    assert fields["returns"]["ok"] is False and fields["returns"]["required"] is False
    assert fields["price"]["ok"] and fields["category"]["ok"]


def test_a_digital_listing_needs_no_shipping(web):
    connected(web, [listing(1000004, "Printable", listing_type="download", shipping_profile_id=None)])
    summary = web.client.get("/api/template/preview/1000004").json()
    fields = fields_by_key(summary)
    assert summary["listing_type"] == "download"
    assert fields["shipping"]["ok"] is True and fields["shipping"]["required"] is False
    assert fields["shipping"]["value"]["digital"] is True
    # Nothing is shipped, so there is no processing time to copy either.
    assert fields["processing"]["ok"] is True and fields["processing"]["value"]["digital"] is True
    assert summary["ok_count"] == summary["total"] == 7


def test_a_digital_template_is_saved_as_digital_without_shipping(web):
    record = listing(1000004, "Boho Planner Printable", listing_type="download",
                     shipping_profile_id=None, readiness_state_id=None)
    connected(web, [record])
    listed = web.client.get("/api/template/listings").json()["items"][0]
    assert listed["listing_type"] == "download"
    saved = web.client.post("/api/template", json={"listing_id": 1000004}).json()["template"]
    assert saved["listing_type"] == "download" and saved["ok_count"] == 7
    data = json.loads(web.ctx.workspace().template_path.read_text(encoding="utf-8"))
    assert data["fields"]["type"] == "download"
    for name in ("shipping_profile_id", "readiness_state_id", "processing_min", "processing_max"):
        assert name not in data["fields"]
    current = web.client.get("/api/template").json()["template"]
    assert current["listing_type"] == "download"
    assert fields_by_key(current)["shipping"]["ok"] is True


def test_a_physical_and_digital_listing_still_needs_its_shipping(web):
    connected(web, [listing(1000006, "Mug plus printable", listing_type="both",
                            shipping_profile_id=None)])
    summary = web.client.get("/api/template/preview/1000006").json()
    fields = fields_by_key(summary)
    assert summary["listing_type"] == "both"
    assert fields["shipping"]["ok"] is False and fields["shipping"]["required"] is True
    assert fields["shipping"]["value"]["digital"] is False
    assert fields["processing"]["value"]["digital"] is False


def test_processing_falls_back_to_the_listing_days(web):
    connected(web, [listing(1000005, "Old style", readiness_state_id=None,
                            processing_min=2, processing_max=4)])
    fields = fields_by_key(web.client.get("/api/template/preview/1000005").json())
    assert fields["processing"]["ok"] is True
    assert fields["processing"]["value"]["min"] == 2 and fields["processing"]["value"]["max"] == 4


def test_a_refused_lookup_leaves_names_unresolved(web):
    fake = connected(web)
    fake.error("GET", f"{SHOP}/shipping-profiles", 403, "insufficient scope")
    summary = web.client.get("/api/template/preview/1000001").json()
    fields = fields_by_key(summary)
    assert summary["resolved"] is False
    assert fields["shipping"]["ok"] is True
    assert fields["shipping"]["value"]["id"] == 501 and fields["shipping"]["value"]["title"] is None
    assert fields["returns"]["value"]["deadline"] == 30  # the other lookups still resolve


def test_a_listing_of_another_shop_is_refused(web):
    fake = connected(web)
    fake.add("GET", "/listings/1000010", listing(1000010, "Not mine", shop_id=99999999))
    resp = web.client.get("/api/template/preview/1000010")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "not_your_listing"
    resp = web.client.post("/api/template", json={"listing_id": 1000010})
    assert resp.json()["error"]["code"] == "not_your_listing"
    assert not (web.desktop / "Etsy Studio" / "product.json").exists()


def test_an_unknown_listing_is_not_found(web):
    connected(web)
    resp = web.client.get("/api/template/preview/1000404")
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "not_found"


# ------------------------------------------------------------------------ save


def test_save_writes_product_json(web):
    fake = connected(web)
    web.client.get("/api/template/listings")
    resp = web.client.post("/api/template", json={"listing_id": 1000002})
    assert resp.status_code == 200
    summary = resp.json()["template"]
    assert summary["is_current"] is True and summary["saved_at"]
    assert summary["title"] == "But First Coffee Mug & Gift"
    assert summary["has_variations"] is True
    # Saving always reads the listing fresh from Etsy (the price may have changed).
    assert ("GET", "/listings/1000002") in fake.calls

    ws = web.ctx.workspace(create=False)
    data = json.loads(ws.template_path.read_text(encoding="utf-8"))
    assert data["source_listing_id"] == 1000002
    assert data["source_title"] == "But First Coffee Mug & Gift"
    assert data["currency_code"] == "USD" and data["has_variations"] is True
    loaded = template_mod.Template.from_dict(data)  # what the drop pipeline reads
    assert loaded.fields["price"] == 18.5
    assert loaded.fields["taxonomy_id"] == 1063
    assert loaded.fields["shipping_profile_id"] == 501
    assert loaded.fields["who_made"] == "i_did"
    assert web.ctx.shop_prefs()["template_listing"] == "1000002"

    current = web.client.get("/api/template").json()
    assert current["problem"] is None
    assert current["template"]["listing_id"] == 1000002
    assert current["template"]["ok_count"] == 7
    assert fields_by_key(current["template"])["category"]["value"]["path"][-1] == "Mugs"
    assert web.client.get("/api/template/listings").json()["template_listing_id"] == 1000002

    status = web.ctx.refresh_status(force=True)
    assert status["setup"]["template"] is True
    assert status["setup"]["template_title"] == "But First Coffee Mug & Gift"


def test_save_validates_the_listing_id(web):
    connected(web)
    for body in ({}, {"listing_id": "abc"}, {"listing_id": -3}, {"listing_id": True},
                 {"listing_id": 10**13}):
        resp = web.client.post("/api/template", json=body)
        assert resp.status_code == 422, body
        assert resp.json()["error"]["code"] == "invalid"
        assert resp.json()["error"]["params"]["field"] == "listing_id"
    resp = web.client.post("/api/template", content=b"[1]", headers={"Content-Type": "application/json"})
    assert resp.status_code == 400


def test_save_accepts_a_numeric_string(web):
    connected(web)
    resp = web.client.post("/api/template", json={"listing_id": "1000001"})
    assert resp.status_code == 200
    assert resp.json()["template"]["listing_id"] == 1000001


def test_the_saved_template_shows_without_a_connection(web):
    connected(web)
    web.client.post("/api/template", json={"listing_id": 1000001})
    fake = use_fake_etsy(web)  # a fresh fake: every Etsy call now fails
    fake.offline = True
    web.ctx.reset_client()
    resp = web.client.get("/api/template")
    assert resp.status_code == 200
    summary = resp.json()["template"]
    assert summary["listing_id"] == 1000001 and summary["ok_count"] == 7
    fields = fields_by_key(summary)
    # Names Etsy would have to supply stay unresolved; the taxonomy comes from disk.
    assert summary["resolved"] is False
    assert fields["shipping"]["value"]["id"] == 501
    assert fields["category"]["value"]["path"][-1] == "T-shirts"


def test_a_broken_product_json_is_reported(web):
    ws = web.ctx.workspace()
    ws.template_path.write_text("{not json", encoding="utf-8")
    assert web.client.get("/api/template").json() == {"template": None, "problem": "malformed"}
    ws.template_path.write_text(json.dumps({"fields": {}}), encoding="utf-8")
    assert web.client.get("/api/template").json()["problem"] == "malformed"


def test_a_template_written_by_the_cli_still_shows(web):
    """product.json from `etsyotomasyon drop template` has no currency or variations keys."""
    connected(web)
    captured = template_mod.capture(listing(1000001, "Example"))
    web.ctx.workspace().write_template(captured.to_dict())
    summary = web.client.get("/api/template").json()["template"]
    assert summary["listing_id"] == 1000001
    assert summary["currency"] is None or summary["currency"] == "USD"
    assert summary["has_variations"] is False
    assert fields_by_key(summary)["price"]["value"]["amount"] == 24.9


# ------------------------------------------------------------------------ Etsy's escaped text


def test_etsy_entities_become_plain_text_in_the_template_and_new_drafts(web):
    # Etsy sends titles, descriptions, tags and materials HTML-escaped. The client decodes
    # them once, so the page, product.json and every draft built from it hold plain text.
    from etsyotomasyon.drop import generate
    from etsyotomasyon.drop.seeds import Seed

    record = listing(
        1000003, "Mom&#39;s &quot;Best&quot; Mug &amp; Gift",
        description="Mom&#39;s favourite mug &amp; saucer. Holds 11&nbsp;oz.",
        tags=["mother&#39;s day", "mom &amp; dad"], materials=["ceramic &amp; glaze"],
    )
    fake = connected(web, [record])
    fake.add("GET", f"{SHOP}/shipping-profiles", {"count": 1, "results": [
        {"shipping_profile_id": 501, "title": "Mugs &amp; Cups", "origin_country_iso": "US"}]})
    plain_title = "Mom's \"Best\" Mug & Gift"
    items = web.client.get("/api/template/listings").json()["items"]
    assert items[0]["title"] == plain_title
    preview = web.client.get("/api/template/preview/1000003").json()
    assert preview["title"] == plain_title
    assert fields_by_key(preview)["shipping"]["value"]["title"] == "Mugs & Cups"
    saved = web.client.post("/api/template", json={"listing_id": 1000003}).json()["template"]
    assert saved["title"] == plain_title
    data = json.loads(web.ctx.workspace().template_path.read_text(encoding="utf-8"))
    assert data["source_title"] == plain_title
    assert data["description"] == "Mom's favourite mug & saucer. Holds 11 oz."
    assert data["tags"] == ["mother's day", "mom & dad"]
    assert data["materials"] == ["ceramic & glaze"]
    assert "&#39;" not in json.dumps(data) and "&amp;" not in json.dumps(data)
    # A draft's description is the template's own, so it carries plain text too.
    template = template_mod.Template.from_dict(data)
    description = generate.build_description(Seed("sunset mug", "sunset-mug.png"),
                                             template.description, "Sunset Mug")
    assert "Mom's favourite mug & saucer." in description and "&" + "#39;" not in description
    current = web.client.get("/api/template").json()["template"]
    assert current["title"] == plain_title


def test_plain_text_is_not_decoded_a_second_time(web):
    # A title whose plain text looks like an entity ("&amp;" typed by the seller comes
    # back from Etsy as "&amp;amp;") stays what the seller typed.
    connected(web, [listing(1000004, "R&amp;amp;B Poster")])
    items = web.client.get("/api/template/listings").json()["items"]
    assert items[0]["title"] == "R&amp;B Poster"
    # Saved, product.json is marked plain, so reading it back does not decode it again.
    assert web.client.post("/api/template", json={"listing_id": 1000004}).status_code == 200
    data = json.loads(web.ctx.workspace().template_path.read_text(encoding="utf-8"))
    assert data["plain_text"] is True and data["source_title"] == "R&amp;B Poster"
    assert template_mod.Template.from_dict(data).source_title == "R&amp;B Poster"
    assert web.client.get("/api/template").json()["template"]["title"] == "R&amp;B Poster"


def test_a_product_json_saved_before_the_fix_reads_as_plain_text(web):
    # product.json written by 0.2.0 holds Etsy's escaped text and no plain_text mark.
    # The page and every draft built from it (Template.from_dict) see plain text.
    connected(web)
    web.ctx.workspace().write_template({
        "source_listing_id": 1000001,
        "source_title": "Mom&#39;s &quot;Best&quot; Shirt &amp; Gift",
        "fields": {"taxonomy_id": 482, "shipping_profile_id": 501, "price": 24.9},
        "materials": ["cotton &amp; linen"],
        "description": "Soft &amp; light. Mom&#39;s favourite.",
        "tags": ["mother&#39;s day"],
    })
    summary = web.client.get("/api/template").json()["template"]
    assert summary["title"] == 'Mom\'s "Best" Shirt & Gift'
    template = template_mod.Template.from_dict(web.ctx.workspace().read_template())
    assert template.description == "Soft & light. Mom's favourite."
    assert template.tags == ["mother's day"] and template.materials == ["cotton & linen"]
