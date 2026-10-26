"""Kâr-Zarar: the P&L arithmetic, the fee buckets, costs.json, TCMB rates and the API.

All data is invented: ExampleShop (shop 12345678), listings 1000001..., made-up money.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from urllib.parse import parse_qs

import httpx
import pytest
from web_helpers import ETSY_SHOP_ID, FakeEtsy, use_fake_etsy, wait_for_job

from etsyotomasyon import auth
from etsyotomasyon.client import RateLimiter
from etsyotomasyon.web.api import profit

RECEIPTS_PATH = f"/shops/{ETSY_SHOP_ID}/receipts"
LEDGER_PATH = f"/shops/{ETSY_SHOP_ID}/payment-account/ledger-entries"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """TCMB is never contacted from a test; a test that wants rates patches this."""

    def refuse():
        raise OSError("no network in tests")

    monkeypatch.setattr(profit, "_fetch_tcmb", refuse)
    # A month is ~3 Etsy calls; the fake needs no rate limit.
    monkeypatch.setattr(RateLimiter, "acquire", lambda self: None)


def money(value: float, currency: str = "USD") -> dict:
    return {"amount": int(round(value * 100)), "divisor": 100, "currency_code": currency}


def receipt(receipt_id, created, lines, *, shipping=0.0, discount=0.0, tax=0.0, refunds=(),
            status="paid", currency="USD"):
    total = sum(price * qty for _lid, _title, price, qty in lines)
    return {
        "receipt_id": receipt_id,
        "status": status,
        "created_timestamp": created,
        "total_price": money(total, currency),
        "subtotal": money(total - discount, currency),
        "discount_amt": money(discount, currency),
        "total_shipping_cost": money(shipping, currency),
        "total_tax_cost": money(tax, currency),
        "total_vat_cost": money(0, currency),
        "gift_wrap_price": money(0, currency),
        "refunds": [{"amount": money(r, currency), "status": "succeeded"} for r in refunds],
        "transactions": [
            {"listing_id": lid, "title": title, "price": money(price, currency), "quantity": qty,
             "listing_image_id": None}
            for lid, title, price, qty in lines
        ],
    }


def entry(kind, cents, created=0, currency="USD", description=None):
    return {"ledger_type": kind, "description": description or kind, "amount": cents,
            "currency": currency, "created_timestamp": created}


def paged(items):
    """A FakeEtsy responder that filters by min/max_created and pages like Etsy."""

    def respond(request: httpx.Request):
        q = parse_qs(request.url.query.decode())
        lo = int(q.get("min_created", ["0"])[0])
        hi = int(q.get("max_created", ["9999999999"])[0])
        limit = int(q.get("limit", ["25"])[0])
        offset = int(q.get("offset", ["0"])[0])
        rows = [r for r in items if lo <= r["created_timestamp"] <= hi]
        return {"count": len(rows), "results": rows[offset:offset + limit]}

    return respond


# ================================================================== months


def test_month_helpers_cross_year_edges():
    assert profit.parse_month("2026-09") == (2026, 9)
    for bad in ("2026-13", "2026-00", "26-09", "2026-9", "", "1999-12", "2026-09-01"):
        with pytest.raises(ValueError):
            profit.parse_month(bad)
    assert profit.shift_month("2026-01", -1) == "2025-12"
    assert profit.shift_month("2025-12", 1) == "2026-01"
    assert profit.shift_month("2026-03", -14) == "2025-01"
    assert profit.recent_months("2026-02", 4) == ["2025-11", "2025-12", "2026-01", "2026-02"]
    assert profit.months_between("2025-11", "2026-02") == 3


def test_month_range_is_the_local_calendar_month():
    start, end = profit.month_range("2025-12")
    assert start == int(datetime(2025, 12, 1).timestamp())
    assert end == int(datetime(2026, 1, 1).timestamp()) - 1
    feb_start, feb_end = profit.month_range("2028-02")  # leap year
    assert feb_end - feb_start + 1 == int(datetime(2028, 3, 1).timestamp()) - int(
        datetime(2028, 2, 1).timestamp()
    )
    assert profit.month_range("2026-01")[0] == end + 1


def test_current_month():
    moment = datetime(2026, 9, 26, 14, 0).timestamp()
    assert profit.current_month(moment) == "2026-09"


def test_raw_freshness():
    now = datetime(2026, 9, 26, 12, 0).timestamp()
    cur = profit.current_month(now)
    assert profit.raw_is_fresh({"fetched_at": now - 60}, cur, now)
    assert not profit.raw_is_fresh({"fetched_at": now - 3600}, cur, now)
    # August fetched on Sept 1st (not settled yet): 6 hours.
    fetched = datetime(2026, 9, 1, 9, 0).timestamp()
    assert not profit.raw_is_fresh({"fetched_at": fetched}, "2026-08", now)
    # July fetched on Sept 20th (settled): a week.
    fetched = datetime(2026, 9, 20, 9, 0).timestamp()
    assert profit.raw_is_fresh({"fetched_at": fetched}, "2026-07", now)
    assert not profit.raw_is_fresh(None, "2026-07", now)


# ================================================================== receipts


def test_receipts_revenue_coupons_refunds_and_tax():
    receipts = [
        receipt(1, 100, [(1000001, "Retro Mountain Sunset Shirt", 30.0, 2),
                         (1000002, "Just One More Chapter Mug", 20.0, 1)],
                shipping=5.0, discount=8.0, tax=4.2),
        receipt(2, 200, [(1000001, "Retro Mountain Sunset Shirt", 30.0, 1)], refunds=[30.0]),
        receipt(3, 300, [(1000003, "Wildflower Botanical Print", 25.0, 1)], status="canceled"),
    ]
    out = profit.summarise_receipts(receipts, "USD")
    assert out["orders"] == 2
    assert out["items_sold"] == 4
    assert out["items_revenue"] == pytest.approx(80.0 - 8.0 + 30.0)
    assert out["shipping_charged"] == pytest.approx(5.0)
    assert out["tax"] == pytest.approx(4.2)
    assert out["refunds"] == pytest.approx(30.0)
    shirt = out["products"]["l1000001"]
    mug = out["products"]["l1000002"]
    # The $8 coupon is shared 60/20 between the shirts and the mug.
    assert shirt["qty"] == 3 and shirt["revenue"] == pytest.approx(60 - 6 + 30)
    assert mug["qty"] == 1 and mug["revenue"] == pytest.approx(20 - 2)
    assert sum(p["revenue"] for p in out["products"].values()) == pytest.approx(out["items_revenue"])
    assert out["order_items"] == [["l1000001", "l1000002"], ["l1000001"]]


def test_receipts_in_another_currency_need_a_rate():
    receipts = [receipt(1, 100, [(1000001, "Tee", 100.0, 1)], currency="EUR")]
    with pytest.raises(profit.CurrencyError):
        profit.summarise_receipts(receipts, "USD")
    convert = profit.make_converter({"EUR": 50.0, "USD": 40.0})
    out = profit.summarise_receipts(receipts, "USD", convert)
    assert out["items_revenue"] == pytest.approx(125.0)


def test_a_transaction_without_listing_is_grouped_by_title():
    out = profit.summarise_receipts(
        [receipt(1, 1, [(None, "Old Tee", 10.0, 1)]), receipt(2, 2, [(None, "Old Tee", 10.0, 1)])],
        "USD",
    )
    (key,) = out["products"]
    assert key.startswith("t") and out["products"][key]["qty"] == 2
    assert out["products"][key]["listing_id"] is None


# ================================================================== ledger


@pytest.mark.parametrize("kind, bucket", [
    ("listing", "listing"),
    ("renew_sold", "listing"),
    ("renew_expired", "listing"),
    ("transaction", "transaction"),
    ("transaction_quantity", "transaction"),
    ("shipping_transaction", "transaction"),
    ("PAYMENT_PROCESSING_FEE", "processing"),
    ("prolist", "ads"),
    ("offsite_ads_fee", "ads"),
    ("shipping_labels", "labels"),
    ("vat_seller_services", "other"),
    ("subscription", "other"),
    ("PAYMENT_GROSS", None),
    ("payment", None),
    ("DISBURSE2", None),
    ("REFUND", None),
    ("sales_tax", None),
    ("RESERVE", None),
])
def test_ledger_entries_fall_into_buckets(kind, bucket):
    assert profit.classify_ledger(entry(kind, -100)) == bucket


def test_unknown_ledger_entries_count_only_when_money_goes_out():
    assert profit.classify_ledger(entry("mystery", -150)) == "other"
    assert profit.classify_ledger(entry("mystery", 150)) is None


def test_ledger_summary_in_minor_units_with_credits():
    entries = [
        entry("listing", -20), entry("renew_sold", -20),
        entry("transaction", -650), entry("shipping_transaction", -39),
        entry("PAYMENT_PROCESSING_FEE", -325),
        entry("prolist", -295),
        entry("vat_seller_services", -40),
        entry("shipping_labels", -512),
        entry("PAYMENT_GROSS", 10000),
        entry("DISBURSE2", -9000),
        # A fee given back after a refund lowers its bucket...
        entry("transaction", 100, description="transaction fee refund"),
        # ...but money in that does not say so is not a fee credit.
        entry("transaction", 5000),
    ]
    out = profit.summarise_ledger(entries, "USD")
    assert out["buckets"] == {
        "listing": pytest.approx(0.40),
        "transaction": pytest.approx(6.50 + 0.39 - 1.00),
        "processing": pytest.approx(3.25),
        "ads": pytest.approx(2.95),
        "other": pytest.approx(0.40),
    }
    assert out["labels"] == pytest.approx(5.12)
    assert out["entries"] == len(entries)


def test_ledger_in_another_currency():
    entries = [entry("transaction", -1000, currency="EUR")]
    with pytest.raises(profit.CurrencyError):
        profit.summarise_ledger(entries, "USD")
    convert = profit.make_converter({"EUR": 50.0, "USD": 40.0})
    out = profit.summarise_ledger(entries, "USD", convert)
    assert out["buckets"]["transaction"] == pytest.approx(12.5)
    jpy = profit.summarise_ledger([entry("listing", -30, currency="JPY")], "JPY")
    assert jpy["buckets"]["listing"] == pytest.approx(30)


def test_fee_estimate():
    raw = {"items_revenue": 1000.0, "shipping_charged": 100.0, "gift_wrap": 0.0, "tax": 50.0,
           "orders": 40, "items_sold": 50}
    est = profit.estimate_fees(raw)
    assert est["listing"] == pytest.approx(10.0)
    assert est["transaction"] == pytest.approx(71.5)
    assert est["processing"] == pytest.approx(0.03 * 1150 + 10.0)
    assert est["ads"] == 0 and est["other"] == 0
    assert profit.estimate_fees(raw, usd=40.0)["listing"] == pytest.approx(400.0)


class StubClient:
    """Just the two reads fetch_month makes."""

    def __init__(self, receipts, ledger=None, ledger_error=None):
        self._receipts, self._ledger, self._error = receipts, ledger or [], ledger_error
        self.ledger_asked = False

    def receipts(self, **filters):
        assert filters["was_paid"] is True and filters["was_canceled"] is False
        yield from self._receipts

    def ledger_entries(self, start, end, *, max_items=None):
        self.ledger_asked = True
        if self._error is not None:
            raise self._error
        yield from self._ledger[:max_items]


def fetch(client, currency="USD", scopes=("transactions_r",), rates=None):
    return profit.fetch_month(client, "2026-08", currency=currency, scopes=scopes,
                              fx=lambda: {"rates": rates or {}})


def test_fetch_month_falls_back_to_an_estimate():
    sale = [receipt(1, 1, [(1000001, "Tee", 20.0, 1)])]
    ok = fetch(StubClient(sale, [entry("transaction", -130)]))
    assert ok["fees"]["source"] == "ledger" and ok["fees"]["buckets"]["transaction"] == 1.3
    assert ok["month"] == "2026-08" and ok["v"] == profit.RAW_VERSION and ok["orders"] == 1

    empty = fetch(StubClient(sale, [entry("PAYMENT_GROSS", 2000)]))
    assert empty["fees"]["reason"] == "no_fees"
    assert empty["fees"]["buckets"]["transaction"] == pytest.approx(1.3)

    missing = fetch(StubClient(sale, ledger_error=profit.EtsyApiError(404, "Not found")))
    assert missing["fees"]["reason"] == "unavailable" and missing["fees"]["status"] == 404

    no_scope_client = StubClient(sale)
    no_scope = fetch(no_scope_client, scopes=("listings_r",))
    assert no_scope["fees"]["reason"] == "scope" and not no_scope_client.ledger_asked

    other_currency = fetch(StubClient(sale, [entry("transaction", -130, currency="EUR")]))
    assert other_currency["fees"]["reason"] == "currency"

    with pytest.raises(profit.EtsyApiError):
        fetch(StubClient(sale, ledger_error=profit.EtsyApiError(0, "offline")))


def test_a_cut_off_ledger_is_not_trusted(monkeypatch):
    monkeypatch.setattr(profit, "MAX_LEDGER_ENTRIES", 2)
    sale = [receipt(1, 1, [(1000001, "Tee", 20.0, 1)])]
    out = fetch(StubClient(sale, [entry("transaction", -130)] * 3))
    assert out["fees"]["source"] == "estimate" and out["fees"]["reason"] == "too_many"


def test_estimate_fixed_fees_are_converted_for_a_lira_shop():
    sale = [receipt(1, 1, [(1000001, "Tee", 1000.0, 1)], currency="TRY")]
    out = fetch(StubClient(sale, ledger_error=profit.EtsyApiError(403, "nope")), currency="TRY",
                rates={"USD": 40.0})
    assert out["fees"]["buckets"]["listing"] == pytest.approx(8.0)  # $0.20 x 40


# ================================================================== the month's P&L


def raw_month(**overrides):
    receipts = [
        receipt(1, 10, [(1000001, "Retro Mountain Sunset Shirt", 30.0, 2)], shipping=5.0),
        receipt(2, 20, [(1000002, "Just One More Chapter Mug", 20.0, 1),
                        (1000004, "Cat Mom Club Tote Bag", 25.0, 1)]),
        receipt(3, 30, [(1000006, "Plain Black Tee", 20.0, 1)]),
    ]
    raw = {
        "v": profit.RAW_VERSION, "month": "2026-09", "fetched_at": 1.0, "currency": "USD",
        **profit.summarise_receipts(receipts, "USD"),
        "fees": {"source": "ledger", "reason": None, "status": None, "labels": 0.0,
                 "buckets": {"listing": 1.0, "transaction": 8.0, "processing": 5.0, "ads": 3.0,
                             "other": 0.0}},
    }
    raw.update(overrides)
    return raw


def test_compute_month_with_costs():
    costs = {"type:tshirt": 12.0, "type:mug": 8.0, "type:tote": 10.0, "listing:1000006": 25.0,
             "shipping:tshirt": 4.0, "shipping:mug": 5.0, "shipping:order": 3.0}
    out = profit.compute_month(raw_month(), costs)
    assert out["orders"] == 3
    assert out["revenue"] == pytest.approx(60 + 5 + 45 + 20)
    assert out["fees"]["total"] == pytest.approx(17.0)
    assert out["product_cost"] == pytest.approx(24 + 8 + 10 + 25)
    # Orders: shirt 4; mug+tote -> max(5 for mug; tote has none) = 5; tee -> tshirt 4.
    assert out["shipping_cost"] == pytest.approx(4 + 5 + 4)
    assert out["shipping_source"] == "costs"
    assert out["net"] == pytest.approx(130 - 17 - 67 - 13)
    assert out["margin"] == pytest.approx(out["net"] / 130, abs=1e-4)
    by_id = {p["listing_id"]: p for p in out["products"]}
    tee = by_id[1000006]
    assert tee["cost_source"] == "listing" and tee["net"] == pytest.approx(-5.0)
    assert tee["margin"] == pytest.approx(-0.25)
    assert by_id[1000001]["type"] == "tshirt" and by_id[1000001]["cost_source"] == "type"
    assert by_id[1000004]["type"] == "tote"


def test_compute_month_without_costs_leaves_net_open():
    out = profit.compute_month(raw_month(), {})
    assert out["product_cost"] is None and out["shipping_cost"] is None and out["net"] is None
    assert out["missing_cost_qty"] == 5
    assert all(p["cost"] is None and p["net"] is None for p in out["products"])


def test_missing_costs_are_counted():
    out = profit.compute_month(raw_month(), {"type:tshirt": 12.0, "shipping:order": 4.0})
    assert out["product_cost"] == pytest.approx(36.0)
    assert out["missing_cost_qty"] == 2  # the mug and the tote
    assert out["shipping_cost"] == pytest.approx(12.0)
    assert out["net"] is not None


def test_etsy_shipping_labels_replace_typed_shipping():
    raw = raw_month()
    raw["fees"]["labels"] = 21.5
    out = profit.compute_month(raw, {"type:tshirt": 1.0, "shipping:order": 100.0})
    assert out["shipping_cost"] == pytest.approx(21.5) and out["shipping_source"] == "labels"


def test_costs_in_lira_are_converted():
    convert = profit.make_converter({"USD": 40.0})
    out = profit.compute_month(raw_month(), {"currency": "TRY", "type:tshirt": 400.0,
                                             "shipping:order": 80.0}, convert=convert)
    shirt = next(p for p in out["products"] if p["listing_id"] == 1000001)
    assert shirt["unit_cost"] == pytest.approx(10.0) and shirt["unit_input"] == 400.0
    assert out["shipping_cost"] == pytest.approx(3 * 2.0)
    no_rate = profit.compute_month(raw_month(), {"currency": "TRY", "type:tshirt": 400.0})
    assert no_rate["product_cost"] is None


def test_product_type_from_titles():
    assert profit.product_type("Retro Mountain Sunset Shirt") == "tshirt"
    assert profit.product_type("Just One More Chapter Mug") == "mug"
    assert profit.product_type("Wildflower Botanical Print") == "poster"
    assert profit.product_type("Cat Mom Club Tote Bag") == "tote"
    assert profit.product_type("Desert Cactus Phone Case") == "phone_case"
    assert profit.product_type("Mr. Fox / Cozy Hoodie") == "hoodie"
    assert profit.product_type("") == "other"


# ================================================================== costs.json


def test_costs_storage_and_validation():
    assert profit.load_costs() == {}
    saved = profit.update_costs({"type:mug": 8.5, "listing:1000001": 12, "shipping:order": 4},
                                "USD")
    assert saved == {"type:mug": 8.5, "listing:1000001": 12.0, "shipping:order": 4.0,
                     "currency": "USD"}
    assert profit.load_costs() == saved
    profit.update_costs({"listing:1000001": None})
    assert "listing:1000001" not in profit.load_costs()
    for bad in ({"type:car": 1}, {"listing:abc": 1}, {"price:mug": 1}, {"type:mug": -1},
                {"type:mug": True}, {"type:mug": "9"}, {"type:mug": float("nan")},
                {"shipping:order": 2_000_000}):
        with pytest.raises(profit.ApiError) as err:
            profit.update_costs(bad)
        assert err.value.status == 422
    with pytest.raises(profit.ApiError):
        profit.update_costs({}, "dollars")


def test_unreadable_or_odd_costs_file_is_ignored():
    path = profit.costs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")
    assert profit.load_costs() == {}
    path.write_text(json.dumps({"type:mug": 5, "type:mug2": 3, "junk": 1, "listing:1": "x",
                                "currency": "usd"}), encoding="utf-8")
    assert profit.load_costs() == {"type:mug": 5.0}


# ================================================================== TCMB rates

TCMB_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<?xml-stylesheet type="text/xsl" href="isokur.xsl"?>
<Tarih_Date Tarih="11.09.2026" Date="09/11/2026" Bulten_No="2026/170">
  <Currency CrossOrder="0" Kod="USD" CurrencyCode="USD">
    <Unit>1</Unit><Isim>ABD DOLARI</Isim><CurrencyName>US DOLLAR</CurrencyName>
    <ForexBuying>34.0512</ForexBuying><ForexSelling>34.1234</ForexSelling>
  </Currency>
  <Currency CrossOrder="9" Kod="EUR" CurrencyCode="EUR">
    <Unit>1</Unit><ForexBuying>37.5</ForexBuying><ForexSelling>37.6543</ForexSelling>
  </Currency>
  <Currency CrossOrder="4" Kod="JPY" CurrencyCode="JPY">
    <Unit>100</Unit><ForexBuying>23.0</ForexBuying><ForexSelling>23.4567</ForexSelling>
  </Currency>
  <Currency CrossOrder="18" Kod="XDR" CurrencyCode="XDR">
    <Unit>1</Unit><ForexBuying>46.1</ForexBuying><ForexSelling/>
  </Currency>
</Tarih_Date>
"""


