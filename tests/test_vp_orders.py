"""Siparişler and Kâr-Zarar as the video shows them: the product kind under each order
("Kupa · 11oz"), the whole waiting list read at once for "select all", the carriers and
numbers kept until they are sent (the video's rows open with them in place), and the
strings both pages ask for.

Every value is invented: shop 12345678, receipts 3000001..., listings 1000001...
"""

from __future__ import annotations

import json
import re

from test_web_orders import RECEIPTS, make_receipt, setup_orders, shipment_answer
from web_helpers import wait_for_job

from etsyotomasyon.config import home_dir
from etsyotomasyon.drop import catalog
from etsyotomasyon.web.api import orders as orders_api
from etsyotomasyon.web.server import STATIC_DIR

I18N = STATIC_DIR / "i18n"
PAGES = STATIC_DIR / "js" / "pages"


def strings(name: str) -> dict:
    return json.loads((I18N / f"{name}.json").read_text(encoding="utf-8"))


def receipt_with(*titles: str) -> dict:
    receipt = make_receipt(1, items=len(titles))
    for txn, title in zip(receipt["transactions"], titles):
        txn["title"] = title
    return receipt


def test_order_items_name_their_product_kind():
    row = orders_api.flatten(receipt_with(
        "Just One More Chapter Mug, Book Lover Gift",
        "Cat Mom Club Tote Bag",
        "Plain Gift Card",
    ))
    assert [item["type"] for item in row["items"]] == ["mug", "tote", "other"]


def test_every_product_kind_has_a_label_on_both_pages():
    orders, profit = strings("orders"), strings("profit")
    for lang in ("tr", "en"):
        kinds = {k for k in orders[lang] if k.startswith("type.")}
        assert kinds == {f"type.{t}" for t in catalog.TYPES}
        assert {k: orders[lang][k] for k in kinds} == {k: profit[lang][k] for k in kinds}


def test_the_whole_waiting_list_is_read_without_asking_etsy_again(web):
    receipts = [make_receipt(n) for n in range(1, 13)] + [make_receipt(20, shipped=True)]
    fake, store = setup_orders(web, receipts)
    first = web.client.get("/api/orders", params={"tab": "unshipped", "per_page": 8}).json()
    assert first["total"] == 12 and len(first["ids"]) == 12 and first["pages"] == 2
    asked = sum(1 for call in fake.calls if call == ("GET", RECEIPTS))
    assert asked >= 1
    # "Select all" reads every waiting order in pages of 100 (the server's maximum).
    whole = web.client.get("/api/orders", params={"tab": "unshipped", "per_page": 100, "page": 1}).json()
    assert [r["receipt_id"] for r in whole["rows"]] == first["ids"]
    assert all(r["status"] == "unshipped" for r in whole["rows"])
    assert sum(1 for call in fake.calls if call == ("GET", RECEIPTS)) == asked
    assert len(store.queries) == asked


def keys_used(js: str) -> set[str]:
    return set(re.findall(r"""\bt\(\s*["']([A-Za-z0-9_.:]+)["']""", js))


def test_the_pages_ask_only_for_strings_that_exist():
    common = strings("common")
    for page in ("orders", "profit"):
        own = strings(page)
        js = (PAGES / f"{page}.js").read_text(encoding="utf-8")
        missing = []
        for key in sorted(keys_used(js)):
            if ":" in key:
                ns, name = key.split(":", 1)
                found = name in strings(ns)["tr"]
            else:
                found = key in own["tr"] or key in common["tr"]
            if not found:
                missing.append(key)
        assert not missing, f"{page}.js: {missing}"


def test_the_video_wording_of_the_orders_page():
    tr, en = strings("orders")["tr"], strings("orders")["en"]
    assert tr["send"] == "Takip numaralarını yükle ({n})" and tr["send.none"] == "Takip numaralarını yükle"
    assert tr["csv.button"] == "CSV içe aktar" and en["csv.button"] == "Import CSV"
    assert tr["status.unshipped"] == "Hazırlanıyor"
    assert tr["uploading"] == "Etsy'ye yükleniyor"
    assert tr["selected"] == "{n} sipariş seçili" and en["selected_one"] == "1 order selected"
    # The confirm step stays: Etsy e-mails every buyer, which cannot be undone.
    assert "e-posta" in tr["confirm.msg"] and "Yükle" in tr["confirm.ok"]


# --- drafts: carriers and numbers not sent yet ----------------------------------------------


def draft(n: int, tracking: str = "", carrier: str = "UPS", note: str = "") -> dict:
    return {"receipt_id": 3000000 + n, "carrier_name": carrier,
            "tracking_code": tracking or f"1ZEXAMPLE{n:02d}", "note_to_buyer": note}


