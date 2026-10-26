"""HTTP client for the Etsy Open API v3.

Handles the four things every caller would otherwise reimplement badly:
throttling under the app's per-second ceiling (5 QPS on Personal Access, higher with
commercial access), retry with backoff that never repeats a non-idempotent write,
transparent token refresh on 401, and offset pagination.
"""

from __future__ import annotations

import html
import random
import threading
import time
from collections import deque
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx

from . import auth
from .config import API_BASE, MAX_PAGE_LIMIT, Config
from .errors import AuthError, EtsyApiError, ValidationError

MAX_ATTEMPTS = 5
# The marketplace search (findAllListingsActive) only pages through its first 12,000
# results; walking past that returns errors, not pages. The OAS documents no such
# window (it gives every `offset` only min=0), and the shop's own collections —
# listings, receipts, the payment ledger — have none, so only the search passes it.
MAX_SEARCH_OFFSET = 12_000
# getListingsByListingIds: "Limit 100 ids maximum per query" (Etsy OpenAPI spec). The
# same ceiling is used for the other id-list parameters, which document none.
MAX_BATCH_IDS = 100

# Etsy sends the seller's own words HTML-escaped: a title typed as `Mom's Mug & Gift`
# comes back as `Mom&#39;s Mug &amp; Gift` (the OAS types these as plain strings and
# does not say so). They are decoded exactly once, here, where they are read, so the
# rest of etsyotomasyon — the editors, the SEO audit and fix, templates, drafts, Pins, CSV
# — only ever sees and sends plain text. Etsy stores what it is sent as-is, so sending
# the escaped form back would put a literal "&#39;" on the live listing.
LISTING_TEXT_FIELDS = ("title", "description", "suggested_title")
LISTING_LIST_FIELDS = ("tags", "materials", "style")
RECEIPT_TEXT_FIELDS = ("name", "city", "state", "message_from_buyer", "gift_message")
TRANSACTION_TEXT_FIELDS = ("title", "description")


def unescape_text(value: Any) -> Any:
    """`value` with HTML entities decoded when it is a string; anything else unchanged."""
    if isinstance(value, str) and "&" in value:
        return html.unescape(value)
    return value


def _unescape_fields(record: dict[str, Any], names: Iterable[str]) -> None:
    for name in names:
        if isinstance(record.get(name), str):
            record[name] = unescape_text(record[name])


def unescape_listing(listing: Any) -> Any:
    """Decode the text fields of one ShopListing (in place) and return it."""
    if not isinstance(listing, dict):
        return listing
    _unescape_fields(listing, LISTING_TEXT_FIELDS)
    for name in LISTING_LIST_FIELDS:
        values = listing.get(name)
        if isinstance(values, list):
            listing[name] = [unescape_text(v) for v in values]
    return listing


def unescape_transaction(transaction: Any) -> Any:
    """Decode a ShopReceiptTransaction's title, description and variations (in place)."""
    if not isinstance(transaction, dict):
        return transaction
    _unescape_fields(transaction, TRANSACTION_TEXT_FIELDS)
    for variation in transaction.get("variations") or []:
        if isinstance(variation, dict):
            _unescape_fields(variation, ("formatted_name", "formatted_value"))
    return transaction


def unescape_receipt(receipt: Any) -> Any:
    """Decode a ShopReceipt's text and its transactions' (in place) and return it."""
    if not isinstance(receipt, dict):
        return receipt
    _unescape_fields(receipt, RECEIPT_TEXT_FIELDS)
    for transaction in receipt.get("transactions") or []:
        unescape_transaction(transaction)
    return receipt