def test_parse_tcmb():
    out = profit.parse_tcmb(TCMB_SAMPLE.encode("utf-8"))
    assert out["date"] == "2026-09-11"
    assert out["rates"] == {"USD": 34.1234, "EUR": 37.6543, "JPY": pytest.approx(0.234567)}
    with pytest.raises(ValueError):
        profit.parse_tcmb(b"<html><body>maintenance</body></html>")
    with pytest.raises(ValueError):
        profit.parse_tcmb(b"not xml at all")


def test_fx_cache_manual_rate_and_pairs(monkeypatch):
    calls = []

    def fetch():
        calls.append(1)
        return profit.parse_tcmb(TCMB_SAMPLE)

    monkeypatch.setattr(profit, "_fetch_tcmb", fetch)
    fx = profit.get_fx()
    assert fx["rates"]["USD"] == 34.1234 and fx["error"] is None
    profit.get_fx()
    assert len(calls) == 1  # once per day
    pair = profit.fx_pair(fx, "USD")
    assert pair["base"] == "USD" and pair["quote"] == "TRY" and pair["rate"] == 34.1234
    assert pair["source"] == "tcmb" and pair["stale"] is False and pair["shop_is_quote"] is False
    lira_shop = profit.fx_pair(fx, "TRY")
    assert lira_shop["base"] == "USD" and lira_shop["shop_is_quote"] is True
    assert profit.fx_pair(fx, "GBP") is None
    fx = profit.set_manual_rate("USD", 35.0)
    assert profit.fx_pair(fx, "USD")["source"] == "manual"
    assert profit.fx_pair(fx, "USD")["rate"] == 35.0
    fx = profit.set_manual_rate("USD", None)
    assert profit.fx_pair(fx, "USD")["source"] == "tcmb"


