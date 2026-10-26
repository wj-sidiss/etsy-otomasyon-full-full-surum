"""Panel: setup steps, recent activity and the six numbers read from (a fake) Etsy."""

from __future__ import annotations

import time
from datetime import datetime

import httpx
import pytest
from web_helpers import ETSY_SHOP_ID, use_fake_etsy

from etsyotomasyon import seo
from etsyotomasyon.client import EtsyClient
from etsyotomasyon.web.api import dashboard

LISTINGS_PATH = f"/shops/{ETSY_SHOP_ID}/listings"
RECEIPTS_PATH = f"/shops/{ETSY_SHOP_ID}/receipts"

GOOD = {
    "listing_id": 1000001,
    "state": "active",
    "title": "Retro Mountain Sunset Shirt, Vintage Hiking T-Shirt, Nature Lover Gift",
    "description": "A soft vintage hiking shirt with a retro mountain sunset. " * 5,
    "tags": ["retro sunset shirt", "mountain tshirt", "hiking shirt", "nature lover gift",
             "camping tee", "outdoor shirt", "vintage mountain", "adventure shirt",
             "hiker gift", "national park tee", "sunset tshirt", "gift for hiker", "wanderlust shirt"],
    "materials": ["cotton"],
}
POOR = {"listing_id": 1000002, "state": "active", "title": "Mug", "description": "", "tags": []}


def _usd(cents):
    return {"amount": cents, "divisor": 100, "currency_code": "USD"}


RECEIPTS = [
    {"receipt_id": 3000001, "status": "paid", "subtotal": _usd(2000), "total_shipping_cost": _usd(500)},
    {"receipt_id": 3000002, "status": "partially refunded", "subtotal": _usd(1850),
     "total_shipping_cost": _usd(0), "refunds": [{"amount": _usd(350)}]},
    {"receipt_id": 3000003, "status": "fully refunded", "subtotal": _usd(9900),
     "total_shipping_cost": _usd(0)},
]


def _listings(request: httpx.Request):
    params = request.url.params
    if params.get("state") == "draft":
        return httpx.Response(200, json={"count": 50, "results": []},
                              headers={"x-remaining-today": "9876"})
    if params.get("limit") == "1":
        return {"count": 306, "results": [GOOD]}
    return {"count": 2, "results": [GOOD, POOR]}


def _receipts(request: httpx.Request):
    params = request.url.params
    if params.get("limit") == "1":
        assert params.get("was_paid") == "true" and params.get("was_shipped") == "false"
        assert params.get("was_canceled") == "false"
        return {"count": 24, "results": []}
    assert int(params["min_created"]) == int(dashboard.month_start().timestamp())
    return {"count": len(RECEIPTS), "results": RECEIPTS}


@pytest.fixture
def no_backoff(monkeypatch):
    monkeypatch.setattr(EtsyClient, "_backoff", staticmethod(lambda attempt: 0.0))


def _fake(web):
    fake = use_fake_etsy(web)
    fake.add("GET", LISTINGS_PATH, _listings)
    fake.add("GET", RECEIPTS_PATH, _receipts)
    return fake


def test_without_keys_the_panel_asks_for_the_first_step(web):
    web.ctx.refresh_status(force=True)
    data = web.client.get("/api/dashboard").json()
    assert data["setup"]["current"] == "account"
    assert [s["state"] for s in data["setup"]["steps"]] == ["current", "todo", "todo", "todo"]
    assert data["setup"]["complete"] is False
    assert data["stats"] is None
    assert data["pinterest"] == {"pending": 0, "due": 0, "attention": 0}

    stats = web.client.get("/api/dashboard/stats").json()
    assert stats["available"] is False
    assert stats["error"]["code"] == "setup_needed"
    assert stats["error"]["params"] == {"step": "keys"}
    assert all(stat["value"] is None for stat in stats["stats"].values())


def test_keys_without_a_sign_in_ask_to_connect(web):
    use_fake_etsy(web, connected=False)
    web.ctx.refresh_status(force=True)
    data = web.client.get("/api/dashboard").json()
    assert [s["state"] for s in data["setup"]["steps"]] == ["done", "current", "todo", "todo"]
    stats = web.client.get("/api/dashboard/stats").json()
    assert stats["error"]["params"] == {"step": "connect"}


