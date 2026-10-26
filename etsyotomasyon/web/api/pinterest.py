"""Pinterest endpoints: the seller's own Pinterest app, account, boards and Pin queue.

    GET  /api/pinterest/status       app settings (never the secret), account, queue summary
    POST /api/pinterest/keys         {app_id, app_secret?, redirect_uri?, sandbox?}
    POST /api/pinterest/connect      -> {job, url}: the consent flow, as a cancellable job
    POST /api/pinterest/disconnect
    GET  /api/pinterest/boards       ?refresh=1
    POST /api/pinterest/board        {board_id, name}: the default board (shop pref pin_board)
    GET  /api/pinterest/listings     ?refresh=1: active listings for the picker
    GET  /api/pinterest/queue
    POST /api/pinterest/queue        {listing_ids, board_id, per_day, images, ai_modified, dry_run}
    POST /api/pinterest/post         {limit?, confirm: true} -> job: post the Pins due today
    POST /api/pinterest/retry        {ids}: put failed / uncertain Pins back for today
    POST /api/pinterest/remove       {ids}: drop Pins that were not posted from the queue

Everything goes through etsyotomasyon.pinterest; queueing only reads Etsy and writes the
local queue file, posting is the only thing that reaches Pinterest in bulk.
"""

from __future__ import annotations

import logging
import re
import secrets
import sys
import threading
import time
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from functools import partial
from typing import TYPE_CHECKING, Any

from ... import pinterest
from ...desktop import settings
from ...errors import AuthError, ConfigError, EtsyApiError, ValidationError
from ..jobs import JobCancelled
from ..router import ApiError, Request

if TYPE_CHECKING:  # pragma: no cover
    from ..context import AppContext
    from ..jobs import Job
    from ..router import Router

log = logging.getLogger("etsyotomasyon.web")

PINTEREST_APPS_URL = "https://developers.pinterest.com/apps/"
DONE_PATH = "/oauth-done"
CONNECT_TIMEOUT = 300.0
CONNECT_TITLE = "pinterest:job.connect"
POST_TITLE = "pinterest:job.post"
BOARDS_TTL = 600.0
LISTINGS_TTL = 300.0
LISTINGS_MAX = 500
MAX_QUEUE_LISTINGS = 200
MAX_PER_DAY = 25
DEFAULT_PER_DAY = 2
UNSETTLED = ("failed", "uncertain", "sending")
PIN_RATE = 1.0  # requests per second to Pinterest: the library's own careful default
ETSY_ATTEMPTS = 2  # tries per Etsy read while a page waits (not the batch default)
_RANKS = re.compile(r"^\s*\d+(\s*-\s*\d+)?(\s*,\s*\d+(\s*-\s*\d+)?)*\s*$")


class PinState:
    """What the Pinterest endpoints keep between requests, per running app."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.boards: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
        # shop -> (fetched at, the newest LISTINGS_MAX active listings, the shop's active count)
        self.listings: dict[str, tuple[float, list[dict[str, Any]], int]] = {}
        self.connecting = False


def register(r: Router, ctx: AppContext) -> None:
    state = PinState()

    def forget_listings(c: AppContext) -> None:
        with state.lock:
            state.listings.pop(c.shop_id, None)

    ctx.on_change("listings", forget_listings, name="pinterest")
    r.get("/api/pinterest/status", partial(get_status, state=state))
    r.post("/api/pinterest/keys", partial(save_keys, state=state))
    r.post("/api/pinterest/connect", partial(connect, state=state))
    r.post("/api/pinterest/disconnect", partial(disconnect, state=state))
    r.get("/api/pinterest/boards", partial(boards, state=state))
    r.post("/api/pinterest/board", set_board)
    r.get("/api/pinterest/listings", partial(listings, state=state))
    r.get("/api/pinterest/queue", partial(queue_get, state=state))
    r.post("/api/pinterest/queue", partial(queue_add, state=state))
    r.post("/api/pinterest/post", post_due)
    r.post("/api/pinterest/retry", partial(retry, state=state))
    r.post("/api/pinterest/remove", partial(remove, state=state))


def _ctx(req: Request) -> AppContext:
    assert req.ctx is not None
    return req.ctx


# --- errors -----------------------------------------------------------------------------


def _api_error(exc: pinterest.PinterestApiError) -> ApiError:
    if exc.status == 0:
        return ApiError(503, "pinterest_offline", exc.message)
    if exc.status == 401:
        return ApiError(401, "pinterest_auth", exc.message)
    if exc.status == 403:
        return ApiError(403, "pinterest_forbidden", exc.message)
    if exc.status == 429:
        return ApiError(429, "pinterest_rate_limited", exc.message)
    return ApiError(502, "pinterest_error", exc.message, status=exc.status)


@contextmanager
def pinterest_errors() -> Iterator[None]:
    """Pinterest's failures as Pinterest error codes (not the Etsy ones)."""
    try:
        yield
    except pinterest.PinterestApiError as exc:
        raise _api_error(exc) from exc
    except ConfigError as exc:
        raise ApiError(409, "pinterest_keys", str(exc)) from exc
    except AuthError as exc:
        if "could not reach" in str(exc).lower():
            raise ApiError(503, "pinterest_offline", str(exc)) from exc
        raise ApiError(401, "pinterest_auth", str(exc)) from exc