def test_fx_offline_falls_back_to_the_cache(monkeypatch):
    stale = {"rates": {"USD": 33.0}, "date": "2026-09-01", "fetched_on": "2026-09-01"}
    profit.fx_path().parent.mkdir(parents=True, exist_ok=True)
    profit.fx_path().write_text(json.dumps(stale), encoding="utf-8")
    fx = profit.get_fx()  # the autouse fixture makes the fetch fail
    assert fx["error"] == "offline" and fx["rates"]["USD"] == 33.0
    assert profit.fx_pair(fx, "USD")["stale"] is True
    # A failure is not retried on every call.
    monkeypatch.setattr(profit, "_fetch_tcmb", lambda: pytest.fail("fetched again"))
    assert profit.get_fx()["error"] == "offline"


# ================================================================== the API


def fake_shop(web, receipts, ledger=None, *, fake=None):
    fake = fake or FakeEtsy()
    fake.add("GET", RECEIPTS_PATH, paged(receipts))
    fake.add("GET", LEDGER_PATH, paged(ledger or []))
    fake.add("GET", "/listings/batch", {"count": 1, "results": [
        {"listing_id": 1000001, "images": [
            {"rank": 1, "url_170x135": "https://i.etsystatic.com/example/il_170x135.jpg"}]},
    ]})
    use_fake_etsy(web, fake)
    return fake


