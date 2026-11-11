"""Niş Bulucu: how much a keyword gets and how crowded it is, and a watch list.

    GET    /api/niche?keyword=&refresh=   the niche of a keyword (etsyotomasyon.niche.analyze)
    GET    /api/niche/watch                the watched listings, their numbers refreshed
    POST   /api/niche/watch {item}         watch a listing (an item of the analysis)
    DELETE /api/niche/watch/{id}           stop watching it

Everything is read from Etsy's public search with the API key alone: no sign-in, no shop
needed. An analysis is kept for a day, since the numbers move slowly and each one is a few
requests; "refresh" asks again. Nothing here changes anything on Etsy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ... import niche, seo
from ...drop import cache as research_cache
from ...errors import EtsyApiError
from ..router import ApiError, Request

if TYPE_CHECKING:  # pragma: no cover
    from ..context import AppContext
    from ..router import Router

SAMPLE = 300
NEWEST = 100
ATTEMPTS = 3
CACHE_TTL = 24 * 3600
NAMESPACE = "niche"
KEYWORD_MAX = 100


def register(r: Router, ctx: AppContext) -> None:
    r.get("/api/niche", analysis)
    r.get("/api/niche/watch", watch_list)
    r.post("/api/niche/watch", watch_add)
    r.delete("/api/niche/watch/{id:int}", watch_remove)


def _ctx(req: Request) -> AppContext:
    assert req.ctx is not None
    return req.ctx


def analysis(req: Request) -> dict[str, Any]:
    ctx = _ctx(req)
    keyword = " ".join((req.query.get("keyword") or "").split()).lower()[:KEYWORD_MAX]
    if not keyword:
        raise ApiError(422, "invalid", "keyword is empty", field="keyword")
    key = f"{keyword}|{SAMPLE}"
    if not req.bool_query("refresh"):
        cached = research_cache.load(key, namespace=NAMESPACE, ttl=CACHE_TTL)
        if isinstance(cached, dict) and cached.get("keyword") == keyword:
            return {**cached, "cached": True}
    client = ctx.client(require_auth=False)  # a public search: the API key is enough
    with client.attempts(ATTEMPTS):
        ranked = seo._search(client, keyword, SAMPLE, "score", {})
        first = client.get("/listings/active", params={"keywords": keyword, "limit": 1},
                           authed=False)
        total = int((first or {}).get("count") or len(ranked))
        try:
            newest = list(client.search_active_listings(
                keywords=keyword, max_items=NEWEST, sort_on="created", sort_order="desc"))
        except EtsyApiError as exc:
            if exc.status != 400:
                raise
            newest = []
    result = niche.analyze(keyword, ranked, newest, total)
    research_cache.store(key, result, namespace=NAMESPACE)
    return {**result, "cached": False}


def watch_list(req: Request) -> dict[str, Any]:
    """The watched listings. Their numbers are read again first when a snapshot is due
    (never more often than every few hours); a failure to read leaves the old ones."""
    ctx = _ctx(req)
    ids = [int(i) for i in niche.watch_load()["items"]]
    if ids:
        try:
            client = ctx.client(require_auth=False)
            with client.attempts(2):
                niche.watch_refresh(client.listings_batch(ids))
        except (EtsyApiError, ApiError):
            pass
    return {"items": niche.watch_view(), "max": niche.MAX_WATCHED}


def watch_add(req: Request) -> dict[str, Any]:
    item = req.json_object().get("item")
    if not isinstance(item, dict):
        raise ApiError(422, "invalid", "item must be an object", field="item")
    try:
        niche.watch_add(item)
    except ValueError as exc:
        raise ApiError(422, "invalid", str(exc), field="item") from exc
    return {"items": niche.watch_view(), "max": niche.MAX_WATCHED}


def watch_remove(req: Request) -> dict[str, Any]:
    if not niche.watch_remove(req.params["id"]):
        raise ApiError(404, "not_found", "That listing is not on the watch list.")
    return {"items": niche.watch_view(), "max": niche.MAX_WATCHED}