# --- settings and account -------------------------------------------------------------


def _config() -> pinterest.PinterestConfig:
    return pinterest.PinterestConfig.load()


def _redirect_port(uri: str) -> int | None:
    """The port to listen on when `uri` can be caught on this computer, else None."""
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme != "http" or (parsed.hostname or "").lower() not in ("localhost", "127.0.0.1"):
        return None
    try:
        return parsed.port or 80
    except ValueError:
        return None


def _load_token() -> pinterest.PinToken | None:
    try:
        return pinterest.load_token()
    except (ConfigError, TypeError, KeyError, ValueError):
        return None


def _schedule(ctx: AppContext) -> dict[str, Any]:
    """The command a daily scheduler runs to post the due Pins, for this computer."""
    shop = f"--shop {ctx.shop_id} " if ctx.shop_id else ""
    if getattr(sys, "frozen", False):
        program, arguments = sys.executable, f"{shop}pinterest post"
    else:
        program, arguments = sys.executable, f"-m etsyotomasyon {shop}pinterest post"
    return {
        "program": program,
        "arguments": arguments,
        "command": f'"{program}" {arguments}',
        "platform": "mac" if sys.platform == "darwin" else ("windows" if sys.platform == "win32" else "linux"),
    }


def _queue_summary(queue: pinterest.Queue) -> dict[str, int]:
    counts = queue.counts()
    return {
        "total": len(queue.entries),
        "pending": counts.get("pending", 0),
        "due": len(queue.due(date.today())),
        "posted": counts.get("posted", 0),
        "failed": counts.get("failed", 0),
        "uncertain": counts.get("uncertain", 0) + counts.get("sending", 0),
    }


def _connect_job(ctx: AppContext) -> Any:
    for job in ctx.jobs.list(kind="pinterest", active=True):
        if job.title_key == CONNECT_TITLE:
            return job
    return None