def month_data(month=None):
    month = month or profit.current_month()
    start, end = profit.month_range(month)
    t = start + 3600
    receipts = [
        receipt(3021000001, t, [(1000001, "Retro Mountain Sunset Shirt", 30.0, 2)], shipping=5.0),
        receipt(3021000002, t + 60, [(1000002, "Just One More Chapter Mug", 20.0, 1)]),
    ]
    ledger = [
        entry("transaction", -390, t), entry("transaction", -130, t + 60),
        entry("renew_sold", -40, t), entry("renew_sold", -20, t + 60),
        entry("PAYMENT_PROCESSING_FEE", -220, t), entry("PAYMENT_PROCESSING_FEE", -85, t + 60),
        entry("prolist", -300, t + 120), entry("PAYMENT_GROSS", 6500, t),
    ]
    return receipts, ledger


def load_ready(web, month=None, **params):
    query = {"month": month} if month else {}
    query.update(params)
    data = web.client.get("/api/profit", params=query).json()
    if data.get("state") == "loading":
        final = wait_for_job(web, data["job"]["id"], timeout=20)
        assert final["status"] == "done", final
        data = web.client.get("/api/profit", params=query).json()
    assert data["state"] == "ready", data
    return data


def test_needs_keys_then_a_connection(web):
    resp = web.client.get("/api/profit")
    assert resp.status_code == 409
    assert resp.json()["error"]["params"] == {"step": "keys"}
    use_fake_etsy(web, connected=False)
    assert web.client.get("/api/profit").json()["error"]["params"] == {"step": "connect"}


