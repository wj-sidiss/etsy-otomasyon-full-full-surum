"""Niche finder: how much a keyword sells for and how crowded it is, from Etsy's public search.

Etsy's API does not publish sales. What it does publish, for every listing, is how many
views and favourites it has had since it was created, and when it was created. From those,
for a sample of the listings that rank for a keyword:

- demand: the median views a listing gets per day of its life, and its favourites;
- competition: how many listings Etsy finds for the keyword, and how much of the sample
  the three biggest shops hold;
- trending: the listings gaining views fastest, and the new ones (under 120 days old) that
  are already getting them: where a newcomer can get in;
- the tags and title phrases the sample shares, and its price band.

Every number is what Etsy reports, and the score is a plain formula (`opportunity`) shown
with its parts, not a secret. A watch list keeps a listing's favourites and views over time
(`watch_*`), so growth can be seen from the days it was added.
"""

from __future__ import annotations

import json
import math
import statistics
import time
from collections import Counter
from pathlib import Path
from typing import Any

from . import seo
from .config import home_dir

DAY = 86400.0
NEW_DAYS = 120
ROWS = 10
TAG_ROWS = 15
PHRASE_ROWS = 12
WATCH_FILE = "niche-watch.json"
SNAPSHOT_EVERY = 6 * 3600.0
MAX_SNAPSHOTS = 400
MAX_WATCHED = 200


def _age_days(listing: dict[str, Any], now: float) -> float:
    for key in ("original_creation_timestamp", "creation_timestamp", "created_timestamp"):
        value = listing.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return max(1.0, (now - value) / DAY)
    return 365.0


def _number(listing: dict[str, Any], key: str) -> int:
    value = listing.get(key)
    return int(value) if isinstance(value, (int, float)) and value >= 0 else 0


def row(listing: dict[str, Any], now: float) -> dict[str, Any]:
    """One listing as the page shows it."""
    price, currency = seo._price(listing)
    age = _age_days(listing, now)
    views, favorers = _number(listing, "views"), _number(listing, "num_favorers")
    return {
        "id": listing.get("listing_id"),
        "title": str(listing.get("title") or ""),
        "url": str(listing.get("url") or ""),
        "shop_id": listing.get("shop_id"),
        "price": round(price, 2) if price is not None else None,
        "currency": currency,
        "views": views,
        "favorers": favorers,
        "age_days": round(age),
        "views_per_day": round(views / age, 2),
        "digital": listing.get("listing_type") in ("download", "both"),
        "tags": [str(t) for t in (listing.get("tags") or [])][:13],
    }


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 2) if values else None


def opportunity(median_views_per_day: float | None, total: int) -> dict[str, Any]:
    """The opportunity score, 0 to 100, from the two numbers it is made of.

    demand: log10(1 + views per day) over 2 (100 views a day is the top).
    competition: log10(listings found) from 1,000 (none) to 1,000,000 (all), over 3.
    score = 100 x demand x (1 - 0.7 x competition).
    """
    demand = min(1.0, math.log10(1 + (median_views_per_day or 0.0)) / 2)
    competition = min(1.0, max(0.0, (math.log10(max(1, total)) - 3) / 3))
    score = round(100 * demand * (1 - 0.7 * competition))
    level = "high" if score >= 60 else "mid" if score >= 35 else "low"
    return {"score": score, "level": level, "demand": round(demand, 2),
            "competition": round(competition, 2)}


def analyze(keyword: str, ranked: list[dict[str, Any]], newest: list[dict[str, Any]],
            total: int, now: float | None = None) -> dict[str, Any]:
    """The niche of `keyword`: `ranked` is the listings Etsy ranks first for it, `newest`
    the most recently created ones, `total` how many listings Etsy finds."""
    now = time.time() if now is None else now
    rows = [row(item, now) for item in ranked]
    per_day = [r["views_per_day"] for r in rows]
    prices_by_currency: dict[str, list[float]] = {}
    for r in rows:
        if r["price"] is not None:
            prices_by_currency.setdefault(r["currency"] or "?", []).append(r["price"])
    currency, prices = ("", [])
    if prices_by_currency:
        currency, prices = max(prices_by_currency.items(), key=lambda kv: len(kv[1]))

    shops = Counter(r["shop_id"] for r in rows if r["shop_id"])
    top3 = sum(n for _shop, n in shops.most_common(3))
    fresh = [r for r in rows if r["age_days"] <= NEW_DAYS]

    seen: dict[Any, dict[str, Any]] = {}
    for item in [*ranked, *newest]:
        r = row(item, now)
        if r["id"] is not None:
            seen[r["id"]] = r
    pool = list(seen.values())
    trending = sorted((r for r in rows if r["age_days"] >= 7),
                      key=lambda r: (-r["views_per_day"], -r["favorers"]))[:ROWS]
    rising = sorted((r for r in pool if r["age_days"] <= NEW_DAYS and r["views"] > 0),
                    key=lambda r: (-r["views_per_day"], -r["favorers"]))[:ROWS]

    tag_counter: Counter[str] = Counter()
    phrase_counter: Counter[str] = Counter()
    for item in ranked:
        tag_counter.update({k for t in (item.get("tags") or []) if (k := seo.tag_key(t))})
        phrase_counter.update(seo.title_phrases(str(item.get("title") or "")))
    median_pd = _median(per_day)
    return {
        "keyword": keyword,
        "sampled": len(rows),
        "total": int(total),
        "opportunity": opportunity(median_pd, total),
        "demand": {"views_per_day": median_pd, "favorers": _median([r["favorers"] for r in rows]),
                   "views": _median([r["views"] for r in rows])},
        "competition": {"total": int(total),
                        "top_shops_share": round(top3 / len(rows), 2) if rows else None,
                        "shops": len(shops),
                        "new_share": round(len(fresh) / len(rows), 2) if rows else None},
        "price": {"currency": currency, "min": round(min(prices), 2) if prices else None,
                  "median": _median(prices), "max": round(max(prices), 2) if prices else None,
                  "sample": len(prices)},
        "trending": trending,
        "rising": rising,
        "tags": [{"tag": t, "count": n} for t, n in
                 sorted(tag_counter.items(), key=lambda kv: (-kv[1], kv[0]))[:TAG_ROWS]],
        "phrases": [{"phrase": p, "count": int(n)}
                    for p, n in seo._top_phrases(phrase_counter, len(rows))[:PHRASE_ROWS]],
        "generated_at": int(now),
    }


