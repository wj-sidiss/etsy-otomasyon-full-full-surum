"""Pinterest: turn published listings into Pins, spread out over days.

Optional, and entirely the seller's own: it runs against *their* Pinterest app and
*their* account, configured in `.env`, and does nothing until asked. Nothing here is
shared between users, and no Pinterest credential ever leaves the seller's machine
except to Pinterest.

The shape mirrors the Etsy side on purpose. Pins are queued first and posted later,
a few a day, because Pinterest treats a burst of Pins pointing at the same link as
spam — and a Pin that was sent but not confirmed is never sent again automatically,
because a duplicate Pin is exactly the thing that burst would have looked like.
"""

from __future__ import annotations

import base64
import html
import http.server
import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

import httpx

from .auth import FOREIGN_DETAIL, FOREIGN_TITLE, LoopbackServer, listener_page, state_matches
from .client import RateLimiter, unescape_text
from .config import home_dir, read_json, write_json_private
from .errors import AuthError, ConfigError, EtsyotomasyonError, ValidationError

API_BASE = "https://api.pinterest.com/v5"
# Apps on Pinterest's trial tier write to the sandbox until they are granted standard
# access. The sandbox takes its own token, generated in the developer portal.
SANDBOX_BASE = "https://api-sandbox.pinterest.com/v5"
AUTHORIZE_URL = "https://www.pinterest.com/oauth/"
TOKEN_URL = f"{API_BASE}/oauth/token"

SCOPES = ("boards:read", "pins:read", "pins:write", "user_accounts:read")
DEFAULT_REDIRECT_URI = "http://localhost:8085/"

# Limits from Pinterest's API schema for POST /pins.
MAX_TITLE_LEN = 100
MAX_DESCRIPTION_LEN = 800
MAX_ALT_TEXT_LEN = 500
MAX_LINK_LEN = 2048

MAX_ATTEMPTS = 5
EXPIRY_SKEW = 120

# Pinterest's disclosure value for imagery created or altered with AI.
AI_MODIFIED = "AI_MODIFIED"


class PinterestApiError(EtsyotomasyonError):
    def __init__(self, status: int, message: str, *, method: str = "", path: str = "") -> None:
        self.status, self.message, self.method, self.path = status, message, method, path
        super().__init__(f"Pinterest {method} {path} -> {status}: {message}".strip())


# --- configuration -------------------------------------------------------------


@dataclass
class PinterestConfig:
    app_id: str = ""
    app_secret: str = ""
    redirect_uri: str = DEFAULT_REDIRECT_URI
    sandbox: bool = False
    # A token pasted from the developer portal. The sandbox only takes these, and it is
    # the quickest way to try things on your own account; it cannot be refreshed.
    access_token: str = ""

    @property
    def base(self) -> str:
        return SANDBOX_BASE if self.sandbox else API_BASE

    @classmethod
    def load(cls) -> PinterestConfig:
        from .config import load_env

        load_env()
        cfg = cls(
            app_id=(os.environ.get("PINTEREST_APP_ID") or "").strip(),
            app_secret=(os.environ.get("PINTEREST_APP_SECRET") or "").strip(),
            redirect_uri=(os.environ.get("PINTEREST_REDIRECT_URI") or DEFAULT_REDIRECT_URI).strip(),
            sandbox=(os.environ.get("PINTEREST_SANDBOX") or "").strip().lower() in {"1", "true", "yes"},
            access_token=(os.environ.get("PINTEREST_ACCESS_TOKEN") or "").strip(),
        )
        return cfg

    def require_app(self) -> None:
        if not (self.app_id and self.app_secret):
            raise ConfigError(
                "Pinterest is not set up. Create an app at https://developers.pinterest.com/apps/, "
                "then put PINTEREST_APP_ID and PINTEREST_APP_SECRET in your .env."
            )


# --- tokens --------------------------------------------------------------------


def token_path() -> Path:
    return home_dir() / "pinterest_token.json"