def test_a_month_that_cannot_be_shown_opens_this_month_instead(web):
    # An old bookmark (?month=2019-01) is not a dead end: the latest month is served and
    # the page is told which month it could not show (it says so, translated).
    receipts, ledger = month_data()
    fake_shop(web, receipts, ledger)
    now = profit.current_month()
    future = profit.shift_month(now, 1)
    ancient = profit.shift_month(now, -30)
    for bad in ("2026-13", "september", future, ancient, "2099-13"):
        data = load_ready(web, bad)
        assert data["month"] == now and data["month_refused"] == bad, bad
        assert data["summary"]["month"] == now and data["summary"]["orders"] == 2
    assert load_ready(web)["month_refused"] is None
    assert load_ready(web, now)["month_refused"] is None
    long_value = "x" * 500
    assert load_ready(web, long_value)["month_refused"] == "x" * profit.MAX_REFUSED_MONTH
    # The costs endpoint is only called with a month the page already holds: still strict.
    for bad in ("2026-13", future, ancient):
        resp = web.client.get("/api/profit/costs", params={"month": bad})
        assert resp.status_code == 422, bad
        assert resp.json()["error"]["code"] == "invalid"


def test_profit_is_fetched_in_a_job_then_served_from_the_cache(web):
    receipts, ledger = month_data()
    fake = fake_shop(web, receipts, ledger)
    first = web.client.get("/api/profit").json()
    assert first["state"] == "loading" and first["job"]["kind"] == "profit"
    assert first["job"]["title_key"] == "profit:job.title"
    data = load_ready(web)
    s = data["summary"]
    assert data["month"] == profit.current_month() and data["currency"] == "USD"
    assert s["orders"] == 2 and s["items_sold"] == 3
    assert s["revenue"] == pytest.approx(60 + 5 + 20)
    assert s["fees"]["source"] == "ledger"
    assert s["fees"]["buckets"] == {"listing": 0.6, "transaction": 5.2, "processing": 3.05,
                                    "ads": 3.0, "other": 0.0}
    assert s["fees"]["total"] == pytest.approx(11.85)
    assert s["net"] is None  # no costs entered yet
    assert len(data["series"]) == 6 and data["series"][-1]["month"] == data["month"]
    assert len(data["months"]) == 12 and data["months"][0] == profit.current_month()
    shirt = next(p for p in s["products"] if p["listing_id"] == 1000001)
    assert shirt["image"] == "https://i.etsystatic.com/example/il_170x135.jpg"
    assert data["refreshing"] is None

    receipt_calls = [r for r in fake.requests if r.url.path.endswith(RECEIPTS_PATH)]
    assert len(receipt_calls) == 6  # one per chart month
    q = parse_qs(receipt_calls[0].url.query.decode())
    assert q["was_paid"] == ["true"] and q["was_canceled"] == ["false"]
    start, end = profit.month_range(profit.current_month())
    assert q["min_created"] == [str(start)] and q["max_created"] == [str(end)]
    ledger_q = parse_qs(
        next(r for r in fake.requests if r.url.path.endswith(LEDGER_PATH)).url.query.decode()
    )
    assert ledger_q["min_created"] == [str(start)] and ledger_q["max_created"] == [str(end)]

    # Entering costs recomputes from the cache: no Etsy call.
    calls = len(fake.calls)
    resp = web.client.post("/api/profit/costs", json={
        "set": {"type:tshirt": 12, "type:mug": 8, "shipping:order": 4}, "currency": "USD"})
    assert resp.status_code == 200 and resp.json()["configured"] == {"product": True,
                                                                     "shipping": True}
    s = web.client.get("/api/profit").json()["summary"]
    assert len(fake.calls) == calls
    assert s["product_cost"] == pytest.approx(32.0)
    assert s["shipping_cost"] == pytest.approx(8.0)
    assert s["net"] == pytest.approx(85 - 11.85 - 32 - 8)


