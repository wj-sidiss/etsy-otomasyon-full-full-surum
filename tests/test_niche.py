"""Niche finder: the numbers from Etsy's public search, and the watch list."""

from __future__ import annotations

import httpx
from web_helpers import use_fake_etsy

from etsyotomasyon import niche

NOW = 1_800_000_000.0
DAY = 86400


def listing(n, *, age_days, views, favorers=5, shop=None, tags=("peel and stick wallpaper",),
            title="Sage Green Floral Wallpaper Peel and Stick", price=2400):
    return {"listing_id": 5000 + n, "title": f"{title} {n}", "url": f"https://etsy.test/{n}",
            "shop_id": shop if shop is not None else 100 + n, "views": views,
            "num_favorers": favorers, "original_creation_timestamp": NOW - age_days * DAY,
            "tags": list(tags), "listing_type": "physical",
            "price": {"amount": price, "divisor": 100, "currency_code": "USD"}}


def test_the_opportunity_grows_with_demand_and_falls_with_competition():
    quiet = niche.opportunity(0.5, 500_000)
    busy = niche.opportunity(40, 500_000)
    open_field = niche.opportunity(40, 2_000)
    assert quiet["score"] < busy["score"] < open_field["score"]
    assert niche.opportunity(None, 0)["score"] == 0
    assert 0 <= niche.opportunity(10_000, 10_000_000)["score"] <= 100
    assert open_field["level"] == "high" and quiet["level"] == "low"


def test_the_analysis_reports_demand_competition_trends_and_the_new_entrants():
    ranked = [listing(i, age_days=400, views=400 + i * 10, shop=1 if i < 3 else 100 + i)
              for i in range(10)]
    newest = [listing(50, age_days=20, views=900, favorers=80),
              listing(51, age_days=60, views=300), listing(52, age_days=300, views=9000)]
    result = niche.analyze("peel and stick wallpaper", ranked, newest, 12_345, now=NOW)
    assert result["sampled"] == 10 and result["total"] == 12_345
    assert result["demand"]["views_per_day"] == 1.11  # the median of 1.0 ... 1.22 a day
    assert result["competition"]["top_shops_share"] == 0.5  # the top three shops hold 3 + 1 + 1 of 10
    assert result["price"] == {"currency": "USD", "min": 24.0, "median": 24.0, "max": 24.0, "sample": 10}
    # a listing under 120 days old that already gets views is "rising"; an old one is not
    assert [r["id"] for r in result["rising"]] == [5050, 5051]
    assert result["rising"][0]["views_per_day"] == 45.0 and result["rising"][0]["age_days"] == 20
    assert result["trending"][0]["views_per_day"] >= result["trending"][-1]["views_per_day"]
    assert result["tags"][0] == {"tag": "peel and stick wallpaper", "count": 10}
    assert result["phrases"] and result["opportunity"]["level"] in ("low", "mid", "high")


def test_an_empty_sample_is_an_empty_analysis_not_an_error():
    result = niche.analyze("nothing here", [], [], 0, now=NOW)
    assert result["sampled"] == 0 and result["opportunity"]["score"] == 0
    assert result["price"]["median"] is None and result["trending"] == [] and result["rising"] == []


def test_the_watch_list_keeps_snapshots_and_shows_the_change():
    item = niche.row(listing(1, age_days=30, views=100, favorers=10), NOW)
    niche.watch_add(item, now=NOW)
    niche.watch_add(item, now=NOW + 60)  # the same listing again is still one, one snapshot
    assert len(niche.watch_load()["items"]) == 1
    fresh = [dict(listing(1, age_days=30, views=250, favorers=22), listing_id=item["id"])]
    assert niche.watch_refresh(fresh, now=NOW + 3600) == 0  # too soon after the last snapshot
    assert niche.watch_refresh(fresh, now=NOW + 8 * DAY) == 1
    [view] = niche.watch_view(now=NOW + 8 * DAY)
    assert (view["favorers"], view["views"]) == (22, 250)
    assert (view["favorers_change"], view["views_change"]) == (12, 150)
    assert view["views_per_day"] == round(150 / 8, 1) and len(view["history"]) == 2
    assert niche.watch_remove(item["id"]) is True and niche.watch_remove(item["id"]) is False
    assert niche.watch_view() == []


def test_a_watch_list_has_a_limit_and_wants_an_id(monkeypatch):
    import pytest
    with pytest.raises(ValueError):
        niche.watch_add({"title": "no id"})
    monkeypatch.setattr(niche, "MAX_WATCHED", 1)
    niche.watch_add(niche.row(listing(1, age_days=5, views=1), NOW), now=NOW)
    with pytest.raises(ValueError):
        niche.watch_add(niche.row(listing(2, age_days=5, views=1), NOW), now=NOW)


def _search(rows, total=None):
    def respond(request: httpx.Request):
        q = request.url.params
        limit, offset = int(q.get("limit", 25)), int(q.get("offset", 0))
        pool = rows
        if q.get("sort_on") == "created":
            pool = sorted(rows, key=lambda r: -r["original_creation_timestamp"])
        return {"count": total or len(rows), "results": pool[offset:offset + limit]}
    return respond


def test_the_niche_api_analyses_caches_and_refreshes(web):
    import time
    now = time.time()
    rows = []
    for i in range(40):
        item = listing(i, age_days=200, views=300 + i * 7)
        item["original_creation_timestamp"] = now - (30 if i % 3 == 0 else 200) * DAY
        rows.append(item)
    fake = use_fake_etsy(web, connected=False)
    fake.add("GET", "/listings/active", _search(rows, total=88_000))
    data = web.client.get("/api/niche", params={"keyword": "  Peel And STICK  Wallpaper "}).json()
    assert data["keyword"] == "peel and stick wallpaper" and data["total"] == 88_000
    assert data["sampled"] == 40 and data["cached"] is False and data["tags"]
    assert data["rising"] and all(r["age_days"] <= 120 for r in data["rising"])
    calls = len([r for r in fake.requests if r.url.path.endswith("/listings/active")])
    again = web.client.get("/api/niche", params={"keyword": "peel and stick wallpaper"}).json()
    assert again["cached"] is True
    assert len([r for r in fake.requests if r.url.path.endswith("/listings/active")]) == calls
    forced = web.client.get("/api/niche", params={"keyword": "peel and stick wallpaper", "refresh": 1})
    assert forced.json()["cached"] is False
    assert web.client.get("/api/niche", params={"keyword": " "}).status_code == 422


def test_the_watch_endpoints_add_list_and_remove(web):
    fake = use_fake_etsy(web, connected=False)
    row = niche.row(listing(1, age_days=10, views=50, favorers=4), NOW)
    fake.add("GET", "/listings/batch", {"count": 1, "results": [
        dict(listing(1, age_days=10, views=90, favorers=9), listing_id=row["id"])]})
    added = web.client.post("/api/niche/watch", json={"item": row}).json()
    assert [i["id"] for i in added["items"]] == [row["id"]] and added["max"] == niche.MAX_WATCHED
    assert web.client.post("/api/niche/watch", json={"item": {"title": "x"}}).status_code == 422
    listed = web.client.get("/api/niche/watch").json()["items"]
    assert len(listed) == 1 and listed[0]["title"] == row["title"]
    removed = web.client.delete(f"/api/niche/watch/{row['id']}").json()
    assert removed["items"] == []
    assert web.client.delete(f"/api/niche/watch/{row['id']}").status_code == 404