def status_payload(ctx: AppContext) -> dict[str, Any]:
    config = _config()
    token = _load_token()
    prefs = ctx.shop_prefs()
    now = time.time()
    port = _redirect_port(config.redirect_uri)
    from_env = bool(config.access_token)
    token_info: dict[str, Any] | None = None
    needs_reconnect = False
    if from_env:
        token_info = {"source": "env", "expires_at": None, "refreshable": False, "scope": ""}
    elif token is not None:
        refresh_dead = bool(token.refresh_expires_at) and now >= token.refresh_expires_at
        needs_reconnect = (token.expired and not token.refresh_token) or refresh_dead
        token_info = {
            "source": "file",
            "expires_at": token.expires_at or None,
            "refresh_expires_at": token.refresh_expires_at or None,
            "refreshable": bool(token.refresh_token),
            "scope": token.scope,
        }
    try:
        queue: dict[str, Any] | None = _queue_summary(pinterest.Queue.load())
        queue_error = None
    except ValidationError as exc:
        queue, queue_error = None, str(exc)
    job = _connect_job(ctx)
    connecting = None
    if job is not None:
        full = job.to_dict()
        connecting = {"job": job.summary(), "url": full["state"].get("url")}
    return {
        "app": {
            "app_id": config.app_id,
            "has_secret": bool(config.app_secret),
            "secret_length": len(config.app_secret),
            "redirect_uri": config.redirect_uri,
            "redirect_default": pinterest.DEFAULT_REDIRECT_URI,
            "redirect_ok": port is not None and port != ctx.port,
            "sandbox": config.sandbox,
            "apps_url": PINTEREST_APPS_URL,
        },
        "configured": bool(config.app_id and config.app_secret),
        "connected": token_info is not None,
        "needs_reconnect": needs_reconnect,
        "token": token_info,
        "board": {
            "id": str(prefs.get("pin_board") or "") or None,
            "name": prefs.get("pin_board_name") or None,
        },
        "defaults": {
            "per_day": _int(prefs.get("pin_per_day"), DEFAULT_PER_DAY, 1, MAX_PER_DAY),
            "images": str(prefs.get("pin_images") or ""),
            "ai_modified": bool(prefs.get("pin_ai", True)),
        },
        "queue": queue,
        "queue_error": queue_error,
        "schedule": _schedule(ctx),
        "connecting": connecting,
    }


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def get_status(req: Request, state: PinState) -> dict[str, Any]:
    return status_payload(_ctx(req))


def save_keys(req: Request, state: PinState) -> dict[str, Any]:
    """Save the app's id, secret, callback and sandbox switch in the shop's .env.

    An empty secret keeps the saved one. A different app id forgets the old sign-in:
    a token belongs to the app that asked for it.
    """
    ctx = _ctx(req)
    body = req.json_object()
    app_id = str(body.get("app_id") or "").strip()
    if not app_id or len(app_id) > 200 or any(ch.isspace() for ch in app_id):
        raise ApiError(422, "invalid", "app_id is missing or has spaces in it", field="app_id")
    secret = str(body.get("app_secret") or "").strip()
    if any(ch.isspace() for ch in secret) or len(secret) > 500:
        raise ApiError(422, "invalid", "app_secret has spaces in it", field="app_secret")
    old = _config()
    if not secret and not old.app_secret:
        raise ApiError(422, "invalid", "app_secret is missing", field="app_secret")
    redirect = str(body.get("redirect_uri") or "").strip() or pinterest.DEFAULT_REDIRECT_URI
    parsed = urllib.parse.urlparse(redirect)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ApiError(422, "invalid", "redirect_uri must be an http(s) address", field="redirect_uri")
    if _redirect_port(redirect) == ctx.port:
        raise ApiError(422, "invalid", "redirect_uri must not use etsyotomasyon's own port",
                       field="redirect_uri")
    sandbox = body.get("sandbox", old.sandbox)
    if not isinstance(sandbox, bool):
        raise ApiError(422, "invalid", "sandbox must be true or false", field="sandbox")
    updates = {
        "PINTEREST_APP_ID": app_id,
        "PINTEREST_REDIRECT_URI": redirect,
        "PINTEREST_SANDBOX": "1" if sandbox else "",
    }
    if secret:
        updates["PINTEREST_APP_SECRET"] = secret
    settings.save(updates)
    cleared = False
    if old.app_id and old.app_id != app_id:
        cleared = pinterest.clear_token()
    with state.lock:
        state.boards.clear()
    payload = status_payload(ctx)
    payload["token_cleared"] = cleared
    return payload