@dataclass
class PinToken:
    access_token: str
    refresh_token: str = ""
    expires_at: float = 0.0
    refresh_expires_at: float = 0.0
    scope: str = ""

    @property
    def expired(self) -> bool:
        return bool(self.expires_at) and time.time() >= self.expires_at - EXPIRY_SKEW

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PinToken:
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})

    @classmethod
    def from_response(cls, payload: dict[str, Any], previous: PinToken | None = None) -> PinToken:
        now = time.time()
        refresh_in = payload.get("refresh_token_expires_in")
        return cls(
            access_token=payload["access_token"],
            # A refresh response may omit the refresh token when it has not rolled.
            refresh_token=payload.get("refresh_token") or (previous.refresh_token if previous else ""),
            expires_at=now + float(payload.get("expires_in", 0) or 0),
            refresh_expires_at=(
                now + float(refresh_in) if refresh_in
                else float(payload.get("refresh_token_expires_at") or (previous.refresh_expires_at if previous else 0))
            ),
            scope=payload.get("scope", "") or (previous.scope if previous else ""),
        )


def load_token() -> PinToken | None:
    data = read_json(token_path())
    return PinToken.from_dict(data) if data else None


def save_token(token: PinToken) -> None:
    write_json_private(token_path(), token.to_dict())


def clear_token() -> bool:
    path = token_path()
    if path.exists():
        path.unlink()
        return True
    return False


def _basic_auth(config: PinterestConfig) -> str:
    raw = f"{config.app_id}:{config.app_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _post_token(config: PinterestConfig, form: dict[str, str], http: httpx.Client | None = None) -> dict[str, Any]:
    config.require_app()
    own = http is None
    http = http or httpx.Client(timeout=30.0)
    try:
        resp = http.post(TOKEN_URL, data=form, headers={"Authorization": _basic_auth(config)})
    except httpx.HTTPError as exc:
        raise AuthError(f"Could not reach Pinterest's token endpoint: {exc}") from exc
    finally:
        if own:
            http.close()
    if resp.status_code != 200:
        raise AuthError(f"Pinterest rejected the token request ({resp.status_code}): {resp.text[:300]}")
    return resp.json()


def exchange_code(config: PinterestConfig, code: str, http: httpx.Client | None = None) -> PinToken:
    payload = _post_token(
        config,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": config.redirect_uri,
            # Roll the refresh token on every refresh, so a tool used at least once a
            # year never has to send the seller back through the consent screen.
            "continuous_refresh": "true",
        },
        http,
    )
    token = PinToken.from_response(payload)
    save_token(token)
    return token


def refresh(config: PinterestConfig, token: PinToken, http: httpx.Client | None = None) -> PinToken:
    if not token.refresh_token:
        raise AuthError("This Pinterest token cannot be refreshed. Run: etsyotomasyon pinterest login")
    payload = _post_token(
        config, {"grant_type": "refresh_token", "refresh_token": token.refresh_token}, http
    )
    fresh = PinToken.from_response(payload, previous=token)
    save_token(fresh)
    return fresh


# --- consent flow --------------------------------------------------------------