def test_ledger_refused_gives_an_estimate(web):
    receipts, _ledger = month_data()
    fake = fake_shop(web, receipts)
    fake.error("GET", LEDGER_PATH, 403, "Access to this resource is restricted")
    s = load_ready(web)["summary"]
    assert s["fees"]["source"] == "estimate"
    assert s["fees"]["reason"] == "forbidden" and s["fees"]["status"] == 403
    assert s["fees"]["buckets"]["listing"] == pytest.approx(0.60)
    assert s["fees"]["buckets"]["transaction"] == pytest.approx(0.065 * 85, abs=0.01)


def test_without_the_orders_scope_the_ledger_is_not_asked(web):
    receipts, ledger = month_data()
    fake = fake_shop(web, receipts, ledger)
    auth.save_token(auth.Token(access_token="7654321.exampleaccesstoken",
                               refresh_token="7654321.examplerefreshtoken",
                               expires_at=time.time() + 3600, scopes=("shops_r", "listings_r")))
    web.ctx.reset_client()
    s = load_ready(web)["summary"]
    assert s["fees"]["source"] == "estimate" and s["fees"]["reason"] == "scope"
    assert not any(path == LEDGER_PATH for _m, path in fake.calls)


def test_a_failed_fetch_is_shown_and_retried_only_on_request(web):
    fake = FakeEtsy()
    fake.error("GET", RECEIPTS_PATH, 400, "bad request")
    use_fake_etsy(web, fake)
    first = web.client.get("/api/profit").json()
    final = wait_for_job(web, first["job"]["id"])
    assert final["status"] == "error" and final["error"]["code"] == "etsy_error"
    again = web.client.get("/api/profit").json()
    assert again["state"] == "loading" and again["job"]["id"] == first["job"]["id"]
    retry = web.client.get("/api/profit", params={"refresh": 1}).json()
    assert retry["job"]["id"] != first["job"]["id"]
    wait_for_job(web, retry["job"]["id"])