def connect(req: Request, state: PinState) -> dict[str, Any]:
    """Start the consent flow: listen on the callback port, then wait for it as a job.

    The listener is open before this answers, so the page can send the browser to
    Pinterest at once; the job waits (cancellable) for the code and swaps it for a token.
    """
    ctx = _ctx(req)
    config = _config()
    if not (config.app_id and config.app_secret):
        raise ApiError(409, "pinterest_keys", "Save the Pinterest app id and secret first.")
    port = _redirect_port(config.redirect_uri)
    if port is None or port == ctx.port:
        raise ApiError(409, "pinterest_callback_not_local",
                       "Only an http://localhost callback can be caught on this computer.",
                       redirect_uri=config.redirect_uri)
    if ctx.jobs.busy():
        raise ApiError(409, "busy", "Wait for the running task to finish first.")
    with state.lock:
        if state.connecting:
            raise ApiError(409, "busy", "Pinterest is already being connected.")
        state.connecting = True
    oauth_state = secrets.token_urlsafe(16)
    url = pinterest.authorization_url(config, oauth_state)
    # The listener's class state: one connect flow at a time (state.connecting).
    pinterest._Callback.result = {}
    pinterest._Callback.return_url = f"http://localhost:{ctx.port}{DONE_PATH}?service=pinterest"
    # A request without our state (any page can hit localhost) is refused; the wait goes on.
    pinterest._Callback.expected_state = oauth_state
    try:
        server = pinterest.LoopbackServer(("127.0.0.1", port), pinterest._Callback)
    except OSError as exc:
        pinterest._Callback.return_url = None
        pinterest._Callback.expected_state = None
        with state.lock:
            state.connecting = False
        raise ApiError(409, "pinterest_port_busy", str(exc), port=port) from exc
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.4},
                              name="etsyotomasyon-pinterest-listener", daemon=True)
    thread.start()
    closed = threading.Event()

    def close_listener() -> None:
        if closed.is_set():
            return
        closed.set()
        server.shutdown()
        server.server_close()
        pinterest._Callback.return_url = None
        pinterest._Callback.expected_state = None
        with state.lock:
            state.connecting = False

    # Should the job never run (the app stopping), the port must still be let go.
    guard = threading.Timer(CONNECT_TIMEOUT + 60.0, close_listener)
    guard.daemon = True
    guard.start()

    def work(job: Job) -> dict[str, Any]:
        try:
            job.emit("phase", phase="waiting")
            deadline = time.monotonic() + CONNECT_TIMEOUT
            while not pinterest._Callback.result:
                if job.cancelled:
                    raise JobCancelled()
                if time.monotonic() > deadline:
                    raise ApiError(408, "pinterest_timeout", "Pinterest did not send the browser back.")
                time.sleep(0.25)
        finally:
            guard.cancel()
            close_listener()
        result = dict(pinterest._Callback.result)
        if "error" in result:
            raise ApiError(400, "pinterest_denied", result.get("error_description") or result["error"])
        if result.get("state") != oauth_state or not result.get("code"):
            raise ApiError(400, "pinterest_state", "The answer did not come from the request we started.")
        job.emit("phase", phase="exchanging")
        with pinterest_errors():
            token = pinterest.exchange_code(config, result["code"])
        with state.lock:
            state.boards.clear()
        ctx.notify("pinterest", "notify.connected", tone="success", link="/pinterest")
        return {"connected": True, "scope": token.scope}

    try:
        job = ctx.jobs.start("pinterest", CONNECT_TITLE, work, cancellable=True)
    except BaseException:
        guard.cancel()
        close_listener()
        raise
    job.set_state(url=url)
    return {"job": job.summary(), "url": url}


def disconnect(req: Request, state: PinState) -> dict[str, Any]:
    ctx = _ctx(req)
    pinterest.clear_token()
    with state.lock:
        state.boards.clear()
    return status_payload(ctx)


# --- boards -------------------------------------------------------------------------------


def _http() -> Any:
    """The HTTP client a PinterestClient sends through (tests swap in a MockTransport)."""
    import httpx

    return httpx.Client(timeout=30.0)


def _pin_client() -> pinterest.PinterestClient:
    config = _config()
    token = None
    if not config.access_token:
        if not (config.app_id and config.app_secret):
            raise ApiError(409, "pinterest_keys", "Save the Pinterest app id and secret first.")
        token = _load_token()
        if token is None:
            raise ApiError(409, "pinterest_not_connected", "Connect your Pinterest account first.")
    with pinterest_errors():
        return pinterest.PinterestClient(config, token=token, http=_http(), per_second=PIN_RATE)


def _board_view(board: dict[str, Any]) -> dict[str, Any]:
    media = board.get("media") if isinstance(board.get("media"), dict) else {}
    return {
        "id": str(board.get("id") or ""),
        "name": str(board.get("name") or ""),
        "description": str(board.get("description") or ""),
        "privacy": str(board.get("privacy") or ""),
        "pin_count": board.get("pin_count"),
        "cover": media.get("image_cover_url") or None,
    }