@pytest.mark.parametrize("status, expected", [
    ({"state": "checking", "setup": {}}, ["current", "todo", "todo", "todo"]),
    ({"state": "bad_keys", "setup": {"keys": True}}, ["current", "todo", "todo", "todo"]),
    ({"state": "reconnect", "setup": {"keys": True, "connected": True}},
     ["done", "current", "todo", "todo"]),
    ({"state": "connected", "setup": {"keys": True, "connected": True, "mockups": 0}},
     ["done", "done", "current", "todo"]),
    ({"state": "offline", "setup": {"keys": True, "connected": True, "mockups": 6}},
     ["done", "done", "done", "current"]),
    ({"state": "connected", "setup": {"keys": True, "connected": True, "mockups": 6, "template": True}},
     ["done", "done", "done", "done"]),
])
def test_setup_steps_follow_the_status(status, expected):
    steps = dashboard.setup_steps(status)
    assert [s["state"] for s in steps["steps"]] == expected
    assert [s["id"] for s in steps["steps"]] == ["account", "shop", "mockups", "designs"]
    assert steps["complete"] is (expected == ["done"] * 4)


def test_the_six_numbers(web):
    fake = _fake(web)
    web.ctx.refresh_status(force=True)
    data = web.client.get("/api/dashboard/stats").json()
    assert data["available"] is True and data["error"] is None
    stats = data["stats"]
    assert stats["active"]["value"] == 306
    assert stats["draft"]["value"] == 50
    assert stats["to_ship"]["value"] == 24
    scores = [seo.audit_listing(GOOD).score, seo.audit_listing(POOR).score]
    assert stats["seo"]["value"] == round(sum(scores) / 2)
    assert stats["seo"]["sample"] == 2 and stats["seo"]["low"] == 1
    revenue = stats["revenue"]
    # 20 + 5 shipping, 18.50 - 3.50 refunded; the fully refunded order counts as 0, as on
    # Kâr-Zarar (it is still one of the month's orders).
    assert revenue["value"] == 40.0
    assert revenue["orders"] == 3 and revenue["currency"] == "USD"
    assert revenue["partial"] is False
    assert revenue["month"] == datetime.now().strftime("%Y-%m")
    assert stats["quota"]["value"] == 9876
    assert all(stats[name]["error"] is None for name in stats)
    # The SEO sample is one page of active listings, not the whole shop.
    sample = [r for r in fake.requests if r.url.path.endswith("/listings")
              and r.url.params.get("limit") == "100"]
    assert len(sample) == 1 and sample[0].url.params.get("state") == "active"


def test_the_numbers_are_cached_until_asked_again(web):
    fake = _fake(web)
    web.client.get("/api/dashboard/stats")
    calls = len(fake.calls)
    again = web.client.get("/api/dashboard/stats").json()
    assert len(fake.calls) == calls
    assert again["stats"]["active"]["value"] == 306 and again["stats"]["active"]["cached_at"]
    # The fast endpoint shows the last numbers without asking Etsy.
    overview = web.client.get("/api/dashboard").json()
    assert overview["stats"]["draft"]["value"] == 50
    assert len(fake.calls) == calls
    web.client.get("/api/dashboard/stats", params={"refresh": 1})
    assert len(fake.calls) > calls


def test_one_failing_number_does_not_hide_the_others(web, no_backoff):
    fake = _fake(web)
    fake.error("GET", RECEIPTS_PATH, 500, "server error")
    data = web.client.get("/api/dashboard/stats").json()
    stats = data["stats"]
    assert stats["active"]["value"] == 306 and stats["active"]["error"] is None
    assert stats["to_ship"]["value"] is None
    assert stats["to_ship"]["error"]["code"] == "etsy_error"
    assert stats["to_ship"]["error"]["params"] == {"status": 500}
    assert stats["revenue"]["error"]["code"] == "etsy_error"
    assert data["error"] is None  # not everything failed


def test_offline_every_number_says_so(web, no_backoff):
    fake = _fake(web)
    fake.offline = True
    started = time.monotonic()
    data = web.client.get("/api/dashboard/stats").json()
    assert time.monotonic() - started < 10
    assert data["error"]["code"] == "offline"
    for name in ("active", "draft", "to_ship", "seo", "revenue"):
        assert data["stats"][name]["error"]["code"] == "offline"