def _unescape_titles(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shipping profiles and shop sections: the seller named them, Etsy escaped the name."""
    for record in records:
        if isinstance(record, dict):
            _unescape_fields(record, ("title",))
    return records


def encode_form(data: dict[str, Any]) -> dict[str, str]:
    """Shape a dict for Etsy's application/x-www-form-urlencoded endpoints.

    Etsy documents list fields (tags, materials, image_ids) as *comma-separated
    strings*, not repeated keys — sending repeated keys silently drops all but one
    value. Booleans must be lowercase literals. None means "leave unset".
    """
    out: dict[str, str] = {}
    for key, value in data.items():
        if value is None:
            continue
        if isinstance(value, bool):
            out[key] = "true" if value else "false"
        elif isinstance(value, (list, tuple)):
            if not value:
                continue
            out[key] = ",".join(str(v) for v in value)
        else:
            out[key] = str(value)
    return out


class RateLimiter:
    """Sliding-window limiter. Thread-safe so callers may parallelise later."""

    def __init__(self, per_second: float) -> None:
        self.per_second = max(0.5, per_second)
        self._times: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            while True:
                now = time.monotonic()
                while self._times and now - self._times[0] >= 1.0:
                    self._times.popleft()
                if len(self._times) < self.per_second:
                    self._times.append(now)
                    return
                sleep_for = 1.0 - (now - self._times[0])
                if sleep_for > 0:
                    time.sleep(sleep_for)


class EtsyClient:
    """One Etsy app plus one sign-in. Safe to share between threads.

    The web app keeps a single instance per shop and lets every request handler and
    background job use it, so there is one RateLimiter (Etsy's per-app QPS holds
    across all of them) and one token: a refresh happens once, under a lock, however
    many threads notice the expiry at the same moment. httpx.Client is thread-safe.
    """

    def __init__(
        self,
        config: Config,
        *,
        token: auth.Token | None = None,
        require_auth: bool = True,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.config = config
        self.token = token if token is not None else auth.load_token()
        if require_auth and self.token is None:
            raise AuthError("Not authenticated. Run: etsyotomasyon auth login")
        self.limiter = RateLimiter(config.rate_per_sec)
        # `transport` is for tests (httpx.MockTransport); production never passes one.
        self._http = httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0), transport=transport)
        self._shop_id: int | None = config.shop_id
        self._me: dict[str, Any] | None = None
        self.quota_remaining: int | None = None
        # One lock for the token (the expiry refresh and the refresh after a 401), a
        # second for the lazy identity caches, so resolving the shop once does not
        # hold up every other thread's requests. Both re-entrant.
        self._lock = threading.RLock()
        self._identity_lock = threading.RLock()
        self._local = threading.local()

    def __enter__(self) -> EtsyClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    @contextmanager
    def attempts(self, limit: int) -> Iterator[EtsyClient]:
        """Cap the tries per request, on the calling thread only.

        A GET is normally tried up to MAX_ATTEMPTS times with growing pauses, which
        is right for a batch and far too slow for "is Etsy reachable right now?".
        Other threads sharing this client keep the normal behaviour.
        """
        previous = getattr(self._local, "attempts", None)
        self._local.attempts = max(1, int(limit))
        try:
            yield self
        finally:
            self._local.attempts = previous

    # --- core request machinery -------------------------------------------------

    def _current_token(self) -> auth.Token:
        """The token to send, refreshed first if it is about to expire.

        Expiry is checked inside the lock, so when two threads find the token stale
        together the second one picks up the first one's fresh token instead of
        spending the refresh token a second time.
        """
        with self._lock:
            if self.token is None:
                raise AuthError("This command needs authentication. Run: etsyotomasyon auth login")
            if self.token.expired:
                self.token = auth.refresh(self.token, self.config)
            return self.token

    def _refresh_after_401(self, sent: str) -> None:
        """Etsy refused the access token `sent`: refresh once, unless another thread has."""
        with self._lock:
            if self.token is not None and self.token.access_token == sent:
                self.token = auth.refresh(self.token, self.config)

    def _headers(self, *, authed: bool) -> dict[str, str]:
        headers = {
            # Both halves, colon-joined. See Config.api_key_header for why.
            "x-api-key": self.config.api_key_header,
            "Accept": "application/json",
            "User-Agent": "etsyotomasyon/0.1 (+https://github.com/wj-sidiss/etsy-otomasyon-full-full-surum-2)",
        }
        if authed:
            headers["Authorization"] = f"Bearer {self._current_token().access_token}"
        return headers

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        form: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        authed: bool = True,
        retry: bool | None = None,
    ) -> Any:
        url = path if path.startswith("http") else f"{API_BASE}{path}"
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        body = encode_form(form) if form is not None else None
        refreshed_once = False

        # Etsy has no idempotency key, so a re-sent write is a real duplicate: a second
        # draft listing, a second image, a second shipment plus a second "your order
        # shipped" email to the buyer. A read timeout does NOT mean Etsy rejected the
        # request — it may have committed. So writes are only retried when the request
        # provably never arrived (connect failure) or was provably refused (429).
        idempotent = method.upper() in {"GET", "HEAD", "OPTIONS"}
        may_retry = idempotent if retry is None else retry
        max_attempts = getattr(self._local, "attempts", None) or MAX_ATTEMPTS

        attempt = 0
        while True:
            attempt += 1
            self.limiter.acquire()
            headers = self._headers(authed=authed)
            try:
                resp = self._http.request(
                    method,
                    url,
                    params=clean_params or None,
                    data=body,
                    json=json_body,
                    files=files,
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                # A connection that was never established cannot have changed anything.
                never_arrived = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
                if attempt >= max_attempts or not (may_retry or never_arrived):
                    message = f"network error: {exc}"
                    if not idempotent and not never_arrived:
                        message += (
                            " — the request may still have been accepted by Etsy. "
                            "Check your shop before running this again."
                        )
                    raise EtsyApiError(0, message, method=method, path=path) from exc
                time.sleep(self._backoff(attempt))
                continue

            remaining = resp.headers.get("x-remaining-today")
            if remaining and remaining.isdigit():
                self.quota_remaining = int(remaining)

            if resp.status_code < 300:
                if not resp.content:
                    return None
                try:
                    return resp.json()
                except ValueError:
                    return resp.text

            # An expired token that we did not predict — refresh once, then send again.
            # That re-send is not a retry: it does not use up an attempt, so it happens
            # under attempts(1) too (Etsy refused the request, it did not act on it).
            if resp.status_code == 401 and authed and not refreshed_once and self.token:
                refreshed_once = True
                sent = headers.get("Authorization", "")[len("Bearer "):]
                self._refresh_after_401(sent)
                attempt -= 1
                continue

            error = EtsyApiError(
                resp.status_code,
                self._error_message(resp),
                method=method,
                path=path,
                body=resp.text[:2000],
            )
            # 429 is always safe to retry: it means Etsy refused, not that it acted.
            # A 5xx on a write may mean the write landed, so do not repeat it.
            retryable = resp.status_code == 429 or (resp.status_code >= 500 and may_retry)
            if not retryable or attempt >= max_attempts:
                raise error

            retry_after = resp.headers.get("Retry-After")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else self._backoff(attempt)
            time.sleep(delay)

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(30.0, (2 ** (attempt - 1)) + random.uniform(0, 0.6))

    @staticmethod
    def _error_message(resp: httpx.Response) -> str:
        try:
            payload = resp.json()
        except ValueError:
            return (resp.text or resp.reason_phrase or "unknown error").strip()[:400]
        if isinstance(payload, dict):
            for key in ("error", "error_description", "message"):
                if payload.get(key):
                    return str(payload[key])[:400]
        return str(payload)[:400]

    def get(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> Any:
        return self.request("POST", path, **kw)

    def patch(self, path: str, **kw: Any) -> Any:
        return self.request("PATCH", path, **kw)

    def put(self, path: str, **kw: Any) -> Any:
        return self.request("PUT", path, **kw)

    def paginate(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        max_items: int | None = None,
        authed: bool = True,
        page_size: int = MAX_PAGE_LIMIT,
        max_offset: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Walk an offset-paginated collection, yielding one record at a time.

        It stops at `max_items`, at the collection's `count`, or at a short page.
        `max_offset` is for an endpoint that cannot page past a window (the
        marketplace search); a shop's own collections are read to the end.
        """
        offset = 0
        seen = 0
        page_size = min(page_size, MAX_PAGE_LIMIT)
        while True:
            page_params = dict(params or {})
            page_params.update(limit=page_size, offset=offset)
            payload = self.get(path, params=page_params, authed=authed)
            results = (payload or {}).get("results") or []
            if not results:
                return
            for item in results:
                yield item
                seen += 1
                if max_items is not None and seen >= max_items:
                    return
            offset += len(results)
            total = (payload or {}).get("count")
            if isinstance(total, int) and offset >= total:
                return
            if len(results) < page_size:
                return
            if max_offset is not None and offset >= max_offset:
                return

    # --- identity ---------------------------------------------------------------

    def me(self) -> dict[str, Any]:
        with self._identity_lock:
            if self._me is None:
                self._me = self.get("/users/me")
            return self._me

    def shop_id(self) -> int:
        """Resolve the shop to operate on: ETSY_SHOP_ID if set, else the token owner's shop."""
        with self._identity_lock:
            return self._resolve_shop_id()

    def _resolve_shop_id(self) -> int:
        if self._shop_id is not None:
            return self._shop_id
        user_id = self.me().get("user_id") or (self.token.user_id if self.token else None)
        if not user_id:
            raise AuthError("Could not determine your Etsy user id. Try: etsyotomasyon auth login")
        shop = self.get(f"/users/{user_id}/shops")
        # Etsy has returned this either as a bare shop object or wrapped in results.
        if isinstance(shop, dict) and shop.get("results"):
            shop = shop["results"][0]
        shop_id = (shop or {}).get("shop_id")
        if not shop_id:
            raise AuthError(
                "No shop is attached to this Etsy account. Open a shop first, "
                "or set ETSY_SHOP_ID in your .env."
            )
        self._shop_id = int(shop_id)
        return self._shop_id

    def shop(self) -> dict[str, Any]:
        return self.get(f"/shops/{self.shop_id()}")

    # --- endpoints used across commands ----------------------------------------

    def ping(self) -> Any:
        """Key-only health check — proves the keystring works before OAuth exists."""
        return self.get("/openapi-ping", authed=False)

    def shipping_profiles(self) -> list[dict[str, Any]]:
        payload = self.get(f"/shops/{self.shop_id()}/shipping-profiles")
        return _unescape_titles((payload or {}).get("results") or [])

    def return_policies(self) -> list[dict[str, Any]]:
        payload = self.get(f"/shops/{self.shop_id()}/policies/return")
        return (payload or {}).get("results") or []

    def shop_sections(self) -> list[dict[str, Any]]:
        payload = self.get(f"/shops/{self.shop_id()}/sections")
        return _unescape_titles((payload or {}).get("results") or [])

    def shipping_carriers(self, origin_country_iso: str) -> list[dict[str, Any]]:
        payload = self.get(
            "/shipping-carriers", params={"origin_country_iso": origin_country_iso}
        )
        return (payload or {}).get("results") or []

    def taxonomy_nodes(self) -> list[dict[str, Any]]:
        payload = self.get("/seller-taxonomy/nodes", authed=False)
        return (payload or {}).get("results") or []

    def listings_by_shop(
        self,
        state: str = "active",
        *,
        includes: Sequence[str] | None = None,
        max_items: int | None = None,
        sort_on: str | None = None,
        sort_order: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """getListingsByShop (listings_r).

        OAS enums: state active|inactive|sold_out|draft|removed|expired (default
        active); sort_on created|price|updated|score (default created); sort_order
        asc|ascending|desc|descending|up|down (default desc); includes Shipping,
        Images, Shop, User, Translations, Inventory, Videos, Personalization,
        BuyerPrice. None leaves a parameter to Etsy's default.
        """
        params: dict[str, Any] = {"state": state, "sort_on": sort_on, "sort_order": sort_order}
        if includes:
            params["includes"] = ",".join(includes)
        for listing in self.paginate(
            f"/shops/{self.shop_id()}/listings",
            params={k: v for k, v in params.items() if v is not None},
            max_items=max_items,
        ):
            yield unescape_listing(listing)

    def count_listings(self, state: str = "active") -> int:
        """How many listings the shop has in `state`, from one limit=1 request."""
        payload = self.get(
            f"/shops/{self.shop_id()}/listings", params={"state": state, "limit": 1, "offset": 0}
        )
        return _count(payload)

    def listing(
        self, listing_id: int, *, includes: Sequence[str] | None = None, authed: bool = False
    ) -> dict[str, Any]:
        """One listing by id (getListing). The OAS gives it API-key security only.

        `authed=True` sends the sign-in as well, for the seller's own draft or inactive
        listing, which the key-only view may hide.
        """
        params = {"includes": ",".join(includes)} if includes else None
        return unescape_listing(self.get(f"/listings/{listing_id}", params=params, authed=authed))

    def listing_images(self, listing_id: int) -> list[dict[str, Any]]:
        """getListingImages: every image of a listing, ordered by rank.

        The spec lists no OAuth scope (API key only). The token is still sent when
        there is one, so a draft or inactive listing of the seller's own shop — which
        the public view may hide — is readable too.
        """
        payload = self.get(f"/listings/{listing_id}/images", authed=self.token is not None)
        images = (payload or {}).get("results") or []
        for image in images:
            if isinstance(image, dict):
                # The seller wrote it; Etsy escapes it like a title ("Size &amp; care").
                _unescape_fields(image, ("alt_text",))
        return sorted(images, key=lambda image: image.get("rank") or 0)

    def download_image(self, url: str, *, max_bytes: int | None = None) -> bytes:
        """A listing photo's bytes from Etsy's image CDN (a ListingImage's url_fullxfull).

        The CDN is public and is not the API: neither the API key nor the sign-in is
        sent, and no other host is fetched (is_etsy_image_url). A GET, so it is retried
        like one; a file over `max_bytes` (Etsy's own 20 MB image limit) is refused while
        it streams. Raises ValidationError for a refused URL or size, EtsyApiError for an
        HTTP error (status 0: the network, or any answer that is not a 2xx: a redirect is
        not followed, and its body is never taken for the picture).
        """
        if not is_etsy_image_url(url):
            raise ValidationError(f"Not an Etsy image address: {str(url)[:120]}")
        limit = MAX_IMAGE_BYTES if max_bytes is None else max_bytes
        max_attempts = getattr(self._local, "attempts", None) or MAX_ATTEMPTS
        headers = {"Accept": "image/*", "User-Agent": self._headers(authed=False)["User-Agent"]}
        attempt = 0
        while True:
            attempt += 1
            try:
                with self._http.stream("GET", url, headers=headers) as resp:
                    if not resp.is_success:
                        retryable = resp.status_code == 429 or resp.status_code >= 500
                        if not retryable or attempt >= max_attempts:
                            raise EtsyApiError(
                                resp.status_code, f"the image could not be downloaded "
                                f"(HTTP {resp.status_code})", method="GET", path=url,
                            )
                    else:
                        data = bytearray()
                        for chunk in resp.iter_bytes():
                            data += chunk
                            if len(data) > limit:
                                raise ValidationError(
                                    f"The image is larger than {limit // 1024 // 1024} MB."
                                )
                        return bytes(data)
            except httpx.HTTPError as exc:
                if attempt >= max_attempts:
                    raise EtsyApiError(0, f"network error: {exc}", method="GET", path=url) from exc
            time.sleep(self._backoff(attempt))

    def listings_batch(
        self, listing_ids: Iterable[int], *, includes: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        """getListingsByListingIds: many listings, at most 100 ids per request.

        Chunked here, so any number of ids may be passed; duplicates are asked once.
        OAS includes enum: Images, Shop, User, Translations, Videos, Personalization,
        BuyerPrice (no Inventory or Shipping on this endpoint). API key only.
        """
        ids = list(dict.fromkeys(int(i) for i in listing_ids))
        out: list[dict[str, Any]] = []
        for start in range(0, len(ids), MAX_BATCH_IDS):
            chunk = ids[start : start + MAX_BATCH_IDS]
            params: dict[str, Any] = {"listing_ids": ",".join(str(i) for i in chunk)}
            if includes:
                params["includes"] = ",".join(includes)
            payload = self.get("/listings/batch", params=params, authed=self.token is not None)
            out.extend(unescape_listing(item) for item in (payload or {}).get("results") or [])
        return out

    def create_draft_listing(self, fields: dict[str, Any]) -> dict[str, Any]:
        # `fields` is plain text (see LISTING_TEXT_FIELDS); Etsy's answer is escaped again.
        return unescape_listing(self.post(f"/shops/{self.shop_id()}/listings", form=fields))

    def listing_inventory(self, listing_id: int) -> dict[str, Any]:
        return self.get(f"/listings/{listing_id}/inventory")

    def update_listing_inventory(
        self, listing_id: int, inventory: dict[str, Any]
    ) -> dict[str, Any]:
        # Unlike createDraftListing, this endpoint takes JSON, and it replaces the whole
        # inventory — every product, property and offering — in one call.
        return self.put(f"/listings/{listing_id}/inventory", json_body=inventory)

    def update_listing(self, listing_id: int, fields: dict[str, Any]) -> dict[str, Any]:
        return unescape_listing(
            self.patch(f"/shops/{self.shop_id()}/listings/{listing_id}", form=fields)
        )

    def upload_listing_image(
        self, listing_id: int, image: Path, *, rank: int = 1, alt_text: str = "",
        is_watermarked: bool = False,
    ) -> dict[str, Any]:
        """uploadListingImage: `is_watermarked` tells Etsy the picture carries a
        watermark (OAS: boolean form field, default false; sent only when true)."""
        # Refuse before reading. A file Etsy will not take should not be pulled into
        # memory first, and the refusal has to land before the request, not after —
        # by upload time the draft already exists and the row can only be "partial".
        mime = _mime_for(image)
        with image.open("rb") as handle:
            files = {"image": (image.name, handle.read(), mime)}
        # `is_watermarked` is sent only for a photo etsyotomasyon stamped (drop.watermark);
        # a shop's info images never carry it.
        data = {"rank": str(rank)}
        if alt_text:
            data["alt_text"] = alt_text[:MAX_ALT_TEXT]
        if is_watermarked:
            data["is_watermarked"] = "true"
        return self.request(
            "POST",
            f"/shops/{self.shop_id()}/listings/{listing_id}/images",
            files={**files, **{k: (None, v) for k, v in data.items()}},
        )

    def upload_listing_file(
        self, listing_id: int, path: Path, *, rank: int = 1, name: str | None = None
    ) -> dict[str, Any]:
        """uploadListingFile (listings_w): attach one file a buyer downloads.

        OAS: POST /shops/{shop_id}/listings/{listing_id}/files, multipart/form-data with
        `file` (binary), `name` (the file name string) and `rank` (positive, default 1);
        the other form, `listing_file_id` of an existing file, is never used here. The
        answer (201) is a ShopListingFile: listing_file_id, listing_id, rank, filename,
        filesize, size_bytes, filetype, create_timestamp.

        The OAS warns that attaching a file to a PHYSICAL listing converts it into a
        digital one and removes its shipping costs and variations, so etsyotomasyon only
        calls this on a draft it just created as `download` or `both`
        (listings.prepare refuses files on any other row). Like an image, the file is
        checked before it is read: a refusal after the draft exists can only leave the
        product "partial".
        """
        issue = file_issue(path)
        if issue is not None:
            raise ValidationError(issue[1])
        filename = (name or path.name).strip() or path.name
        with path.open("rb") as handle:
            data = handle.read()
        return self.request(
            "POST",
            f"/shops/{self.shop_id()}/listings/{listing_id}/files",
            files={
                "file": (filename, data, _file_mime(path)),
                "name": (None, filename),
                "rank": (None, str(max(1, int(rank)))),
            },
        )

    def receipts(self, *, max_items: int | None = None, **filters: Any) -> Iterator[dict[str, Any]]:
        for receipt in self.paginate(
            f"/shops/{self.shop_id()}/receipts", params=filters, max_items=max_items
        ):
            yield unescape_receipt(receipt)

    def receipts_page(self, *, limit: int, offset: int = 0, **filters: Any) -> dict[str, Any]:
        """One page of getShopReceipts: {"count": int | None, "results": [receipt, ...]}."""
        params = {k: v for k, v in filters.items() if v is not None}
        params.update(limit=limit, offset=offset)
        payload = self.get(f"/shops/{self.shop_id()}/receipts", params=params) or {}
        results = [r for r in payload.get("results") or [] if isinstance(r, dict)]
        count = payload.get("count")
        return {
            "count": count if isinstance(count, int) else None,
            "results": [unescape_receipt(r) for r in results],
        }

    def count_receipts(self, **filters: Any) -> int:
        """How many receipts match getShopReceipts' filters, from one limit=1 request.

        Filters (OAS): min_created, max_created, min_last_modified, max_last_modified
        (epoch seconds), was_paid, was_shipped, was_delivered, was_canceled (bools).
        """
        params = {k: v for k, v in filters.items() if v is not None}
        params.update(limit=1, offset=0)
        payload = self.get(f"/shops/{self.shop_id()}/receipts", params=params)
        return _count(payload)

    def receipt(self, receipt_id: int) -> dict[str, Any]:
        """getShopReceipt (transactions_r)."""
        return unescape_receipt(self.get(f"/shops/{self.shop_id()}/receipts/{receipt_id}"))

    def shop_transactions(
        self, *, max_items: int | None = None, **filters: Any
    ) -> Iterator[dict[str, Any]]:
        """getShopReceiptTransactionsByShop (transactions_r), newest first.

        The spec gives this endpoint no date or state filters, only limit/offset (and
        `legacy`); anything in `filters` is passed through as a query parameter.
        """
        for transaction in self.paginate(
            f"/shops/{self.shop_id()}/transactions",
            params={k: v for k, v in filters.items() if v is not None},
            max_items=max_items,
        ):
            yield unescape_transaction(transaction)

    def ledger_entries(
        self, min_created: int, max_created: int, *, max_items: int | None = None
    ) -> Iterator[dict[str, Any]]:
        """getShopPaymentAccountLedgerEntries: the payment account's ledger.

        Both bounds are required epoch seconds (OAS minimum 946684800). Scope
        transactions_r per the spec — already in DEFAULT_SCOPES, so no reconnect.
        Each entry: entry_id, amount (minor units), currency, description, balance,
        created_timestamp, ledger_type, reference_type, reference_id, ...
        """
        yield from self.paginate(
            f"/shops/{self.shop_id()}/payment-account/ledger-entries",
            params={"min_created": int(min_created), "max_created": int(max_created)},
            max_items=max_items,
        )

    def receipt_payments(self, receipt_id: int) -> list[dict[str, Any]]:
        """getShopPaymentByReceiptId (transactions_r): amount_gross/fees/net per payment."""
        payload = self.get(f"/shops/{self.shop_id()}/receipts/{receipt_id}/payments")
        return (payload or {}).get("results") or []

    def payments(self, payment_ids: Iterable[int]) -> list[dict[str, Any]]:
        """getPayments (transactions_r) for the given ids, chunked 100 at a time."""
        ids = list(dict.fromkeys(int(i) for i in payment_ids))
        out: list[dict[str, Any]] = []
        for start in range(0, len(ids), MAX_BATCH_IDS):
            chunk = ids[start : start + MAX_BATCH_IDS]
            payload = self.get(
                f"/shops/{self.shop_id()}/payments",
                params={"payment_ids": ",".join(str(i) for i in chunk)},
            )
            out.extend((payload or {}).get("results") or [])
        return out

    def readiness_state_definitions(self) -> list[dict[str, Any]]:
        """getShopReadinessStateDefinitions (shops_r): the shop's processing profiles.

        Each: readiness_state_id, readiness_state (ready_to_ship|made_to_order),
        min_processing_days, max_processing_days, processing_days_display_label.
        """
        return list(self.paginate(f"/shops/{self.shop_id()}/readiness-state-definitions"))

    def create_receipt_shipment(self, receipt_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        # Note: unlike listings, this endpoint takes JSON, not form encoding.
        return unescape_receipt(self.post(
            f"/shops/{self.shop_id()}/receipts/{receipt_id}/tracking",
            json_body={k: v for k, v in payload.items() if v is not None},
        ))

    def search_active_listings(
        self, *, keywords: str, max_items: int = 100, **filters: Any
    ) -> Iterator[dict[str, Any]]:
        """Public marketplace search. Needs the API key but no OAuth token."""
        params = {"keywords": keywords, **filters}
        for listing in self.paginate(
            "/listings/active", params=params, max_items=max_items, authed=False,
            max_offset=MAX_SEARCH_OFFSET,
        ):
            yield unescape_listing(listing)


def _count(payload: Any) -> int:
    """The `count` of a collection response; 0 when Etsy sent none."""
    count = (payload or {}).get("count") if isinstance(payload, dict) else None
    return int(count) if isinstance(count, int) else 0


def taxonomy_path(nodes: list[dict[str, Any]], taxonomy_id: int | str) -> str:
    """The "Parent > Child > Leaf" name of a seller-taxonomy node, or "" if unknown.

    `nodes` is what taxonomy_nodes() returns: a tree with `children` lists.
    """
    try:
        wanted = int(taxonomy_id)
    except (TypeError, ValueError):
        return ""
    for node_id, path in walk_taxonomy(nodes):
        if node_id == wanted:
            return path
    return ""


def walk_taxonomy(nodes: list[dict[str, Any]], trail: tuple[str, ...] = ()) -> Iterator[tuple[int, str]]:
    """Every node of the seller taxonomy as (id, "A > B > C"), depth first."""
    for node in nodes or []:
        path = (*trail, str(node.get("name", "")))
        try:
            node_id = int(node.get("id", 0))
        except (TypeError, ValueError):
            node_id = 0
        yield node_id, " > ".join(path)
        yield from walk_taxonomy(node.get("children") or [], path)


# What Etsy's listing-image endpoint accepts, and nothing else. WEBP is deliberately
# absent even though Pillow reads it: Etsy refuses it, and mapping it to image/webp
# would make etsyotomasyon look like it supported a format that fails on arrival. Nothing
# is ever sent under a guessed type such as application/octet-stream.
_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
}

# The same fact as a suffix set, for callers deciding about a file before a listing
# exists. `drop.workspace.IMAGE_SUFFIXES` is the wider *input* set and is not this.
UPLOADABLE_SUFFIXES = frozenset(_MIME)

# Etsy refuses a listing image over 20MB.
MAX_IMAGE_BYTES = 20 * 1024 * 1024

# uploadListingImage's alt_text: "Max length 500 characters" (the Open API spec).
MAX_ALT_TEXT = 500

# Where a ListingImage's pictures live (url_fullxfull, url_570xN, ...): Etsy's image
# CDN, i.etsystatic.com. download_image fetches nothing else.
IMAGE_HOSTS = ("etsystatic.com",)


def is_etsy_image_url(url: Any) -> bool:
    """An https address on Etsy's image CDN: the only kind download_image fetches."""
    if not isinstance(url, str) or not url.startswith("https://"):
        return False
    try:
        host = (httpx.URL(url).host or "").lower().rstrip(".")
    except (httpx.InvalidURL, ValueError, TypeError):
        return False
    return any(host == name or host.endswith("." + name) for name in IMAGE_HOSTS)


def image_problem(path: Path) -> str | None:
    """Why Etsy would refuse this file, or None if it would take it.

    Suffix and size only, deliberately: this runs over every image of every row
    inside `listings.prepare()`, and the question it answers is "would the endpoint
    accept this", not "are the pixels intact". Decoding belongs further in, where a
    batch is being read anyway.
    """
    suffix = path.suffix.lower()
    if suffix not in UPLOADABLE_SUFFIXES:
        return (
            f"{path.name}: Etsy accepts JPG, PNG and GIF listing images, not "
            f"{suffix or 'a file with no extension'} — save it as a .jpg and re-run"
        )
    try:
        size = path.stat().st_size
    except OSError as exc:
        return f"{path.name}: cannot be read ({exc})"
    if size == 0:
        return f"{path.name} is empty"
    if size > MAX_IMAGE_BYTES:
        return (
            f"{path.name} is {size / 1024 / 1024:.1f}MB and Etsy's limit for a listing "
            f"image is {MAX_IMAGE_BYTES // 1024 // 1024}MB"
        )
    return None


def _mime_for(path: Path) -> str:
    """The Content-Type for an upload, refusing anything Etsy would not accept."""
    problem = image_problem(path)
    if problem:
        raise ValidationError(problem)
    return _MIME[path.suffix.lower()]


# --- digital files (uploadListingFile) ---------------------------------------------------
#
# The OAS gives uploadListingFile no limits at all: no size, no count, no file types
# (checked in the saved spec: only `file`, `name`, `rank` and `listing_file_id`). The two
# numbers below are Etsy's seller help for digital items — at most five files per
# listing, each at most 20 MB — and are enforced here, before the draft exists, because
# a refusal after the create can only leave the product "partial". Not verifiable
# against the spec; if Etsy's limits differ it will say so and the product is partial.
MAX_LISTING_FILES = 5
MAX_FILE_BYTES = 20 * 1024 * 1024

# Types are not restricted by the spec either, so any file a buyer can open goes: PDF,
# ZIP, PNG, JPG, SVG, EPS, DXF, fonts, presets... What is refused is what should never be
# sold as a download because it runs when double-clicked (and marketplaces refuse it):
# programs, installers and scripts. A file with no extension is refused too — neither
# Etsy nor the buyer's computer could tell what it is.
BLOCKED_FILE_SUFFIXES = frozenset({
    ".exe", ".msi", ".bat", ".cmd", ".com", ".scr", ".pif", ".cpl", ".msc", ".hta",
    ".ps1", ".vbs", ".vbe", ".jse", ".wsf", ".wsh", ".reg", ".lnk", ".url", ".dll",
    ".sys", ".jar", ".apk", ".app", ".dmg", ".pkg", ".sh", ".command",
})

# Sent as the file part's Content-Type; anything else goes as application/octet-stream
# (Etsy works the type out itself and reports it back as `filetype`).
_FILE_MIME = {
    ".pdf": "application/pdf",
    ".zip": "application/zip",
    ".rar": "application/vnd.rar",
    ".7z": "application/x-7z-compressed",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".svg": "image/svg+xml",
    ".eps": "application/postscript",
    ".ai": "application/postscript",
    ".psd": "image/vnd.adobe.photoshop",
    ".dxf": "image/vnd.dxf",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".epub": "application/epub+zip",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
}


def file_issue(path: Path) -> tuple[str, str, dict[str, Any]] | None:
    """Why this file cannot be a listing's download: (code, message, params), or None.

    Codes: file_type (a program or no extension), file_missing, file_unreadable,
    file_empty, file_too_large. Like image_problem(): the file name and size only.
    """
    name = path.name
    suffix = path.suffix.lower()
    if not suffix or suffix in BLOCKED_FILE_SUFFIXES:
        return (
            "file_type",
            f"{name}: {'a file with no extension' if not suffix else suffix + ' files'} "
            "cannot be sold as a download — use PDF, ZIP, PNG, JPG, SVG or another "
            "document or image type",
            {"name": name, "ext": suffix},
        )
    try:
        if not path.is_file():
            return ("file_missing", f"{name} was not found", {"name": name})
        size = path.stat().st_size
    except OSError as exc:
        return ("file_unreadable", f"{name}: cannot be read ({exc})", {"name": name})
    if size == 0:
        return ("file_empty", f"{name} is empty", {"name": name})
    if size > MAX_FILE_BYTES:
        mb = round(size / 1024 / 1024, 1)
        return (
            "file_too_large",
            f"{name} is {mb}MB and Etsy's limit for a digital file is "
            f"{MAX_FILE_BYTES // 1024 // 1024}MB — make it smaller or split it",
            {"name": name, "mb": mb, "max_mb": MAX_FILE_BYTES // 1024 // 1024},
        )
    return None


def file_problem(path: Path) -> str | None:
    """file_issue()'s message only: why Etsy would refuse this download, or None."""
    issue = file_issue(path)
    return issue[1] if issue is not None else None


def _file_mime(path: Path) -> str:
    return _FILE_MIME.get(path.suffix.lower(), "application/octet-stream")