def _boards_key(ctx: AppContext) -> tuple[str, str]:
    config = _config()
    return ctx.shop_id, f"{config.app_id}|{config.sandbox}|{bool(config.access_token)}"


def _cached_boards(ctx: AppContext, state: PinState) -> list[dict[str, Any]]:
    with state.lock:
        hit = state.boards.get(_boards_key(ctx))
    return hit[1] if hit else []


def boards(req: Request, state: PinState) -> dict[str, Any]:
    ctx = _ctx(req)
    key = _boards_key(ctx)
    with state.lock:
        hit = state.boards.get(key)
    if hit is None or req.bool_query("refresh") or time.time() - hit[0] > BOARDS_TTL:
        client = _pin_client()
        try:
            with pinterest_errors():
                items = [_board_view(b) for b in client.boards()]
        finally:
            client.close()
        with state.lock:
            state.boards[key] = (time.time(), items)
    else:
        items = hit[1]
    prefs = ctx.shop_prefs()
    wanted = str(prefs.get("pin_board") or "").strip()
    default = None
    if wanted:
        default = next((b["id"] for b in items if b["id"] == wanted), None)
        if default is None:
            # The old window stored whatever was typed: often the board's name.
            named = [b for b in items if b["name"].strip().casefold() == wanted.casefold()]
            if len(named) == 1:
                default = named[0]["id"]
                ctx.update_shop_prefs(pin_board=default, pin_board_name=named[0]["name"])
    return {"items": items, "default": default}


def set_board(req: Request) -> dict[str, Any]:
    ctx = _ctx(req)
    body = req.json_object()
    board_id = str(body.get("board_id") or "").strip()
    name = str(body.get("name") or "").strip()[:200]
    if not board_id or len(board_id) > 100:
        raise ApiError(422, "invalid", "board_id is missing", field="board_id")
    ctx.update_shop_prefs(pin_board=board_id, pin_board_name=name or None)
    return {"board": {"id": board_id, "name": name or None}}


# --- listings for the picker ----------------------------------------------------------------


def _small_image(url: str) -> str:
    """Etsy's 170x135 rendition of a full-size image URL, when the URL has the usual shape."""
    return url.replace("il_fullxfull.", "il_170x135.") if "il_fullxfull." in url else url


def _thumb(images: list[dict[str, Any]], rank: int | None = None) -> str | None:
    chosen = sorted(images, key=lambda img: img.get("rank") or 0)
    if rank is not None:
        chosen = [img for img in chosen if img.get("rank") == rank] or chosen
    for img in chosen:
        url = img.get("url_170x135") or img.get("url_75x75") or img.get("url_570xN")
        if url:
            return str(url)
    return None


def _queue_or_empty() -> pinterest.Queue:
    try:
        return pinterest.Queue.load()
    except ValidationError:
        return pinterest.Queue(pinterest.queue_path())


def listings(req: Request, state: PinState) -> dict[str, Any]:
    """Active listings with a thumbnail, and how many of their Pins are queued.

    At most the newest LISTINGS_MAX are listed (`shown`); `total` is the shop's real
    active count, so a large shop is not reported as having exactly LISTINGS_MAX.
    """
    ctx = _ctx(req)
    shop = ctx.shop_id
    with state.lock:
        hit = state.listings.get(shop)
    if hit is None or req.bool_query("refresh") or time.time() - hit[0] > LISTINGS_TTL:
        client = ctx.client()
        with client.attempts(ETSY_ATTEMPTS):  # a page waits on this: fail fast when offline
            rows = [
                {
                    "listing_id": listing.get("listing_id"),
                    "title": listing.get("title") or "",
                    "url": (listing.get("url") or "").split("?", 1)[0],
                    "thumb": _thumb(listing.get("images") or []),
                    "images": len(listing.get("images") or []),
                }
                for listing in client.listings_by_shop("active", includes=["Images"],
                                                       max_items=LISTINGS_MAX)
            ]
            active = len(rows)
            if active >= LISTINGS_MAX:  # cut: one limit=1 request for the real count
                try:
                    active = max(active, client.count_listings("active"))
                except EtsyApiError as exc:
                    log.warning("pinterest: active listing count unavailable (%s)", exc)
        with state.lock:
            state.listings[shop] = (time.time(), rows, active)
    else:
        rows, active = hit[1], hit[2]
    queued: dict[Any, dict[str, int]] = {}
    for entry in _queue_or_empty().entries:
        mark = queued.setdefault(entry.get("listing_id"), {"queued": 0, "posted": 0})
        if entry.get("status") == "posted":
            mark["posted"] += 1
        elif entry.get("status") == "pending":
            mark["queued"] += 1
    items = [dict(row, **queued.get(row["listing_id"], {"queued": 0, "posted": 0})) for row in rows]
    return {"items": items, "total": active, "shown": len(items),
            "truncated": active > len(items)}