def test_a_known_offline_shop_answers_at_once(web, no_backoff):
    fake = _fake(web)
    web.client.get("/api/dashboard/stats")  # something to keep from before
    fake.offline = True
    assert web.ctx.refresh_status(force=True)["state"] == "offline"
    calls = len(fake.calls)
    data = web.client.get("/api/dashboard/stats").json()
    assert len(fake.calls) == calls  # Etsy was not asked again
    assert data["error"]["code"] == "offline"
    assert data["stats"]["active"]["value"] == 306  # the last numbers stay on screen
    retried = web.client.get("/api/dashboard/stats", params={"refresh": 1}).json()
    assert len(fake.calls) > calls
    assert retried["stats"]["active"]["error"]["code"] == "offline"


def test_recent_activity_shows_running_jobs_then_notifications(web):
    import threading

    web.ctx.notify("orders", "notify.shipped", {"n": 3}, tone="success", link="/siparisler")
    release = threading.Event()
    job = web.ctx.jobs.start("designs", "designs:job.title", lambda job: release.wait(5), params={"n": 4})
    try:
        deadline = time.monotonic() + 5
        while job.status != "running" and time.monotonic() < deadline:
            time.sleep(0.01)
        recent = web.client.get("/api/dashboard").json()["recent"]
    finally:
        release.set()
    assert recent[0]["type"] == "job" and recent[0]["title_key"] == "designs:job.title"
    assert recent[0]["status"] == "running"
    assert recent[1]["type"] == "notification" and recent[1]["key"] == "notify.shipped"
    assert recent[1]["params"] == {"n": 3}


def test_the_pinterest_line_counts_the_local_queue(web):
    from etsyotomasyon import pinterest

    queue = pinterest.Queue.load()
    pins = [{"listing_id": 1000001, "rank": r, "payload": {"board_id": "900", "title": "t"}}
            for r in (1, 2, 3)]
    queue.add(pins, start=datetime.now().date(), per_day=2)
    queue.entries[2]["status"] = "uncertain"
    queue.save()
    assert web.client.get("/api/dashboard").json()["pinterest"] == {
        "pending": 2, "due": 2, "attention": 1,
    }


def test_revenue_of_one_receipt():
    assert dashboard.receipt_revenue(RECEIPTS[0]) == (25.0, "USD")
    assert dashboard.receipt_revenue(RECEIPTS[1]) == (15.0, "USD")
    assert dashboard.receipt_revenue(RECEIPTS[2]) == (0.0, "USD")
    assert dashboard.receipt_revenue({"status": "canceled", "subtotal": RECEIPTS[0]["subtotal"]}) == (
        0.0, None)
    assert dashboard.receipt_revenue({"status": "paid"}) == (0.0, None)


def test_the_month_starts_at_local_midnight_on_the_first():
    assert dashboard.month_start(datetime(2026, 9, 26, 14, 5, 7)) == datetime(2026, 9, 1)


def test_the_panel_and_kar_zarar_agree_on_a_months_revenue():
    # Gift wrap, a taxed order refunded in full, a partial refund, a failed refund and a
    # fully refunded order: the Panel tile and Kâr-Zarar must show the same number.
    from etsyotomasyon.web.api import profit

    def receipt(n, status, *, items, shipping=0, gift=0, tax=0, refunds=()):
        return {
            "receipt_id": 3000100 + n, "status": status,
            "subtotal": _usd(items), "total_shipping_cost": _usd(shipping),
            "gift_wrap_price": _usd(gift), "total_tax_cost": _usd(tax), "total_vat_cost": _usd(0),
            "refunds": [{"amount": _usd(cents), "status": state} for cents, state in refunds],
            "transactions": [{"listing_id": 1000001, "title": "Example Mug", "quantity": 1,
                              "price": _usd(items)}],
        }

    receipts = [
        receipt(1, "paid", items=2000, shipping=500, gift=300),
        receipt(2, "partially refunded", items=2000, shipping=500, tax=200,
                refunds=[(2700, "succeeded")]),
        receipt(3, "partially refunded", items=4000, tax=400, refunds=[(1100, "succeeded")]),
        receipt(4, "paid", items=1500, refunds=[(1500, "failed")]),
        receipt(5, "fully refunded", items=9900, tax=800),
    ]
    panel = sum(dashboard.receipt_revenue(r)[0] for r in receipts)
    raw = {**profit.summarise_receipts(receipts, "USD"), "month": "2026-09", "currency": "USD"}
    kar_zarar = profit.compute_month(raw, {})["revenue"]
    assert panel == pytest.approx(kar_zarar)
    # 28 (gift wrap counts) + 0 (the refund took its tax share back) + 30 + 15 + 0
    assert kar_zarar == pytest.approx(28 + 0 + 30 + 15 + 0)