def test_stale_months_are_shown_while_a_job_refreshes_them(web):
    receipts, ledger = month_data()
    fake = fake_shop(web, receipts, ledger)
    load_ready(web)
    shop_key = str(web.ctx.status["shop"]["etsy_shop_id"] or ETSY_SHOP_ID)
    month = profit.current_month()
    raw = profit.load_raw(shop_key, month)
    raw["fetched_at"] -= 3600
    profit.save_raw(shop_key, raw)
    before = len(fake.calls)
    data = web.client.get("/api/profit").json()
    assert data["state"] == "ready" and data["refreshing"]["kind"] == "profit"
    assert data["refreshing"]["params"]["months"] == [month]
    assert wait_for_job(web, data["refreshing"]["id"])["status"] == "done"
    assert len(fake.calls) > before
    assert web.client.get("/api/profit").json()["refreshing"] is None


def test_an_earlier_month(web):
    month = profit.shift_month(profit.current_month(), -3)
    receipts, ledger = month_data(month)
    fake_shop(web, receipts, ledger)
    data = load_ready(web, month)
    assert data["month"] == month and data["summary"]["orders"] == 2
    assert data["series"][-1]["month"] == month and data["series"][-1]["orders"] == 2
    assert data["previous"]["month"] == profit.shift_month(month, -1)


def test_costs_endpoints(web):
    receipts, ledger = month_data()
    fake_shop(web, receipts, ledger)
    load_ready(web)
    info = web.client.get("/api/profit/costs").json()
    assert info["costs"] == {} and info["shop_currency"] == "USD"
    assert [t["id"] for t in info["types"]] == ["tshirt", "mug"]
    assert [p["listing_id"] for p in info["products"]] == [1000001, 1000002]
    resp = web.client.post("/api/profit/costs", json={"set": {"listing:1000001": 13.5}})
    assert resp.status_code == 200
    info = web.client.get("/api/profit/costs").json()
    assert info["products"][0]["unit"] == 13.5
    bad = web.client.post("/api/profit/costs", json={"set": {"type:spaceship": 1}})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid"
    bad = web.client.post("/api/profit/costs", json={"set": ["type:mug"]})
    assert bad.status_code == 422
    assert web.client.post("/api/profit/costs", content=b"[1]").status_code == 400
    saved = json.loads(profit.costs_path().read_text(encoding="utf-8"))
    assert saved == {"listing:1000001": 13.5}


def test_fx_endpoints(web, monkeypatch):
    monkeypatch.setattr(profit, "_fetch_tcmb", lambda: profit.parse_tcmb(TCMB_SAMPLE))
    use_fake_etsy(web)
    got = web.client.get("/api/profit/fx").json()
    assert got["pair"]["rate"] == 34.1234 and got["pair"]["quote"] == "TRY"
    manual = web.client.post("/api/profit/fx", json={"rate": 35.5}).json()
    assert manual["pair"]["source"] == "manual" and manual["pair"]["rate"] == 35.5
    assert manual["manual"] is True
    cleared = web.client.post("/api/profit/fx", json={"rate": None}).json()
    assert cleared["pair"]["source"] == "tcmb"
    for bad in ({"rate": -1}, {"rate": "34"}, {"rate": True}, {}):
        resp = web.client.post("/api/profit/fx", json=bad)
        assert resp.status_code == 422, bad
    refreshed = web.client.post("/api/profit/fx", json={"refresh": True}).json()
    assert refreshed["error"] is None