# --- the queue ------------------------------------------------------------------------------


def _entry_view(entry: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    payload = entry.get("payload") or {}
    media = payload.get("media_source") or {}
    image = str(media.get("url") or "")
    board_id = str(payload.get("board_id") or "")
    status = entry.get("status")
    return {
        "id": entry.get("key"),
        "listing_id": entry.get("listing_id"),
        "rank": entry.get("rank"),
        "due": entry.get("due"),
        "status": "uncertain" if status == "sending" else status,
        "pin_id": entry.get("pin_id"),
        "message": entry.get("message") or "",
        "title": payload.get("title") or "",
        "link": payload.get("link") or "",
        "board_id": board_id,
        "board_name": names.get(board_id),
        "thumb": entry.get("thumb") or (_small_image(image) if image else None),
        "ai_modified": bool(payload.get("ai_disclosures")),
    }


def _board_names(ctx: AppContext, state: PinState) -> dict[str, str]:
    names = {b["id"]: b["name"] for b in _cached_boards(ctx, state)}
    prefs = ctx.shop_prefs()
    if prefs.get("pin_board") and prefs.get("pin_board_name"):
        names.setdefault(str(prefs["pin_board"]), str(prefs["pin_board_name"]))
    return names


def queue_get(req: Request, state: PinState) -> dict[str, Any]:
    ctx = _ctx(req)
    queue = pinterest.Queue.load()
    names = _board_names(ctx, state)
    # What needs a look first, then what goes out next, then the newest posted.
    order = {"sending": 0, "uncertain": 0, "failed": 1, "pending": 2}
    waiting = sorted(
        (e for e in queue.entries if e.get("status") != "posted"),
        key=lambda e: (order.get(e.get("status"), 3), e.get("due") or "", e.get("key") or ""),
    )
    posted = sorted(
        (e for e in queue.entries if e.get("status") == "posted"),
        key=lambda e: (e.get("due") or "", e.get("key") or ""),
        reverse=True,
    )
    items = [_entry_view(e, names) for e in waiting + posted]
    return {"items": items, "summary": _queue_summary(queue), "today": date.today().isoformat()}


def _listing_ids(body: dict[str, Any]) -> list[int]:
    raw = body.get("listing_ids")
    if not isinstance(raw, list) or not raw:
        raise ApiError(422, "invalid", "listing_ids must be a non-empty list", field="listing_ids")
    ids: list[int] = []
    for value in raw:
        if isinstance(value, bool) or not str(value).strip().isdigit():
            raise ApiError(422, "invalid", "listing_ids must be listing numbers", field="listing_ids")
        ids.append(int(str(value).strip()))
    ids = list(dict.fromkeys(ids))
    if len(ids) > MAX_QUEUE_LISTINGS:
        raise ApiError(422, "invalid", f"at most {MAX_QUEUE_LISTINGS} listings at a time",
                       field="listing_ids")
    return ids


def _problem(listing_id: int, listing: dict[str, Any] | None, code: str) -> dict[str, Any]:
    return {"listing_id": listing_id, "title": (listing or {}).get("title") or "", "code": code}


def queue_add(req: Request, state: PinState) -> dict[str, Any]:
    """Build the Pins for the chosen listings and schedule them (or only preview that)."""
    ctx = _ctx(req)
    body = req.json_object()
    ids = _listing_ids(body)
    board_id = str(body.get("board_id") or "").strip()
    if not board_id or len(board_id) > 100:
        raise ApiError(422, "invalid", "board_id is missing", field="board_id")
    board_name = str(body.get("board_name") or "").strip()[:200]
    per_day = body.get("per_day", DEFAULT_PER_DAY)
    if isinstance(per_day, bool) or not isinstance(per_day, int) or not 1 <= per_day <= MAX_PER_DAY:
        raise ApiError(422, "invalid", f"per_day must be 1 to {MAX_PER_DAY}", field="per_day",
                       min=1, max=MAX_PER_DAY)
    images = str(body.get("images") or "").strip()
    if images and not _RANKS.match(images):
        raise ApiError(422, "pinterest_bad_ranks", "images takes ranks like 1-3 or 1,3,5",
                       field="images")
    try:
        ranks = pinterest.parse_ranks(images or None)
    except ValidationError as exc:
        raise ApiError(422, "pinterest_bad_ranks", str(exc), field="images") from exc
    ai_modified = body.get("ai_modified", False)
    dry_run = body.get("dry_run", False)
    if not isinstance(ai_modified, bool) or not isinstance(dry_run, bool):
        raise ApiError(422, "invalid", "ai_modified and dry_run must be true or false")

    client = ctx.client()
    with client.attempts(ETSY_ATTEMPTS):
        found = {int(item["listing_id"]): item
                 for item in client.listings_batch(ids, includes=["Images"])
                 if item.get("listing_id") is not None}
    pins: list[dict[str, Any]] = []
    problems: list[dict[str, Any]] = []
    thumbs: dict[tuple[Any, Any], str] = {}
    titles: dict[int, str] = {}
    for listing_id in ids:
        listing = found.get(listing_id)
        if listing is None:
            problems.append(_problem(listing_id, None, "not_found"))
            continue
        titles[listing_id] = listing.get("title") or ""
        if listing.get("state") != "active":
            problems.append(_problem(listing_id, listing, "not_active"))
            continue
        if not listing.get("url"):
            problems.append(_problem(listing_id, listing, "no_url"))
            continue
        listing_images = listing.get("images")
        if listing_images is None:
            listing_images = client.listing_images(listing_id)
        try:
            made = pinterest.pins_for_listing(listing, listing_images, board_id, ranks=ranks,
                                              ai_modified=ai_modified)
        except ValidationError:
            problems.append(_problem(listing_id, listing, "no_images"))
            continue
        if not made:
            problems.append(_problem(listing_id, listing, "no_images"))
            continue
        for pin in made:
            small = _thumb(listing_images, pin["rank"])
            if small:
                thumbs[(pin["listing_id"], pin["rank"])] = small
        pins.extend(made)

    queue = pinterest.Queue.load()
    added = queue.add(pins, start=date.today(), per_day=per_day)
    for entry in added:
        small = thumbs.get((entry["listing_id"], entry["rank"]))
        if small:
            entry["thumb"] = small
    if not dry_run and added:
        queue.save()
    if not dry_run:
        ctx.update_shop_prefs(pin_board=board_id, pin_board_name=board_name or None,
                              pin_per_day=per_day, pin_images=images, pin_ai=ai_modified)
        with state.lock:
            state.listings.pop(ctx.shop_id, None)
    names = _board_names(ctx, state)
    if board_name:
        names.setdefault(board_id, board_name)
    return {
        "dry_run": dry_run,
        "added": [_entry_view(e, names) for e in added],
        "skipped": len(pins) - len(added),
        "problems": problems,
        "first_due": added[0]["due"] if added else None,
        "last_due": max(e["due"] for e in added) if added else None,
        "summary": _queue_summary(queue) if not dry_run else _queue_summary(_queue_or_empty()),
    }


def post_due(req: Request) -> dict[str, Any]:
    """Post the Pins due today, as a job. Each is marked `sending` on disk before its
    request, and one that may have landed is parked as uncertain, never re-sent."""
    ctx = _ctx(req)
    body = req.json_object()
    limit = body.get("limit")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500):
        raise ApiError(422, "invalid", "limit must be 1 to 500", field="limit")
    due = pinterest.Queue.load().due(date.today())
    if not due:
        raise ApiError(409, "pinterest_nothing_due", "No Pin is due today.")
    _pin_client().close()  # not set up / not connected: say so now, not in the job
    # Pins go out publicly under the seller's name: like every other live write, the
    # page asks first and says so; a request that skipped the question posts nothing.
    if body.get("confirm") is not True:
        raise ApiError(409, "confirm_required", "Posting Pins needs confirm: true.")
    total = min(len(due), limit) if limit else len(due)

    def work(job: Job) -> dict[str, Any]:
        client = _pin_client()
        done: list[dict[str, Any]] = []
        try:
            with pinterest_errors():
                # One read first: it proves the sign-in works (and refreshes it) before
                # any Pin is marked as being sent.
                next(iter(client.boards()), None)
                queue = pinterest.Queue.load()
                count = min(len(queue.due(date.today())), limit) if limit else len(queue.due(date.today()))
                job.progress(0, count)

                def on_progress(entry: dict[str, Any]) -> None:
                    done.append(entry)
                    title = (entry.get("payload") or {}).get("title") or ""
                    job.progress(len(done), count, label=title[:80])
                    job.emit("pin", id=entry.get("key"), status=entry.get("status"),
                             message=entry.get("message") or "")
                    job.check_cancel()  # between Pins: this one is already saved

                pinterest.post_due(client, queue, today=date.today(), limit=limit,
                                   on_progress=on_progress)
        finally:
            client.close()
        posted = sum(1 for e in done if e.get("status") == "posted")
        failed = sum(1 for e in done if e.get("status") == "failed")
        uncertain = sum(1 for e in done if e.get("status") in ("uncertain", "sending"))
        if failed or uncertain:
            ctx.notify("pinterest", "notify.posted_problems",
                       {"n": posted, "problems": failed + uncertain}, tone="warning", link="/pinterest")
        else:
            ctx.notify("pinterest", "notify.posted", {"n": posted}, tone="success", link="/pinterest")
        return {"posted": posted, "failed": failed, "uncertain": uncertain, "total": len(done)}

    job = ctx.jobs.start("pinterest", POST_TITLE, work, params={"n": total}, cancellable=True)
    return {"job": job.summary()}