def test_unsent_numbers_are_kept_in_their_own_file_and_come_back(web):
    setup_orders(web, [make_receipt(n) for n in range(1, 4)])
    rows = [draft(1), draft(2, carrier=""), draft(3, tracking=" ", carrier="USPS")]
    resp = web.client.post("/api/orders/drafts", json={"rows": rows})
    assert resp.status_code == 200 and resp.json() == {"saved": 3}
    back = web.client.get("/api/orders/drafts").json()
    assert back["checked"] is True
    assert back["rows"] == [
        {"receipt_id": 3000001, "tracking_code": "1ZEXAMPLE01", "carrier_name": "UPS", "note_to_buyer": ""},
        {"receipt_id": 3000002, "tracking_code": "1ZEXAMPLE02", "carrier_name": "", "note_to_buyer": ""},
        {"receipt_id": 3000003, "tracking_code": "", "carrier_name": "USPS", "note_to_buyer": ""},
    ]
    # A file of its own in the shop's home: never in the prefs that /api/prefs returns whole.
    assert (home_dir() / orders_api.DRAFTS_FILE).is_file()
    assert "1ZEXAMPLE01" not in json.dumps(web.ctx.shop_prefs())
    # An empty list clears them.
    assert web.client.post("/api/orders/drafts", json={"rows": []}).json() == {"saved": 0}
    assert not (home_dir() / orders_api.DRAFTS_FILE).exists()
    assert web.client.get("/api/orders/drafts").json()["rows"] == []


def test_drafts_are_checked_like_an_imported_csv(web):
    setup_orders(web, [make_receipt(1)])
    for bad in ({"rows": "x"}, {"rows": [{"receipt_id": "abc", "tracking_code": "1Z"}]},
                {"rows": [{"receipt_id": -4, "tracking_code": "1Z"}]},
                {"rows": [{"receipt_id": 3000001, "tracking_code": "1" * 65}]},
                {"rows": [{"receipt_id": 3000001, "tracking_code": 12}]},
                {"rows": [draft(1)] * (orders_api.SHIP_MAX_ROWS + 1)}):
        resp = web.client.post("/api/orders/drafts", json=bad)
        assert resp.status_code == 422 and resp.json()["error"]["code"] == "invalid", bad
    # "#" is allowed before the number, an empty row is dropped, the later row wins.
    rows = [{"receipt_id": "#3000001", "tracking_code": "OLD", "carrier_name": "UPS"},
            {"receipt_id": 3000001, "tracking_code": "NEW", "carrier_name": "DHL"},
            {"receipt_id": 3000002, "tracking_code": " ", "carrier_name": ""}]
    assert web.client.post("/api/orders/drafts", json={"rows": rows}).json() == {"saved": 1}
    assert orders_api.draft_rows(rows) == [
        {"receipt_id": 3000001, "tracking_code": "NEW", "carrier_name": "DHL", "note_to_buyer": ""}]


def test_drafts_of_orders_shipped_meanwhile_are_dropped(web):
    receipts = [make_receipt(1), make_receipt(2)]
    setup_orders(web, receipts)
    web.client.post("/api/orders/drafts", json={"rows": [draft(1), draft(2)]})
    assert len(web.client.get("/api/orders/drafts").json()["rows"]) == 2
    # Shipped on etsy.com in the meantime: dropped once the waiting list is read again.
    receipts[1].update(make_receipt(2, shipped=True))
    web.client.get("/api/orders", params={"tab": "unshipped", "fresh": 1})
    back = web.client.get("/api/orders/drafts").json()
    assert [r["receipt_id"] for r in back["rows"]] == [3000001] and back["checked"] is True
    stored = json.loads((home_dir() / orders_api.DRAFTS_FILE).read_text(encoding="utf-8"))
    assert [r["receipt_id"] for r in stored["rows"]] == [3000001]


def test_drafts_stay_when_etsy_cannot_be_asked(web):
    fake, _store = setup_orders(web, [make_receipt(1)])
    web.client.post("/api/orders/drafts", json={"rows": [draft(1), draft(9)]})
    fake.offline = True
    back = web.client.get("/api/orders/drafts").json()
    assert back["checked"] is False and [r["receipt_id"] for r in back["rows"]] == [3000001, 3000009]


def test_a_send_forgets_the_drafts_etsy_took(web):
    fake, _store = setup_orders(web, [make_receipt(1), make_receipt(2), make_receipt(3)])
    fake.add("POST", f"{RECEIPTS}/3000001/tracking", shipment_answer)
    fake.error("POST", f"{RECEIPTS}/3000002/tracking", 400, "tracking_code is not valid")
    web.client.post("/api/orders/drafts", json={"rows": [draft(1), draft(2), draft(3)]})
    job = web.client.post("/api/orders/ship", json={
        "rows": [draft(1), draft(2)], "country": "TR", "confirm": True}).json()
    result = wait_for_job(web, job["id"])["result"]
    assert (result["sent"], result["failed"]) == (1, 1)
    stored = json.loads((home_dir() / orders_api.DRAFTS_FILE).read_text(encoding="utf-8"))
    # The refused number stays to be corrected; the one not sent stays too.
    assert [r["receipt_id"] for r in stored["rows"]] == [3000002, 3000003]


def test_the_orders_page_keeps_its_drafts_and_its_video_controls():
    js = (PAGES / "orders.js").read_text(encoding="utf-8")
    # Saved a moment after each change, read before the first rows show, flushed on leave.
    assert "/api/orders/drafts" in js and "saveDraftsSoon()" in js and "keepalive: true" in js
    # The upload button is never greyed out: aria-disabled while Etsy works.
    assert "sendBtn.setDisabled" not in js and "sendBtn.setLoading" not in js
    assert 'setAttribute("aria-disabled", "true")' in js
    orders, profit = strings("orders"), strings("profit")
    for lang in ("tr", "en"):
        assert {"leave.unsaved", "leave.unsaved_one", "drafts.failed"} <= set(orders[lang])
        assert "leave.unsent" not in orders[lang] and "products.show_less" not in profit[lang]