def test_display_preference(web):
    receipts, ledger = month_data()
    fake_shop(web, receipts, ledger)
    assert web.client.post("/api/profit/prefs", json={"display": "secondary"}).json() == {
        "display": "secondary"}
    assert load_ready(web)["display"] == "secondary"
    assert web.client.post("/api/profit/prefs", json={"display": "lira"}).status_code == 422


# ================================================================== review fixes


def test_a_refund_gives_back_its_tax_share_too():
    # Items 20 + shipping 5, tax 2: the buyer paid 27. A refund pays back tax as well,
    # so only its revenue share comes off revenue, and its tax share off the tax.
    full = receipt(1, 1, [(1000001, "Tee", 20.0, 1)], shipping=5.0, tax=2.0, refunds=[27.0],
                   status="partially refunded")
    out = profit.summarise_receipts([full], "USD")
    assert out["refunds"] == pytest.approx(25.0) and out["tax"] == pytest.approx(0.0)
    month = profit.compute_month({**out, "month": "2026-08", "currency": "USD"}, {})
    assert month["revenue"] == pytest.approx(0.0)  # was -2: the refunded tax came off revenue

    half = receipt(2, 2, [(1000001, "Tee", 20.0, 1)], shipping=5.0, tax=2.0, refunds=[13.5])
    out = profit.summarise_receipts([half], "USD")
    assert out["refunds"] == pytest.approx(12.5) and out["tax"] == pytest.approx(1.0)

    untaxed = receipt(3, 3, [(1000001, "Tee", 20.0, 1)], refunds=[5.0])
    assert profit.summarise_receipts([untaxed], "USD")["refunds"] == pytest.approx(5.0)


def test_a_fully_refunded_order_is_nothing_whatever_its_refunds_say():
    gone = receipt(1, 1, [(1000001, "Tee", 20.0, 1)], shipping=5.0, tax=2.0,
                   status="fully refunded")  # Etsy sent no refunds list
    gone["gift_wrap_price"] = money(3.0)
    out = profit.summarise_receipts([gone], "USD")
    assert out["orders"] == 1
    assert out["refunds"] == pytest.approx(28.0) and out["tax"] == pytest.approx(0.0)
    month = profit.compute_month({**out, "month": "2026-08", "currency": "USD"}, {})
    assert month["revenue"] == pytest.approx(0.0)
    money_ = profit.receipt_money(gone)
    assert money_["revenue"] == 0 and money_["refunded_tax"] == pytest.approx(2.0)


def test_failed_refunds_and_over_refunds_are_bounded():
    failed = receipt(1, 1, [(1000001, "Tee", 20.0, 1)], refunds=[20.0])
    failed["refunds"][0]["status"] = "failed"
    assert profit.receipt_money(failed)["revenue"] == pytest.approx(20.0)
    over = receipt(2, 2, [(1000001, "Tee", 20.0, 1)], refunds=[50.0])
    assert profit.receipt_money(over)["revenue"] == pytest.approx(0.0)
    assert profit.receipt_money({"status": "canceled", "subtotal": money(9.0)})["revenue"] == 0


class CountingClient(StubClient):
    """Like Etsy: stops at max_items and remembers what was asked for."""

    def __init__(self, receipts, ledger=None):
        super().__init__(receipts, ledger)
        self.asked: dict[str, int | None] = {}

    def receipts(self, **filters):
        self.asked["receipts"] = filters.get("max_items")
        yield from self._receipts[:filters.get("max_items")]

    def ledger_entries(self, start, end, *, max_items=None):
        self.asked["ledger"] = max_items
        yield from self._ledger[:max_items]


def test_a_month_with_more_orders_than_can_be_read_says_so(monkeypatch):
    monkeypatch.setattr(profit, "MAX_RECEIPTS", 2)
    sales = [receipt(n, n, [(1000001, "Tee", 10.0, 1)]) for n in range(1, 4)]
    client = CountingClient(sales, [entry("transaction", -130)])
    raw = fetch(client)
    assert client.asked["receipts"] == 3  # one more than the cap, to know it was cut
    assert raw["orders"] == 2 and raw["orders_truncated"] is True
    assert profit.compute_month(raw, {})["partial"] is True
    assert profit.compute_month(raw, {})["partial_limit"] == 2  # the page's note says how many
    whole = fetch(CountingClient(sales[:2], [entry("transaction", -130)]))
    assert whole["orders_truncated"] is False and profit.compute_month(whole, {})["partial"] is False
    assert profit.compute_month(whole, {})["partial_limit"] is None


def test_a_ledger_of_exactly_the_cap_is_still_trusted(monkeypatch):
    monkeypatch.setattr(profit, "MAX_LEDGER_ENTRIES", 3)
    sale = [receipt(1, 1, [(1000001, "Tee", 20.0, 1)])]
    client = CountingClient(sale, [entry("transaction", -100)] * 3)
    out = fetch(client)
    assert client.asked["ledger"] == 4
    assert out["fees"]["source"] == "ledger" and out["fees"]["buckets"]["transaction"] == 3.0


def test_month_files_of_the_old_formula_are_read_again():
    assert profit.RAW_VERSION >= 2