def _entry_ids(body: dict[str, Any]) -> set[str]:
    raw = body.get("ids")
    if not isinstance(raw, list) or not raw or not all(isinstance(x, str) and x for x in raw):
        raise ApiError(422, "invalid", "ids must be a non-empty list of queue entry ids", field="ids")
    return set(raw)


def retry(req: Request, state: PinState) -> dict[str, Any]:
    """Put failed or uncertain Pins back in the queue for today (like `pinterest retry`).

    An uncertain Pin may already be on the board: the page asks the seller to look
    first, since retrying one that did land posts it twice.
    """
    ctx = _ctx(req)
    ids = _entry_ids(req.json_object())
    queue = pinterest.Queue.load()
    today = date.today().isoformat()
    touched = 0
    for entry in queue.entries:
        if entry.get("key") in ids and entry.get("status") in UNSETTLED:
            entry.update(status="pending", due=today, message="")
            touched += 1
    if not touched:
        raise ApiError(409, "pinterest_nothing_to_retry", "None of those Pins can be retried.")
    queue.save()
    return {"retried": touched, "summary": _queue_summary(queue),
            "items": [_entry_view(e, _board_names(ctx, state)) for e in queue.entries
                      if e.get("key") in ids]}


def remove(req: Request, state: PinState) -> dict[str, Any]:
    """Drop Pins from the queue. A posted Pin stays: it is what stops a second one."""
    _ctx(req)
    ids = _entry_ids(req.json_object())
    queue = pinterest.Queue.load()
    keep = [e for e in queue.entries if e.get("key") not in ids or e.get("status") == "posted"]
    removed = len(queue.entries) - len(keep)
    if not removed:
        raise ApiError(409, "pinterest_nothing_to_remove", "None of those Pins can be removed.")
    queue.entries = keep
    queue.save()
    return {"removed": removed, "summary": _queue_summary(queue)}