# --- the watch list ----------------------------------------------------------------------


def watch_path() -> Path:
    return home_dir() / WATCH_FILE


def watch_load() -> dict[str, Any]:
    try:
        data = json.loads(watch_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    items = data.get("items") if isinstance(data, dict) else None
    return {"items": items if isinstance(items, dict) else {}}


def _save(state: dict[str, Any]) -> None:
    p = watch_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    temporary = p.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    temporary.replace(p)


def watch_add(item: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """Start watching the listing `item` (a `row`); its numbers now are the first snapshot."""
    now = time.time() if now is None else now
    listing_id = item.get("id")
    if not isinstance(listing_id, int):
        raise ValueError("a listing id is needed")
    state = watch_load()
    if len(state["items"]) >= MAX_WATCHED and str(listing_id) not in state["items"]:
        raise ValueError(f"at most {MAX_WATCHED} listings can be watched")
    entry = state["items"].setdefault(str(listing_id), {"snapshots": []})
    entry.update({
        "id": listing_id, "title": str(item.get("title") or "")[:200],
        "url": str(item.get("url") or ""), "shop_id": item.get("shop_id"),
        "price": item.get("price"), "currency": str(item.get("currency") or ""),
        "added": entry.get("added") or int(now),
    })
    if not entry["snapshots"]:
        entry["snapshots"].append([int(now), _number(item, "favorers"), _number(item, "views")])
    _save(state)
    return entry


def watch_remove(listing_id: int) -> bool:
    state = watch_load()
    if state["items"].pop(str(int(listing_id)), None) is None:
        return False
    _save(state)
    return True


def watch_refresh(listings: list[dict[str, Any]], now: float | None = None) -> int:
    """Add a snapshot of the numbers Etsy reports now to each watched listing in
    `listings` (not more often than every SNAPSHOT_EVERY). How many got one."""
    now = time.time() if now is None else now
    state = watch_load()
    done = 0
    for listing in listings:
        entry = state["items"].get(str(listing.get("listing_id")))
        if entry is None:
            continue
        shots = entry.setdefault("snapshots", [])
        if shots and now - shots[-1][0] < SNAPSHOT_EVERY:
            continue
        shots.append([int(now), _number(listing, "num_favorers"), _number(listing, "views")])
        del shots[:-MAX_SNAPSHOTS]
        done += 1
    if done:
        _save(state)
    return done


def watch_view(now: float | None = None) -> list[dict[str, Any]]:
    """The watched listings with how they moved: since the first snapshot and in 7 days."""
    now = time.time() if now is None else now
    out = []
    for entry in watch_load()["items"].values():
        shots = entry.get("snapshots") or []
        if not shots:
            continue
        first, last = shots[0], shots[-1]
        week = next((s for s in reversed(shots) if now - s[0] >= 7 * DAY), shots[0])
        days = max(1 / 24, (last[0] - first[0]) / DAY)
        out.append({
            **{k: entry.get(k) for k in ("id", "title", "url", "shop_id", "price", "currency", "added")},
            "favorers": last[1], "views": last[2],
            "favorers_change": last[1] - first[1], "views_change": last[2] - first[2],
            "favorers_week": last[1] - week[1], "views_week": last[2] - week[2],
            "views_per_day": round((last[2] - first[2]) / days, 1) if len(shots) > 1 else None,
            "history": [[s[0], s[1]] for s in shots[-60:]],
        })
    return sorted(out, key=lambda e: -(e["added"] or 0))