def authorization_url(config: PinterestConfig, state: str) -> str:
    query = urllib.parse.urlencode(
        {
            "client_id": config.app_id,
            "redirect_uri": config.redirect_uri,
            "response_type": "code",
            "scope": ",".join(SCOPES),
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def code_from_redirect(text: str, expected_state: str) -> str:
    """Pull the code out of a pasted redirect address, checking the state."""
    raw = (text or "").strip().strip('"').strip("'")
    if not raw:
        raise AuthError("Nothing pasted.")
    query = raw.split("?", 1)[1] if "?" in raw else raw
    params = urllib.parse.parse_qs(query)
    if "error" in params:
        raise AuthError(f"Pinterest returned an error: {params['error'][0]}")
    if not params.get("code"):
        if " " not in raw and "=" not in raw:
            return raw  # a bare code
        raise AuthError("That address has no `code=` in it. Copy the full address Pinterest sent you to.")
    state = params.get("state", [None])[0]
    if state and state != expected_state:
        raise AuthError("The state value does not match the request we started. Run login again.")
    return params["code"][0]


class _Callback(http.server.BaseHTTPRequestHandler):
    result: dict[str, str] = {}
    # The state of the consent request being waited for. Only a request carrying it
    # is recorded (and sent on to the app); any other is refused and the wait goes on,
    # so a link or <img> on some web page cannot end the flow or fake a success.
    # None records any answer (a handler used on its own).
    expected_state: str | None = None
    # Where to send the browser once Pinterest answers with a code — the web app's
    # page that closes the tab. None keeps the plain page below (the CLI).
    return_url: str | None = None
    timeout = 2  # see auth._CallbackHandler: an idle browser socket must not block Cancel

    def do_GET(self) -> None:  # noqa: N802 — name fixed by BaseHTTPRequestHandler
        query = urllib.parse.urlparse(self.path).query
        params = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
        if "code" not in params and "error" not in params:
            # The browser's favicon request, or a speculative connection: not Pinterest's
            # answer, and it must not wipe out an answer that already arrived.
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not state_matches(params.get("state"), type(self).expected_state):
            body = listener_page(FOREIGN_TITLE, FOREIGN_DETAIL)
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        type(self).result = params
        # Only a success goes back to the app: an error keeps the page below.
        if "code" in params and "error" not in params and self.return_url:
            self.send_response(302)
            self.send_header("Location", self.return_url)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            # The code is in this address: the app's page must not get it as a referrer.
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            return
        if "code" in params:
            body = b"<p>Pinterest is connected. You can close this tab.</p>"
        else:
            reason = html.escape(params.get("error_description") or params["error"])
            body = f"<p>Pinterest did not connect: {reason}</p>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        pass


def _listen_for_code(
    config: PinterestConfig, state: str, timeout: float, cancel: threading.Event | None = None
) -> str:
    parsed = urllib.parse.urlparse(config.redirect_uri)
    port = parsed.port or 80
    _Callback.result = {}
    try:
        server = LoopbackServer(("127.0.0.1", port), _Callback)
    except OSError as exc:
        raise AuthError(f"Cannot listen on port {port} ({exc}). Use --paste instead.") from exc
    _Callback.expected_state = state  # a forged answer is refused and the wait goes on
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.4}, daemon=True)
    thread.start()
    deadline = time.time() + timeout
    try:
        while time.time() < deadline and not _Callback.result:
            if cancel is not None and cancel.is_set():
                break
            time.sleep(0.25)
    finally:
        server.shutdown()
        server.server_close()
        _Callback.expected_state = None
    result = dict(_Callback.result)
    if not result and cancel is not None and cancel.is_set():
        raise AuthError("Cancelled before Pinterest sent the browser back. Nothing was changed.")
    if not result:
        raise AuthError(f"Timed out after {int(timeout)}s waiting for Pinterest's redirect.")
    if "error" in result:
        raise AuthError(f"Pinterest returned an error: {result['error']}")
    if result.get("state") != state:
        raise AuthError("State mismatch — the redirect did not come from the request we started.")
    return result["code"]


def login(
    config: PinterestConfig,
    *,
    paste: bool = False,
    open_browser: bool = True,
    timeout: float = 300.0,
    prompt: Callable[[str], str] = input,
    cancel: threading.Event | None = None,
) -> PinToken:
    config.require_app()
    state = secrets.token_urlsafe(16)
    url = authorization_url(config, state)
    print("Open this URL to connect your Pinterest account:\n", flush=True)
    print(f"  {url}\n", flush=True)
    if open_browser:
        try:
            webbrowser.open(url)
        except (webbrowser.Error, OSError):
            pass
    host = (urllib.parse.urlparse(config.redirect_uri).hostname or "").lower()
    if not paste and host in {"localhost", "127.0.0.1"}:
        code = _listen_for_code(config, state, timeout, cancel)
    else:
        code = code_from_redirect(prompt("Paste the full address you were sent to: "), state)
    return exchange_code(config, code)


# --- the API client ------------------------------------------------------------


class PinterestClient:
    def __init__(
        self,
        config: PinterestConfig,
        *,
        token: PinToken | None = None,
        http: httpx.Client | None = None,
        per_second: float = 1.0,
    ) -> None:
        self.config = config
        if config.access_token:
            token = PinToken(access_token=config.access_token)
        self.token = token or load_token()
        if self.token is None:
            raise AuthError("Pinterest is not connected. Run: etsyotomasyon pinterest login")
        self._http = http or httpx.Client(timeout=60.0)
        self.limiter = RateLimiter(per_second)

    def __enter__(self) -> PinterestClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _ensure_fresh(self) -> None:
        if self.token.expired and self.token.refresh_token and not self.config.access_token:
            self.token = refresh(self.config, self.token)

    def request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                json_body: dict[str, Any] | None = None) -> Any:
        """One API call. Reads are retried; a write is retried only when Pinterest
        provably refused it (429) or it provably never left (connect failure)."""
        url = f"{self.config.base}{path}"
        idempotent = method.upper() == "GET"
        refreshed = False
        self._ensure_fresh()
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self.limiter.acquire()
            try:
                resp = self._http.request(
                    method, url, params=params, json=json_body,
                    headers={"Authorization": f"Bearer {self.token.access_token}"},
                )
            except httpx.HTTPError as exc:
                never_arrived = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
                if attempt == MAX_ATTEMPTS or not (idempotent or never_arrived):
                    message = f"network error: {exc}"
                    if not idempotent and not never_arrived:
                        message += " — Pinterest may have created it anyway."
                    raise PinterestApiError(0, message, method=method, path=path) from exc
                time.sleep(min(30.0, 2 ** (attempt - 1)))
                continue

            if resp.status_code < 300:
                return resp.json() if resp.content else None

            if (resp.status_code == 401 and not refreshed and self.token.refresh_token
                    and not self.config.access_token):
                refreshed = True
                self.token = refresh(self.config, self.token)
                continue

            message = _error_message(resp)
            retryable = resp.status_code == 429 or (resp.status_code >= 500 and idempotent)
            if not retryable or attempt == MAX_ATTEMPTS:
                raise PinterestApiError(resp.status_code, message, method=method, path=path)
            retry_after = resp.headers.get("Retry-After", "")
            time.sleep(float(retry_after) if retry_after.isdigit() else min(30.0, 2 ** (attempt - 1)))
        raise PinterestApiError(0, "exhausted retries", method=method, path=path)

    def boards(self) -> Iterator[dict[str, Any]]:
        bookmark: str | None = None
        while True:
            params = {"page_size": 100}
            if bookmark:
                params["bookmark"] = bookmark
            page = self.request("GET", "/boards", params=params) or {}
            yield from page.get("items") or []
            bookmark = page.get("bookmark")
            if not bookmark:
                return

    def create_pin(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/pins", json_body=payload)


def _error_message(resp: httpx.Response) -> str:
    try:
        payload = resp.json()
    except ValueError:
        return (resp.text or resp.reason_phrase or "unknown error").strip()[:300]
    if isinstance(payload, dict) and payload.get("message"):
        return str(payload["message"])[:300]
    return str(payload)[:300]


def resolve_board(boards: Iterable[dict[str, Any]], wanted: str) -> dict[str, Any]:
    """Accept a board id or its exact name (case-insensitive)."""
    items = list(boards)
    for board in items:
        if str(board.get("id")) == wanted:
            return board
    matches = [b for b in items if (b.get("name") or "").strip().casefold() == wanted.strip().casefold()]
    if len(matches) == 1:
        return matches[0]
    names = ", ".join(sorted(b.get("name", "?") for b in items)) or "none"
    if matches:
        raise ValidationError(f"More than one board is called {wanted!r}; use its id instead.")
    raise ValidationError(f"No board called {wanted!r}. Your boards: {names}")


# --- turning a listing into Pins -----------------------------------------------


def parse_ranks(spec: str | None) -> set[int] | None:
    """`1-6`, `1,3,5` or `2-4,7` → a set of image ranks. None means every image."""
    if not spec:
        return None
    ranks: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                low, high = (int(x) for x in part.split("-", 1))
                if low > high:
                    raise ValueError
                ranks.update(range(low, high + 1))
            else:
                ranks.add(int(part))
        except ValueError as exc:
            raise ValidationError(f"--images takes ranks like 1-6 or 1,3,5, not {spec!r}") from exc
    if not ranks or min(ranks) < 1:
        raise ValidationError(f"--images takes ranks starting at 1, not {spec!r}")
    return ranks


def pin_title(listing_title: str) -> str:
    """The listing title cut to Pinterest's 100 characters on a `|` or word boundary.

    Etsy titles run to 140 and front-load the keyword, so whole segments are kept from
    the left and the least important trailing ones are what gets dropped.
    """
    title = " ".join((listing_title or "").split())
    if len(title) <= MAX_TITLE_LEN:
        return title
    kept = ""
    for segment in (s.strip() for s in title.split("|")):
        candidate = f"{kept} | {segment}" if kept else segment
        if len(candidate) > MAX_TITLE_LEN:
            break
        kept = candidate
    if kept:
        return kept
    return title[:MAX_TITLE_LEN].rsplit(" ", 1)[0]


def pin_description(listing_title: str, tags: Iterable[str], override: str | None = None) -> str:
    """Keyword-carrying copy within 800 characters.

    A listing description usually opens with shop logistics, which is not what a Pin
    should say. The title's phrases plus the listing's own tags are the search terms the
    seller already chose, so the default is built from those.
    """
    if override:
        text = " ".join(override.split())
    else:
        phrases = [p.strip() for p in (listing_title or "").split("|") if p.strip()]
        text = ". ".join(phrases)
        tag_list = [t for t in tags if t]
        if tag_list:
            text = f"{text}. {', '.join(tag_list)}"
    if len(text) <= MAX_DESCRIPTION_LEN:
        return text
    return text[:MAX_DESCRIPTION_LEN].rsplit(" ", 1)[0].rstrip(",.;")


def pins_for_listing(
    listing: dict[str, Any],
    images: list[dict[str, Any]],
    board_id: str,
    *,
    ranks: set[int] | None = None,
    ai_modified: bool = False,
    description: str | None = None,
) -> list[dict[str, Any]]:
    """One Pin payload per chosen image, each linking back to the listing."""
    if listing.get("state") != "active":
        raise ValidationError(
            f"Listing {listing.get('listing_id')} is {listing.get('state')!r}, not active — "
            "a Pin to it would lead nowhere. Publish it first."
        )
    link = (listing.get("url") or "").split("?", 1)[0]
    if not link:
        raise ValidationError(f"Listing {listing.get('listing_id')} has no public URL.")
    title = pin_title(listing.get("title", ""))
    body = pin_description(listing.get("title", ""), listing.get("tags") or [], description)
    chosen = sorted(
        (img for img in images if ranks is None or img.get("rank") in ranks),
        key=lambda img: img.get("rank") or 0,
    )
    if not chosen:
        raise ValidationError(f"Listing {listing.get('listing_id')} has no images at those ranks.")
    pins = []
    for img in chosen:
        url = img.get("url_fullxfull") or img.get("url_570xN")
        if not url:
            continue
        payload: dict[str, Any] = {
            "board_id": str(board_id),
            "title": title,
            "description": body,
            "link": link[:MAX_LINK_LEN],
            "alt_text": title[:MAX_ALT_TEXT_LEN],
            "media_source": {"source_type": "image_url", "url": url},
        }
        if ai_modified:
            payload["ai_disclosures"] = {"values": [AI_MODIFIED]}
        pins.append({"listing_id": listing.get("listing_id"), "rank": img.get("rank"), "payload": payload})
    return pins


# --- the queue -----------------------------------------------------------------


def queue_path() -> Path:
    return home_dir() / "pinterest-queue.json"


# Pins queued before 0.3.0 carry the listing title as Etsy sent it, HTML-escaped
# ("Mom&#39;s Mug &amp; Gift"), in their title, description and alt text. Entries queued
# since hold plain text and carry this key. An entry without it is decoded once (when the
# queue is read, and before it is sent) and then marked, so a Pin never goes out with
# "&#39;" in it and a seller's own literal "&amp;" in a newer Pin is left alone.
PLAIN_TEXT = "plain_text"
PAYLOAD_TEXT_FIELDS = ("title", "description", "alt_text")


def plain_entry(entry: dict[str, Any]) -> bool:
    """Decode an old queue entry's escaped Pin text in place and mark it.

    True when the entry was an old one (it changed and should be saved)."""
    if not isinstance(entry, dict) or entry.get(PLAIN_TEXT) is True:
        return False
    payload = entry.get("payload")
    if isinstance(payload, dict):
        for name in PAYLOAD_TEXT_FIELDS:
            if isinstance(payload.get(name), str):
                payload[name] = unescape_text(payload[name])
    entry[PLAIN_TEXT] = True
    return True


@dataclass
class Queue:
    path: Path
    entries: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path | None = None) -> Queue:
        path = path or queue_path()
        if not path.exists():
            return cls(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise ValidationError(f"Cannot read {path}; fix or move it before queueing more.") from exc
        if not isinstance(data, list):
            raise ValidationError(f"{path} is not a Pin queue.")
        for entry in data:
            plain_entry(entry)  # in memory; the next save() writes it back decoded
        return cls(path, data)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.entries, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def keys(self) -> set[str]:
        return {e["key"] for e in self.entries}

    def add(self, pins: list[dict[str, Any]], *, start: date, per_day: int) -> list[dict[str, Any]]:
        """Schedule each Pin on the earliest day, from `start`, that still has room.

        The daily limit counts everything in the queue, not just this batch, so
        queueing three listings in one sitting still comes out at `per_day` a day.
        Anything already queued for the same image and board is skipped.
        """
        if per_day < 1:
            raise ValidationError("--per-day must be at least 1.")
        load: dict[str, int] = {}
        for e in self.entries:
            if e["status"] in {"pending", "posted", "sending"}:
                load[e["due"]] = load.get(e["due"], 0) + 1
        known = self.keys()
        added = []
        day = start
        for pin in pins:
            key = f"{pin['listing_id']}:{pin['rank']}:{pin['payload']['board_id']}"
            if key in known:
                continue
            while load.get(day.isoformat(), 0) >= per_day:
                day += timedelta(days=1)
            entry = {
                "key": key,
                "listing_id": pin["listing_id"],
                "rank": pin["rank"],
                "due": day.isoformat(),
                "status": "pending",
                "pin_id": None,
                "message": "",
                "payload": pin["payload"],
                PLAIN_TEXT: True,  # built from decoded listing text (see plain_entry)
            }
            self.entries.append(entry)
            load[entry["due"]] = load.get(entry["due"], 0) + 1
            known.add(key)
            added.append(entry)
        return added

    def due(self, today: date) -> list[dict[str, Any]]:
        return [e for e in self.entries if e["status"] == "pending" and e["due"] <= today.isoformat()]

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.entries:
            out[e["status"]] = out.get(e["status"], 0) + 1
        return out


def post_due(
    client: PinterestClient,
    queue: Queue,
    *,
    today: date,
    limit: int | None = None,
    dry_run: bool = False,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    """Send the Pins that are due. Each is marked `sending` on disk before the request,
    so a crash mid-call leaves a record rather than a silent retry tomorrow."""
    batch = queue.due(today)
    if limit is not None:
        batch = batch[:limit]
    for entry in batch:
        # A Pin queued before 0.3.0 holds Etsy's escaped title: decode it before it
        # goes out (saved with the `sending` mark below).
        plain_entry(entry)
        if dry_run:
            if on_progress:
                on_progress(entry)
            continue
        entry["status"] = "sending"
        queue.save()
        try:
            created = client.create_pin(entry["payload"]) or {}
            entry["status"] = "posted"
            entry["pin_id"] = created.get("id")
            entry["message"] = ""
        except PinterestApiError as exc:
            # A 4xx other than 429 is a definite refusal: nothing was created. Anything
            # else may have landed, so it is parked for a human rather than retried.
            definite = 400 <= exc.status < 500 and exc.status != 429
            entry["status"] = "failed" if definite else "uncertain"
            entry["message"] = exc.message
        queue.save()
        if on_progress:
            on_progress(entry)
    return batch
