"""Siparişler: the shop's orders (Etsy "receipts") and tracking-number upload.

    GET  /api/orders?tab=unshipped|shipped|delivered|all&q=&page=&per_page=&fresh=
    GET  /api/orders/summary?fresh=          tab counts, shipped this month, restriction flag
    GET  /api/orders/carriers?country=       Etsy's carriers for a ship-from country
    POST /api/orders/country {country}       remember the ship-from country, answer its carriers
    POST /api/orders/ship {rows:[{receipt_id, carrier_name, tracking_code, note_to_buyer?}],
                          country?, confirm: true}
    GET  /api/orders/export.csv?tab=         (POST {tab, edits} adds the numbers typed so far)
    POST /api/orders/import-tracking         raw CSV body -> parsed rows (no Etsy call)
    GET  /api/orders/drafts                  carriers and numbers not sent yet (this computer)
    POST /api/orders/drafts {rows:[{receipt_id, carrier_name, tracking_code, note_to_buyer?}]}
    GET  /api/orders/{id}/print?dpi=&repeats=  what each item's print-ready file would be
    POST /api/orders/{id}/print {dpi?, repeats?, bleed?} make them (print-files/orders/<id>/; local)
    POST /api/orders/{id}/print/open         show that folder
    GET/POST /api/partner                    the seller's production partner (address, token, costs)
    POST /api/orders/{id}/partner {confirm: true, force?}  send the order to that partner
    POST /api/orders/print-waiting {dpi?}    job "print": make them for every waiting order
                                             that has none yet (the page asks when it opens)

Etsy facts (checked against the OpenAPI spec, 3.0.0): getShopReceipts filters
was_paid / was_shipped / was_delivered / was_canceled (booleans), sort_on
created|updated|receipt_id; createReceiptShipment takes JSON {tracking_code,
carrier_name, send_bcc, note_to_buyer, ...}, needs transactions_w, e-mails the buyer
every time it succeeds, and accepts the carrier name "other"; getShippingCarriers
needs origin_country_iso and only the API key.

Uploading tracking is irreversible (Etsy e-mails every buyer), so the page asks
first and the request must say {"confirm": true}; one job at a time, and the job
refuses any receipt that is no longer waiting for shipment. Etsy has withdrawn
tracking uploads from newer API keys in many countries: the first 403 on /tracking
stops the job and is remembered.
"""

from __future__ import annotations

import csv
import functools
import io
import json
import logging
import re
import threading
import time
import unicodedata
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ... import orders as orders_lib
from ...config import base_home, home_dir
from ...csvio import as_int
from ...errors import AuthError, AuthUnreachable, EtsyApiError, EtsyotomasyonError
from ..errors import to_api_error
from ..router import ApiError, Request, Response
from .profit import product_type

if TYPE_CHECKING:  # pragma: no cover
    from ..context import AppContext
    from ..router import Router

# Tab -> getShopReceipts filters. Only paid orders: an unpaid one cannot be shipped.
TAB_FILTERS: dict[str, dict[str, bool]] = {
    "unshipped": {"was_paid": True, "was_shipped": False, "was_canceled": False},
    "shipped": {"was_paid": True, "was_shipped": True, "was_delivered": False,
                "was_canceled": False},
    "delivered": {"was_paid": True, "was_delivered": True},
    "all": {"was_paid": True},
}
TABS = tuple(TAB_FILTERS)
SORT = {"sort_on": "created", "sort_order": "desc"}

SCAN_LIMIT = 500          # receipts read for a search, or for the waiting list
SHIPPED_SCAN_LIMIT = 1000  # shipped receipts changed this month, read to count this month's
EXPORT_LIMIT = 1000       # receipts in one CSV export
MAX_PER_PAGE = 100
SHIP_MAX_ROWS = 500
log = logging.getLogger("etsyotomasyon.web")
QUICK_ATTEMPTS = 2        # a page is waiting: two tries per Etsy request, not five
IMPORT_MAX_BYTES = 2 * 1024 * 1024
LIST_TTL = 60.0           # receipts and counts: re-read after a minute
LISTING_TTL = 3600.0      # listing thumbnails
CARRIER_TTL = 86400.0     # Etsy's carrier list per country: a day
COUNTRY_TTL = 86400.0
FALLBACK_COUNTRY = "US"
OTHER_CARRIER = "other"   # "If the carrier is not supported, you may use `other`" (OAS)
CARRIER_CACHE = "shipping-carriers.json"
DRAFTS_FILE = "orders-drafts.json"  # in the open shop's home
TRACKING_MAX = 64         # the page's input takes 64 characters
CARRIER_MAX = 100
NOTE_MAX = 1000
SOLD_ORDERS_URL = "https://www.etsy.com/your/orders/sold"
_COUNT_KEY = {"ok": "sent", "error": "failed", "skipped": "skipped"}

# Errors after which sending the next row cannot succeed either.
STOP_CODES = ("tracking_restricted", "missing_scope", "offline", "reconnect", "bad_keys",
              "rate_limited", "setup_needed")

EXPORT_COLUMNS = [
    "receipt_id",
    "order_date",
    "buyer_name",
    "ship_city",
    "ship_state",
    "ship_country",
    "items",
    "item_count",
    "order_total",
    "currency",
    "status",
    "carrier_name",
    "tracking_code",
    "note_to_buyer",
]

_COUNTRY = re.compile(r"^[A-Z]{2}$")


def register(r: Router, ctx: AppContext) -> None:
    api = OrdersApi(ctx)
    ctx.on_change("orders", lambda c: api._invalidate(), name="orders")
    r.get("/api/orders", api.list_orders)
    r.get("/api/orders/summary", api.summary)
    r.get("/api/orders/carriers", api.carriers)
    r.post("/api/orders/country", api.set_country)
    r.post("/api/orders/ship", api.ship)
    r.get("/api/orders/export.csv", api.export_csv)
    r.post("/api/orders/export.csv", api.export_csv)
    r.post("/api/orders/import-tracking", api.import_tracking)
    r.get("/api/orders/drafts", api.get_drafts)
    r.post("/api/orders/drafts", api.save_drafts)
    r.get("/api/orders/{id:int}/print", api.print_plan)
    r.post("/api/orders/{id:int}/print", api.make_print)
    r.post("/api/orders/{id:int}/print/open", api.open_print)
    r.post("/api/orders/print-waiting", api.print_waiting)
    r.get("/api/partner", api.get_partner)
    r.post("/api/partner", api.set_partner)
    r.post("/api/orders/{id:int}/partner", api.send_to_partner)


# --- pure helpers (tested directly) -----------------------------------------------------


def receipt_status(receipt: dict[str, Any], tab: str | None = None) -> str:
    """unshipped | shipped | delivered | canceled, for the DURUM column.

    A receipt carries no "delivered" flag of its own: only the was_delivered filter
    knows, so a row is "delivered" when it was read for the delivered tab.
    """
    status = str(receipt.get("status") or "").lower()
    if status in ("canceled", "fully refunded"):
        return "canceled"
    if receipt.get("is_shipped"):
        return "delivered" if tab == "delivered" else "shipped"
    return "unshipped"


def short_name(name: str | None) -> str:
    """ "Emily Rodriguez" -> "Emily R." (first name, last initial), as in the video."""
    words = str(name or "").split()
    if not words:
        return ""
    if len(words) == 1:
        return words[0]
    return f"{words[0]} {words[-1][0].upper()}."


def _money(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not isinstance(value.get("amount"), (int, float)):
        return None
    return {
        "amount": value["amount"],
        "divisor": value.get("divisor") or 100,
        "currency_code": str(value.get("currency_code") or ""),
    }


def flatten(receipt: dict[str, Any], tab: str | None = None) -> dict[str, Any]:
    """One receipt as a table row. Never includes the buyer's e-mail or street address."""
    items = []
    count = 0
    for txn in receipt.get("transactions") or []:
        if not isinstance(txn, dict):
            continue
        quantity = txn.get("quantity") if isinstance(txn.get("quantity"), int) else 1
        count += quantity
        variations = [
            {"name": str(v.get("formatted_name") or ""), "value": str(v.get("formatted_value") or "")}
            for v in txn.get("variations") or []
            if isinstance(v, dict) and (v.get("formatted_value") or v.get("formatted_name"))
        ]
        title = str(txn.get("title") or "")
        items.append({
            "transaction_id": txn.get("transaction_id"),
            "title": title,
            # The kind of product ("Kupa · 11oz" under the title), read from the title
            # the way Kâr-Zarar and the mockups read it.
            "type": product_type(title),
            "quantity": quantity,
            "variations": variations,
            "listing_id": txn.get("listing_id"),
            "listing_image_id": txn.get("listing_image_id"),
            "thumb": None,
            # Whether the listing's print file is on this computer (drop.printfiles),
            # filled in by the page's answer; a digital item needs none.
            "print_file": None,
            "_digital": bool(txn.get("is_digital")),
        })
    shipments = [
        {
            "carrier_name": str(s.get("carrier_name") or ""),
            "tracking_code": str(s.get("tracking_code") or ""),
            "at": s.get("shipment_notification_timestamp"),
        }
        for s in receipt.get("shipments") or []
        if isinstance(s, dict)
    ]
    created = receipt.get("created_timestamp") or receipt.get("create_timestamp")
    return {
        "receipt_id": receipt.get("receipt_id"),
        "created": created if isinstance(created, (int, float)) else None,
        "buyer": short_name(receipt.get("name")),
        "city": str(receipt.get("city") or ""),
        "state": str(receipt.get("state") or ""),
        "country": str(receipt.get("country_iso") or ""),
        "items": items,
        "item_count": count,
        "total": _money(receipt.get("grandtotal") or receipt.get("total_price")),
        "shipments": shipments,
        "status": receipt_status(receipt, tab),
        "is_gift": bool(receipt.get("is_gift")),
        # When it must ship by (Etsy counts a late shipment against the shop's rating).
        "ship_by": receipt.get("expected_ship_date")
        if isinstance(receipt.get("expected_ship_date"), (int, float)) else None,
    }


def matches(receipt: dict[str, Any], query: str, *, names: bool = True) -> bool:
    """Search: order number (with or without #), buyer name, product title."""
    q = _fold(query)
    if not q:
        return True
    digits = q.lstrip("#").strip()
    if digits.isdigit() and digits in str(receipt.get("receipt_id") or ""):
        return True
    if names and q in _fold(receipt.get("name")):
        return True
    return any(
        isinstance(txn, dict) and q in _fold(txn.get("title"))
        for txn in receipt.get("transactions") or []
    )


def _fold(text: Any) -> str:
    """Case- and accent-insensitive text (Turkish dotted/dotless i included)."""
    value = str(text or "").replace("İ", "i").replace("I", "ı").casefold().replace("ı", "i")
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).strip()


def thumb_map(listings: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """listing_id -> {"images": {image_id: url}, "first": url} from getListingsByListingIds."""
    out: dict[int, dict[str, Any]] = {}
    for listing in listings:
        if not isinstance(listing, dict) or not isinstance(listing.get("listing_id"), int):
            continue
        images = sorted(
            (i for i in listing.get("images") or [] if isinstance(i, dict)),
            key=lambda i: i.get("rank") or 0,
        )
        urls: dict[int, str] = {}
        for image in images:
            url = image.get("url_75x75") or image.get("url_170x135") or image.get("url_570xN")
            if isinstance(image.get("listing_image_id"), int) and url:
                urls[image["listing_image_id"]] = str(url)
        first = next(iter(urls.values()), None)
        out[listing["listing_id"]] = {"images": urls, "first": first}
    return out


def _header_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _fold(name))


_ALIASES = {
    "receipt_id": ("receipt_id", "receiptid", "receipt", "order_id", "orderid", "order",
                   "order_no", "order_number", "ordernumber", "siparis_no", "siparis",
                   "siparis_numarasi", "sipariş no", "sipariş"),
    "tracking_code": ("tracking_code", "tracking", "tracking_number", "trackingnumber",
                      "tracking_no", "takip_no", "takip", "takip_kodu", "takip numarası",
                      "takip no"),
    "carrier_name": ("carrier_name", "carrier", "kargo", "kargo_firmasi", "kargo firması",
                     "shipping_carrier"),
    "note_to_buyer": ("note_to_buyer", "note", "not", "aliciya_not", "alıcıya not"),
}
_HEADER = {_header_key(alias): field for field, names in _ALIASES.items() for alias in names}


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1254"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def parse_tracking_csv(data: bytes) -> dict[str, Any]:
    """Read receipt_id / tracking_code / carrier_name (/ note_to_buyer) from a CSV.

    Accepts comma, semicolon (Excel in Turkish) and tab separated files, UTF-8 or
    Windows-1254, and Turkish column names. Rows with no tracking number are
    skipped (an export fed back as it is); a later row for the same order wins.
    """
    text = _decode(data)
    if not text.strip():
        raise ApiError(422, "invalid_csv", "The file is empty.")
    sample = text[:4096]
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
    except csv.Error:
        delimiter = ","
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    try:
        header = next(reader)
    except StopIteration as exc:
        raise ApiError(422, "invalid_csv", "The file is empty.") from exc
    columns: dict[str, int] = {}
    for index, name in enumerate(header):
        field = _HEADER.get(_header_key(name))
        if field and field not in columns:
            columns[field] = index
    missing = [f for f in ("receipt_id", "tracking_code") if f not in columns]
    if missing:
        raise ApiError(
            422, "invalid_csv",
            "The CSV needs receipt_id and tracking_code columns.",
            missing=missing,
        )
    rows: dict[int, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    skipped = 0
    for cells in reader:
        line = reader.line_num
        if not any(c.strip() for c in cells):
            continue

        def cell(field: str, cells: list[str] = cells) -> str:
            index = columns.get(field)
            return cells[index].strip() if index is not None and index < len(cells) else ""

        raw_id = cell("receipt_id").lstrip("#").replace(" ", "")
        tracking = cell("tracking_code")
        if not tracking:
            skipped += 1
            continue
        try:
            receipt_id = as_int(raw_id, "receipt_id", required=True)
        except EtsyotomasyonError as exc:
            errors.append({"line": line, "message": str(exc)})
            continue
        if receipt_id is None or receipt_id <= 0:
            errors.append({"line": line, "message": "receipt_id must be a positive number"})
            continue
        rows.pop(receipt_id, None)
        rows[receipt_id] = {
            "receipt_id": receipt_id,
            "tracking_code": tracking,
            "carrier_name": cell("carrier_name"),
            "note_to_buyer": cell("note_to_buyer"),
            "line": line,
        }
    return {"rows": list(rows.values()), "errors": errors, "skipped": skipped}


def carriers_from(payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Etsy's carrier objects -> [{id, name}], sorted by name, duplicates dropped."""
    seen: set[str] = set()
    out = []
    for carrier in payload:
        name = str((carrier or {}).get("name") or "").strip()
        if not name or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        out.append({"id": carrier.get("shipping_carrier_id"), "name": name})
    return sorted(out, key=lambda c: c["name"].casefold())


def sheet_safe(value: Any) -> str:
    """Text a spreadsheet will not run as a formula (a buyer chooses their own name)."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", chr(9), chr(13)) else text


def month_start(now: float | None = None) -> int:
    moment = datetime.fromtimestamp(now if now is not None else time.time())
    return int(moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())


def shipped_since(receipt: dict[str, Any], since: int) -> bool:
    """True when the order went out at or after `since` (epoch seconds).

    When it went out is its first shipment's shipment_notification_timestamp (OAS: "the
    time at which Etsy notified the buyer of the shipment event"); a later shipment is
    a corrected tracking number, not a second sending. An order marked shipped with no
    shipment record has no such time: it counts only when it was also created since
    then (it cannot have gone out before it existed).
    """
    stamps = [
        s["shipment_notification_timestamp"]
        for s in receipt.get("shipments") or []
        if isinstance(s, dict) and isinstance(s.get("shipment_notification_timestamp"), (int, float))
    ]
    if stamps:
        return min(stamps) >= since
    created = receipt.get("created_timestamp") or receipt.get("create_timestamp")
    return isinstance(created, (int, float)) and created >= since


def still_waiting(receipt: Any) -> bool:
    """A receipt (getShopReceipt) that is paid, not shipped and not cancelled."""
    if not isinstance(receipt, dict) or not receipt.get("receipt_id"):
        return False
    status = str(receipt.get("status") or "").lower()
    return (
        receipt.get("is_paid") is not False
        and not receipt.get("is_shipped")
        and status not in ("canceled", "fully refunded")
    )


def draft_rows(rows: Any) -> list[dict[str, Any]]:
    """The page's unsent carriers and numbers, checked like an imported CSV: a positive
    order number each, text of a sane length; a row with neither a carrier nor a number
    is dropped, and a later row for the same order wins."""
    if not isinstance(rows, list):
        raise ApiError(422, "invalid", "rows must be a list", field="rows")
    if len(rows) > SHIP_MAX_ROWS:
        raise ApiError(422, "invalid", f"at most {SHIP_MAX_ROWS} rows", field="rows")
    out: dict[int, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ApiError(422, "invalid", "not an object", field="rows", index=index)
        raw_id = str(row.get("receipt_id") if row.get("receipt_id") is not None else "")
        try:
            receipt_id = as_int(raw_id.lstrip("#"), "receipt_id", required=True)
        except EtsyotomasyonError as exc:
            raise ApiError(422, "invalid", str(exc), field="rows", index=index) from exc
        if receipt_id is None or receipt_id <= 0:
            raise ApiError(422, "invalid", "receipt_id must be a positive number",
                           field="rows", index=index)
        texts = {}
        for name, limit in (("tracking_code", TRACKING_MAX), ("carrier_name", CARRIER_MAX),
                            ("note_to_buyer", NOTE_MAX)):
            value = row.get(name)
            if value is None:
                value = ""
            if not isinstance(value, str):
                raise ApiError(422, "invalid", f"{name} must be text", field="rows", index=index)
            value = value.strip()
            if len(value) > limit:
                raise ApiError(422, "invalid", f"{name} is longer than {limit} characters",
                               field="rows", index=index)
            texts[name] = value
        if not texts["tracking_code"] and not texts["carrier_name"]:
            continue
        out.pop(receipt_id, None)
        out[receipt_id] = {"receipt_id": receipt_id, **texts}
    return list(out.values())


def _ship_queue(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What a send holds, as its job state keeps it for a page that comes back."""
    return [{"receipt_id": int(row["receipt_id"]), "carrier_name": row["carrier_name"],
             "tracking_code": row["tracking_code"]} for row in rows]


# --- the endpoints --------------------------------------------------------------------------


def _quick(handler: Any) -> Any:
    """Someone is looking at the page: give up on Etsy after two tries (about a second)
    instead of the client's five with growing pauses. Only this request's thread."""

    @functools.wraps(handler)
    def run(self: OrdersApi, req: Request) -> Any:
        with self.ctx.client().attempts(QUICK_ATTEMPTS):
            return handler(self, req)

    return run


def _print_items(root: Any, receipt: dict[str, Any], *, dpi: int,
                 repeats: int | None, bleed: float = 0.0) -> list[dict[str, Any]]:
    """Each physical item of an order and the print-ready file it becomes (drop.production):
    its size from the variation, its listing's print file, how the design fills it and
    how well the design repeats. `problem` says why an item cannot be made."""
    from PIL import Image

    from ...drop import production

    out: list[dict[str, Any]] = []
    folder = production.order_folder(root, receipt.get("receipt_id") or 0)
    txns = [t for t in receipt.get("transactions") or [] if isinstance(t, dict)]
    for index, txn in enumerate(txns, start=1):
        if txn.get("is_digital"):
            continue
        values = [str(v.get("formatted_value") or "") for v in txn.get("variations") or []
                  if isinstance(v, dict)]
        size = production.parse_size(values + [str(txn.get("title") or "")])
        material = production.material_of(values, size)
        quantity = txn.get("quantity") if isinstance(txn.get("quantity"), int) else 1
        listing_id = txn.get("listing_id") if isinstance(txn.get("listing_id"), int) else None
        item: dict[str, Any] = {
            "index": index, "transaction_id": txn.get("transaction_id"),
            "title": str(txn.get("title") or ""), "listing_id": listing_id,
            "quantity": quantity, "variations": values, "material": material,
            "size": size.to_dict() if size else None, "source": None, "seam": None,
            "plan": None, "file": None, "made": False, "problem": None,
        }
        out.append(item)
        design = production.design_file(root, listing_id) if listing_id else None
        if size is None:
            item["problem"] = "no_size"
        if design is None:
            item["problem"] = item["problem"] or ("no_print_file" if listing_id else "no_listing")
            continue
        try:
            image = production.open_design(design)
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
            item["problem"] = item["problem"] or "unreadable"
            continue
        item["source"] = {"name": design.name, "rel": design.relative_to(root).as_posix(),
                          "width": image.width, "height": image.height,
                          "mtime": int(design.stat().st_mtime)}
        if size is None:
            continue
        made = production.plan(size, image.size, dpi=dpi, repeats=repeats, bleed_in=bleed)
        if made.mode == "pattern":
            # A pattern whose edges do not meet shows a line where it repeats: its edges
            # are blended (the design file itself is never changed) and re-measured.
            across, down = production.seam_ratios(image)
            seam: dict[str, Any] = {"across": across, "down": down, "fixed": False,
                                    "ok": max(across, down) < production.SEAM_WARN}
            if not seam["ok"]:
                repaired = production.make_seamless(image)
                a2, d2 = production.seam_ratios(repaired)
                if max(a2, d2) < max(across, down):
                    image = repaired
                    seam.update(fixed=True, before={"across": across, "down": down},
                                across=a2, down=d2, ok=max(a2, d2) < production.SEAM_WARN)
            item["seam"] = seam
        item.update(_image=image, _plan=made, _size=size, plan=made.to_dict())
        item["file"] = production.file_name(index, made, material, quantity)
        item["made"] = (folder / item["file"]).is_file()
    return out


def _public(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in item.items() if not k.startswith("_")} for item in items]


class _Cache:
    """A small time-limited dict, safe between request threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[Any, tuple[float, Any]] = {}

    def get(self, key: Any, ttl: float) -> Any:
        with self._lock:
            found = self._items.get(key)
        if found is None or time.monotonic() - found[0] > ttl:
            return None
        return found[1]

    def put(self, key: Any, value: Any) -> Any:
        with self._lock:
            self._items[key] = (time.monotonic(), value)
        return value

    def drop(self, match: Any = None) -> None:
        with self._lock:
            if match is None:
                self._items.clear()
            else:
                for key in [k for k in self._items if match(k)]:
                    del self._items[key]


class OrdersApi:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.receipts = _Cache()   # (shop, tab) -> list of raw receipts (a scan)
        self.counts = _Cache()     # shop -> summary
        self.listings = _Cache()   # (shop, listing_id) -> thumbnails of one listing
        self.countries = _Cache()  # shop -> (country, source)
        self._drafts_lock = threading.Lock()

    # --- helpers -----------------------------------------------------------------------

    def _shop(self) -> str:
        return self.ctx.shop_id

    def _tab(self, req: Request) -> str:
        tab = (req.query.get("tab") or "unshipped").strip().lower()
        if tab not in TABS:
            raise ApiError(422, "invalid", "tab must be unshipped, shipped, delivered or all",
                           field="tab")
        return tab

    def _scan(self, client: Any, tab: str, *, fresh: bool = False,
              limit: int | None = None) -> tuple[list[dict[str, Any]], bool]:
        """Up to `limit` (default SCAN_LIMIT) receipts of a tab, newest first (cached a
        minute), and whether the tab holds more than that."""
        limit = limit or SCAN_LIMIT
        key = (self._shop(), tab, limit)
        if not fresh:
            cached = self.receipts.get(key, LIST_TTL)
            if cached is not None:
                return cached
        found = list(client.receipts(max_items=limit + 1, **TAB_FILTERS[tab], **SORT))
        truncated = len(found) > limit
        return self.receipts.put(key, (found[:limit], truncated))

    def _invalidate(self) -> None:
        shop = self._shop()
        self.receipts.drop(lambda key: key[0] == shop)
        self.counts.drop(lambda key: key == shop)

    def _add_thumbs(self, client: Any, rows: list[dict[str, Any]]) -> None:
        """Fill items[].thumb from the listings' images (one batch call, cached)."""
        shop = self._shop()
        wanted = {
            item["listing_id"]
            for row in rows
            for item in row["items"]
            if isinstance(item.get("listing_id"), int)
        }
        missing = [i for i in wanted if self.listings.get((shop, i), LISTING_TTL) is None]
        if missing:
            try:
                with client.attempts(2):
                    found = thumb_map(client.listings_batch(missing, includes=["Images"]))
            except (EtsyApiError, AuthError):
                found = {}
            for listing_id in missing:
                self.listings.put((shop, listing_id), found.get(listing_id) or {"images": {}, "first": None})
        for row in rows:
            for item in row["items"]:
                info = self.listings.get((shop, item.get("listing_id")), LISTING_TTL) or {}
                images = info.get("images") or {}
                item["thumb"] = images.get(item.get("listing_image_id")) or info.get("first")

    def _add_print_files(self, rows: list[dict[str, Any]]) -> None:
        """items[].print_file: True when the listing's print file is kept here, False when
        it is not (the order cannot be printed yet), None for a digital item."""
        from ...drop import printfiles

        try:
            have = printfiles.listing_ids(self.ctx.workspace_root())
        except OSError:
            have = set()
        for row in rows:
            for item in row["items"]:
                digital = item.pop("_digital", False)
                if not digital and isinstance(item.get("listing_id"), int):
                    item["print_file"] = item["listing_id"] in have

    def _anonymise(self, rows: list[dict[str, Any]], offset: int) -> None:
        if not self.ctx.anonymise_names:
            return
        word = "Alıcı" if self.ctx.language == "tr" else "Buyer"
        for index, row in enumerate(rows):
            row["buyer"] = f"{word} {offset + index + 1}"

    # --- GET /api/orders -------------------------------------------------------------------

    @_quick
    def list_orders(self, req: Request) -> dict[str, Any]:
        tab = self._tab(req)
        query = (req.query.get("q") or "").strip()[:100]
        page = req.int_query("page", 1, min=1, max=10_000) or 1
        per_page = req.int_query("per_page", 8, min=1, max=MAX_PER_PAGE) or 8
        fresh = req.bool_query("fresh")
        client = self.ctx.client()
        offset = (page - 1) * per_page
        truncated = False
        scanned: int | None = None
        ids: list[int] | None = None
        if query or tab == "unshipped":
            # Etsy cannot search receipts: read the tab (at most SCAN_LIMIT) and filter here.
            # The waiting list is read whole anyway: it is short, and the send guard needs it.
            receipts, truncated = self._scan(client, tab, fresh=fresh)
            scanned = len(receipts)
            names = not self.ctx.anonymise_names
            hits = [r for r in receipts if matches(r, query, names=names)]
            digits = query.lstrip("#").strip()
            if query and not hits and digits.isdigit() and len(digits) >= 6:
                hits = self._one_receipt(client, int(digits))
            total = len(hits)
            page_items = hits[offset: offset + per_page]
            # Every order of the list, in order: the page finds where an imported number is.
            ids = [r.get("receipt_id") for r in hits]
        else:
            payload = client.receipts_page(
                limit=per_page, offset=offset, **TAB_FILTERS[tab], **SORT
            )
            page_items = payload["results"]
            total = payload["count"] if payload["count"] is not None else len(page_items)
        rows = [flatten(r, tab) for r in page_items]
        self._add_thumbs(client, rows)
        self._add_print_files(rows)
        self._anonymise(rows, offset)
        pages = max(1, -(-total // per_page))
        return {
            "tab": tab,
            "q": query,
            "page": page,
            "per_page": per_page,
            "pages": pages,
            "total": total,
            "rows": rows,
            "scanned": scanned,
            "truncated": truncated,
            "ids": ids,
        }

    def _one_receipt(self, client: Any, receipt_id: int) -> list[dict[str, Any]]:
        """An order number typed in full that the tab does not hold: look it up directly."""
        try:
            with client.attempts(2):
                found = client.receipt(receipt_id)
        except EtsyApiError as exc:
            if exc.status in (400, 404):
                return []
            raise
        return [found] if isinstance(found, dict) and found.get("receipt_id") else []

    # --- print-ready files (drop.production) ------------------------------------------------

    def _print_args(self, source: Any) -> tuple[int, int | None, float]:
        from ...drop import production

        try:
            dpi = int(source.get("dpi") or production.DEFAULT_DPI)
            raw = source.get("repeats")
            repeats = int(raw) if raw not in (None, "", 0, "0") else None
        except (TypeError, ValueError) as exc:
            raise ApiError(422, "invalid", "dpi and repeats must be whole numbers",
                           field="dpi") from exc
        if not production.MIN_DPI <= dpi <= production.MAX_DPI:
            raise ApiError(422, "invalid",
                           f"dpi must be {production.MIN_DPI}-{production.MAX_DPI}", field="dpi")
        if repeats is not None and not 1 <= repeats <= 40:
            raise ApiError(422, "invalid", "repeats must be 1-40", field="repeats")
        try:
            bleed = float(source.get("bleed") or 0)
        except (TypeError, ValueError) as exc:
            raise ApiError(422, "invalid", "bleed must be a number of inches",
                           field="bleed") from exc
        if not 0 <= bleed <= production.MAX_BLEED_IN:
            raise ApiError(422, "invalid",
                           f"bleed must be 0-{production.MAX_BLEED_IN:g} inches", field="bleed")
        return dpi, repeats, bleed

    def _receipt_for_print(self, receipt_id: int) -> dict[str, Any]:
        client = self.ctx.client()
        with client.attempts(QUICK_ATTEMPTS):
            found = self._one_receipt(client, receipt_id)
        if not found:
            raise ApiError(404, "not_found", f"Your shop has no order {receipt_id}.")
        return found[0]

    def _print_answer(self, receipt_id: int, items: list[dict[str, Any]], dpi: int,
                      receipt: dict[str, Any] | None = None) -> dict[str, Any]:
        from ... import partner
        from ...drop import printfiles, production

        root = self.ctx.workspace_root()
        folder = production.order_folder(root, receipt_id)
        settings = partner.load()
        income = _money(receipt.get("subtotal")) if receipt else None
        cost = partner.estimate_cost(items, settings)
        return {
            "partner": {
                **partner.public(settings),
                "sent": partner.sent_record(folder),
                "cost": cost,
                "income": income["amount"] / income["divisor"] if income else None,
                "currency": income["currency_code"] if income else None,
            },
            "receipt_id": receipt_id,
            "dpi": dpi,
            "folder": folder.relative_to(root).as_posix(),
            "items": _public(items),
            "ready": sum(1 for i in items if i["plan"]),
            "files": [printfiles.describe(root, p) for p in printfiles.files_in(folder)],
        }

    def print_plan(self, req: Request) -> dict[str, Any]:
        receipt_id = req.params["id"]
        dpi, repeats, bleed = self._print_args(req.query)
        receipt = self._receipt_for_print(receipt_id)
        items = _print_items(self.ctx.workspace_root(), receipt, dpi=dpi, repeats=repeats, bleed=bleed)
        return self._print_answer(receipt_id, items, dpi, receipt)

    def make_print(self, req: Request) -> dict[str, Any]:
        """Make every item's file that can be made. Local only: nothing goes to Etsy."""
        from ...drop import production

        receipt_id = req.params["id"]
        dpi, repeats, bleed = self._print_args(req.json_object())
        receipt = self._receipt_for_print(receipt_id)
        root = self.ctx.workspace_root()
        items = _print_items(root, receipt, dpi=dpi, repeats=repeats, bleed=bleed)
        if not any(i["plan"] for i in items):
            raise ApiError(422, "nothing_to_print", "No item of this order can be made yet.")
        folder = production.order_folder(root, receipt_id)
        try:
            self._make_files(receipt, items, folder)
        except (OSError, ValueError, MemoryError) as exc:
            raise ApiError(500, "internal", f"Could not make the files: {exc}") from exc
        return self._print_answer(receipt_id, items, dpi, receipt)

    def _make_files(self, receipt: dict[str, Any], items: list[dict[str, Any]],
                    folder: Any) -> int:
        """Render and save every item that has a plan, then the invoice sheet. How many
        files were made."""
        from ...drop import production

        made_count = 0
        for item in items:
            made = item.get("_plan")
            if made is None:
                continue
            production.save(production.render(item["_image"], made), folder / item["file"],
                            made.dpi)
            item["made"] = True
            made_count += 1
        self._write_invoice(receipt, items, folder)
        return made_count

    def print_waiting(self, req: Request) -> dict[str, Any]:
        """Prepare, in the background, the print files of every order waiting to ship that
        has none yet (an order whose invoice sheet exists is done). The page asks when it
        opens, so a new order's files are ready before the seller looks. Local only."""
        from ...drop import production

        dpi, repeats, bleed = self._print_args(req.json_object())
        for job in self.ctx.jobs.active(lane="read"):
            if job.kind == "print":
                return job.summary()
        client = self.ctx.client()
        root = self.ctx.workspace_root()

        def work(job: Any) -> dict[str, Any]:
            receipts, _more = self._scan(client, "unshipped")
            prepared = files = lacking = 0
            for n, receipt in enumerate(receipts, start=1):
                rid = receipt.get("receipt_id")
                if not isinstance(rid, int):
                    continue
                folder = production.order_folder(root, rid)
                if (folder / f"order-{rid}-invoice.pdf").is_file():
                    continue
                items = _print_items(root, receipt, dpi=dpi, repeats=repeats, bleed=bleed)
                if not any(i["plan"] for i in items):
                    lacking += 1
                    continue
                try:
                    files += self._make_files(receipt, items, folder)
                    prepared += 1
                except (OSError, ValueError, MemoryError):
                    log.warning("could not prepare order %s", rid, exc_info=True)
                job.progress(n, len(receipts), label=str(rid))
            if prepared:
                self.ctx.notify("orders", "print_ready", {"n": prepared}, tone="success",
                                link="/siparisler")
            if lacking:
                self.ctx.notify("orders", "print_missing", {"n": lacking}, tone="warning",
                                link="/siparisler", replace=True)
            return {"prepared": prepared, "files": files, "lacking": lacking}

        job = self.ctx.jobs.start("print", "orders:job.print", work, params={"n": 0},
                                  cancellable=False, lane="read")
        return job.summary()

    def _write_invoice(self, receipt: dict[str, Any], items: list[dict[str, Any]],
                       folder: Any) -> None:
        """order-<id>-invoice.pdf: the order's sheet with each item's picture at the bottom
        right, as the printer is sent it (drop/invoice.py). A failure here never costs the
        print files already made."""
        from ...drop import invoice, printfiles

        try:
            pictures = {
                item["index"]: invoice.picture_for(item["_image"], item["_plan"], item["_size"])
                for item in items if item.get("_plan") is not None
            }
            shop = (self.ctx.status.get("shop") or {}).get("name") or ""
            data = invoice.build(receipt, shop, pictures)
            name = f"order-{receipt.get('receipt_id')}-invoice.pdf"
            folder.mkdir(parents=True, exist_ok=True)
            temporary = folder / f"{printfiles.PART_PREFIX}{name}"
            temporary.write_bytes(data)
            temporary.replace(folder / name)
        except (OSError, ValueError, MemoryError):
            log.warning("could not write the invoice of order %s", receipt.get("receipt_id"),
                        exc_info=True)

    # --- the production partner (partner.py) ---------------------------------------------

    def get_partner(self, req: Request) -> dict[str, Any]:
        from ... import partner

        return partner.public()

    def set_partner(self, req: Request) -> dict[str, Any]:
        """Save the seller's partner: name, address, token, costs. {"clear": true} removes it."""
        from ... import partner
        from ...errors import ValidationError

        body = req.json_object()
        if body.get("clear"):
            partner.clear()
            return partner.public()
        try:
            partner.save({k: v for k, v in body.items()
                          if k in ("name", "url", "token", "send_files", "cost_sqm", "fixed")})
        except ValidationError as exc:
            raise ApiError(422, "invalid", str(exc), field="url") from exc
        return partner.public()

    def send_to_partner(self, req: Request) -> dict[str, Any]:
        """Send the order to the seller's partner, once, after the seller said so.

        The files are made first when they are not yet. An order already sent is not sent
        again unless {"force": true}.
        """
        from ... import partner
        from ...drop import production
        from ...errors import ValidationError

        body = req.json_object()
        if body.get("confirm") is not True:
            raise ApiError(422, "confirm_required", "Sending an order needs confirm: true.")
        settings = partner.load()
        if not partner.configured(settings):
            raise ApiError(409, "no_partner", "Connect your production partner first.")
        receipt_id = req.params["id"]
        dpi, repeats, bleed = self._print_args(body)
        receipt = self._receipt_for_print(receipt_id)
        root = self.ctx.workspace_root()
        folder = production.order_folder(root, receipt_id)
        if partner.sent_record(folder) and body.get("force") is not True:
            raise ApiError(409, "already_sent", "This order was already sent to the partner.")
        items = _print_items(root, receipt, dpi=dpi, repeats=repeats, bleed=bleed)
        if not any(i["plan"] for i in items):
            raise ApiError(422, "nothing_to_print", "No item of this order can be made yet.")
        try:
            if not all(i["made"] for i in items if i["plan"]):
                self._make_files(receipt, items, folder)
            payload = partner.build_payload(receipt, _public(items), folder, settings)
            record = partner.send(payload, settings, folder,
                                  transport=getattr(self.ctx, "partner_transport", None))
        except ValidationError as exc:
            raise ApiError(502, "partner_unreachable", str(exc)) from exc
        except (OSError, ValueError, MemoryError) as exc:
            raise ApiError(500, "internal", f"Could not send the order: {exc}") from exc
        if not record["ok"]:
            raise ApiError(502, "partner_refused",
                           f"The partner answered {record['status']}: {record['answer']}",
                           status=record["status"])
        return {"sent": record, **self._print_answer(receipt_id, items, dpi, receipt)}

    def open_print(self, req: Request) -> dict[str, Any]:
        from ...drop import production
        from .. import files

        path = production.order_folder(self.ctx.workspace_root(), req.params["id"])
        try:
            path.mkdir(parents=True, exist_ok=True)
            files.open_path(path)
        except OSError as exc:
            raise ApiError(500, "internal", f"Could not open the folder: {exc}") from exc
        return {"ok": True, "path": str(path)}

    # --- GET /api/orders/summary -------------------------------------------------------------

    @_quick
    def summary(self, req: Request) -> dict[str, Any]:
        client = self.ctx.client()
        shop = self._shop()
        cached = None if req.bool_query("fresh") else self.counts.get(shop, LIST_TTL)
        if cached is None:
            counts = {tab: client.count_receipts(**filters) for tab, filters in TAB_FILTERS.items()}
            shipped_month, partial = self._shipped_this_month(client)
            cached = self.counts.put(shop, {"counts": counts, "shipped_month": shipped_month,
                                            "shipped_month_partial": partial})
        prefs = self.ctx.shop_prefs()
        return {
            **cached,
            "tracking_restricted": bool(prefs.get("tracking_restricted")),
            # Etsy has accepted a tracking number from this shop's key before: the page's
            # advance note ("Etsy may refuse...", for a Türkiye ship-from) is not needed.
            "tracking_sent": bool(prefs.get("tracking_sent")),
            "sold_orders_url": SOLD_ORDERS_URL,
        }

    def _shipped_this_month(self, client: Any) -> tuple[int, bool]:
        """(orders that went out this month, whether more were left unread).

        getShopReceipts can only filter on when a receipt was created or last changed,
        not on when it shipped, so an order placed last month and shipped this month
        is not in a min_created count. Shipping it changed it this month, though: read
        the shipped receipts changed since the 1st and look at their shipments.
        """
        since = month_start()
        found = list(client.receipts(
            max_items=SHIPPED_SCAN_LIMIT + 1, was_paid=True, was_shipped=True,
            min_last_modified=since, sort_on="updated", sort_order="desc",
        ))
        partial = len(found) > SHIPPED_SCAN_LIMIT
        return sum(1 for r in found[:SHIPPED_SCAN_LIMIT] if shipped_since(r, since)), partial

    # --- carriers ---------------------------------------------------------------------------

    def _default_country(self, client: Any) -> tuple[str, str]:
        """(country, source): the saved choice, else where the shop ships from."""
        saved = str(self.ctx.shop_prefs().get("country") or "").strip().upper()
        if _COUNTRY.match(saved):
            return saved, "pref"
        shop = self._shop()
        cached = self.countries.get(shop, COUNTRY_TTL)
        if cached is not None:
            return cached
        found: tuple[str, str] | None = None
        try:
            with client.attempts(2):
                for profile in client.shipping_profiles():
                    code = str(profile.get("origin_country_iso") or "").upper()
                    if not profile.get("is_deleted") and _COUNTRY.match(code):
                        found = (code, "profile")
                        break
                if found is None:
                    code = str(client.shop().get("shipping_from_country_iso") or "").upper()
                    if _COUNTRY.match(code):
                        found = (code, "shop")
        except (EtsyApiError, AuthError):
            found = None
        if found is None:
            return FALLBACK_COUNTRY, "fallback"
        return self.countries.put(shop, found)

    def _carrier_list(self, client: Any, country: str) -> list[dict[str, Any]]:
        """Etsy's carriers for `country`, from a day-long cache shared by every shop."""
        path = base_home() / "cache" / CARRIER_CACHE
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stored = {}
        if not isinstance(stored, dict):
            stored = {}
        entry = stored.get(country)
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("carriers"), list)
            and time.time() - float(entry.get("at") or 0) < CARRIER_TTL
        ):
            return entry["carriers"]
        carriers = carriers_from(client.shipping_carriers(country))
        stored[country] = {"at": round(time.time()), "carriers": carriers}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass
        return carriers

    def _carriers_answer(self, client: Any, country: str, source: str) -> dict[str, Any]:
        last = self.ctx.shop_prefs().get("last_carrier")
        return {
            "country": country,
            "source": source,
            "carriers": self._carrier_list(client, country),
            "other": OTHER_CARRIER,
            "last_carrier": str(last.get(country) or "") if isinstance(last, dict) else "",
        }

    @_quick
    def carriers(self, req: Request) -> dict[str, Any]:
        client = self.ctx.client()
        raw = (req.query.get("country") or "").strip().upper()
        if raw:
            if not _COUNTRY.match(raw):
                raise ApiError(422, "invalid", "country must be a two-letter code", field="country")
            return self._carriers_answer(client, raw, "query")
        country, source = self._default_country(client)
        return self._carriers_answer(client, country, source)

    @_quick
    def set_country(self, req: Request) -> dict[str, Any]:
        raw = str(req.json_object().get("country") or "").strip().upper()
        if not _COUNTRY.match(raw):
            raise ApiError(422, "invalid", "country must be a two-letter code", field="country")
        client = self.ctx.client()
        answer = self._carriers_answer(client, raw, "pref")  # an unknown country fails here
        self.ctx.update_shop_prefs(country=raw)
        return answer

    # --- POST /api/orders/ship ------------------------------------------------------------------

    def _ship_rows(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        rows = body.get("rows")
        if not isinstance(rows, list) or not rows:
            raise ApiError(422, "invalid", "rows is empty", field="rows")
        if len(rows) > SHIP_MAX_ROWS:
            raise ApiError(422, "invalid", f"at most {SHIP_MAX_ROWS} rows at a time", field="rows")
        out: list[dict[str, Any]] = []
        seen: set[int] = set()
        problems: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                problems.append({"index": index, "receipt_id": None, "message": "not an object"})
                continue
            raw_id = str(row.get("receipt_id") if row.get("receipt_id") is not None else "")
            try:
                receipt_id = as_int(raw_id.lstrip("#"), "receipt_id", required=True)
            except EtsyotomasyonError as exc:
                problems.append({"index": index, "receipt_id": None, "message": str(exc)})
                continue
            if receipt_id in seen:
                problems.append({"index": index, "receipt_id": receipt_id,
                                 "message": "the same order is listed twice"})
                continue
            seen.add(receipt_id)
            note = str(row.get("note_to_buyer") or "").strip()
            out.append({
                "receipt_id": str(receipt_id),
                "tracking_code": str(row.get("tracking_code") or "").strip(),
                "carrier_name": str(row.get("carrier_name") or "").strip(),
                "note_to_buyer": note[:1000],
            })
        if problems:
            first = problems[0]
            raise ApiError(422, "invalid", first["message"], field="rows", rows=problems)
        return out

    @_quick
    def ship(self, req: Request) -> dict[str, Any]:
        ctx = self.ctx
        body = req.json_object()
        rows = self._ship_rows(body)
        client = ctx.client()
        country = str(body.get("country") or "").strip().upper()
        valid: list[str] | None = None
        if country:
            if not _COUNTRY.match(country):
                raise ApiError(422, "invalid", "country must be a two-letter code", field="country")
            valid = [c["name"] for c in self._carrier_list(client, country)] + [OTHER_CARRIER]
            by_folded = {name.casefold(): name for name in valid}
            for row in rows:  # Etsy's own spelling of the carrier
                row["carrier_name"] = by_folded.get(row["carrier_name"].casefold(), row["carrier_name"])
        # The library's own checks (tracking and carrier present, carrier in Etsy's list).
        report = orders_lib.ship(None, rows, dry_run=True, valid_carriers=valid)
        problems = [
            {"index": r.row - 2, "receipt_id": r.receipt_id, "message": r.message}
            for r in report.results
            if r.failed
        ]
        if problems:
            raise ApiError(422, "invalid", problems[0]["message"], field="rows", rows=problems)
        if any(job.active for job in ctx.jobs.list(kind="orders")):
            raise ApiError(409, "busy", "Tracking numbers are already being sent.")
        # Etsy e-mails every buyer and nothing takes it back: like every other live
        # write, the page asks first and says so; a request that skipped it sends nothing.
        if body.get("confirm") is not True:
            raise ApiError(409, "confirm_required", "Sending tracking numbers needs confirm: true.")

        def work(job: Any) -> dict[str, Any]:
            return self._run_ship(job, rows, country)

        job = ctx.jobs.start("orders", "orders:job.ship", work, params={"n": len(rows)},
                             cancellable=True)
        # Saved now, not only once the job runs: a send still queued behind another write
        # job is found again, rows and all, by a page that comes back to it.
        job.set_state(queue=_ship_queue(rows), total=len(rows), country=country)
        return job.summary()

    def _run_ship(self, job: Any, rows: list[dict[str, Any]], country: str) -> dict[str, Any]:
        ctx = self.ctx
        client = ctx.client()
        total = len(rows)
        job.progress(0, total)
        # Only orders still waiting: never a second "your order shipped" e-mail by mistake.
        # The scan holds the newest SCAN_LIMIT waiting orders; when there are more, an
        # order outside it is asked about on its own (getShopReceipt) before it is sent.
        waiting_list, truncated = self._scan(client, "unshipped", fresh=True)
        waiting = {r.get("receipt_id") for r in waiting_list}
        results: list[dict[str, Any]] = []
        counts = {"sent": 0, "failed": 0, "skipped": 0}
        stopped: str | None = None
        used: dict[str, int] = {}
        # What is being sent, for a page that comes back while the job runs.
        job.set_state(rows=[], total=total, country=country, queue=_ship_queue(rows))
        try:
            for n, row in enumerate(rows, 1):
                job.check_cancel()
                receipt_id = int(row["receipt_id"])
                result: dict[str, Any] = {
                    "receipt_id": receipt_id,
                    "carrier_name": row["carrier_name"],
                    "tracking_code": row["tracking_code"],
                }
                refusal = None
                if not stopped and receipt_id not in waiting:
                    refusal = self._not_waiting(client, receipt_id, truncated)
                if stopped:
                    result.update(status="skipped", code=stopped)
                elif refusal is not None:
                    result.update(refusal)
                    if refusal["status"] == "error" and refusal["code"] in STOP_CODES:
                        stopped = refusal["code"]
                else:
                    payload = {"tracking_code": row["tracking_code"],
                               "carrier_name": row["carrier_name"]}
                    if row.get("note_to_buyer"):
                        payload["note_to_buyer"] = row["note_to_buyer"]
                    try:
                        answer = client.create_receipt_shipment(receipt_id, payload)
                        result["status"] = "ok"
                        if isinstance(answer, dict) and answer.get("receipt_id"):
                            result["shipments"] = flatten(answer)["shipments"]
                        used[row["carrier_name"]] = used.get(row["carrier_name"], 0) + 1
                    except (EtsyApiError, AuthError) as exc:
                        code, message, http_status = _ship_error(exc)
                        result.update(status="error", code=code, message=message)
                        if http_status is not None:
                            result["http_status"] = http_status
                        if code in STOP_CODES:
                            stopped = code
                counts[_COUNT_KEY[result["status"]]] += 1
                results.append(result)
                job.emit("row", **result)
                job.progress(n, total, label=f"#{receipt_id}")
                job.set_state(rows=list(results), **counts, stopped=stopped)
        finally:
            # Also after a cancel: what was sent is sent, and the lists are stale now.
            self._after_ship(counts, stopped, used, country)
            self._forget_drafts({r["receipt_id"] for r in results if r.get("status") == "ok"})
        return {**counts, "total": total, "stopped": stopped,
                "restricted": stopped == "tracking_restricted", "rows": results}

    def _not_waiting(self, client: Any, receipt_id: int, truncated: bool) -> dict[str, Any] | None:
        """Why an order outside the waiting scan must not be sent; None when it may be.

        A complete scan is the answer: the order is no longer waiting. A cut one (a shop
        with more than SCAN_LIMIT waiting orders) is not, so the order is read on its
        own and sent only if it is still paid, unshipped and not cancelled.
        """
        skipped = {"status": "skipped", "code": "not_waiting"}
        if not truncated:
            return skipped
        try:
            receipt = client.receipt(receipt_id)
        except (EtsyApiError, AuthError) as exc:
            if isinstance(exc, EtsyApiError) and exc.status in (400, 404):
                return skipped
            code, message, http_status = _ship_error(exc)
            error: dict[str, Any] = {"status": "error", "code": code, "message": message}
            if http_status is not None:
                error["http_status"] = http_status
            return error
        return None if still_waiting(receipt) else skipped

    def _after_ship(self, counts: dict[str, int], stopped: str | None, used: dict[str, int],
                    country: str) -> None:
        ctx = self.ctx
        self._invalidate()
        if counts.get("sent"):
            ctx.changed("orders", source="orders")  # the dashboard's "to ship" number
        changes: dict[str, Any] = {}
        if stopped == "tracking_restricted":
            changes["tracking_restricted"] = True
        elif counts["sent"]:
            changes["tracking_restricted"] = None
        if counts["sent"] and not ctx.shop_prefs().get("tracking_sent"):
            changes["tracking_sent"] = True
        if used and country:
            last = ctx.shop_prefs().get("last_carrier")
            last = dict(last) if isinstance(last, dict) else {}
            last[country] = max(used, key=lambda name: used[name])
            changes["last_carrier"] = last
        if changes:
            ctx.update_shop_prefs(**changes)
        if counts["sent"]:
            # The Panel's sub-line names the carriers ("UPS · USPS"), most used first.
            ctx.notify("orders", "notify.shipped",
                       {"n": counts["sent"], "carriers": self._carrier_labels(used, country)},
                       tone="success", link="/siparisler")
        if stopped == "tracking_restricted":
            ctx.notify("orders", "notify.restricted", {}, tone="warning", link="/siparisler")
        elif counts["failed"]:
            ctx.notify("orders", "notify.failed", {"n": counts["failed"]}, tone="danger",
                       link="/siparisler")

    def _carrier_labels(self, used: dict[str, int], country: str) -> list[str]:
        """The carriers a send used, most used first, spelled as the Siparişler page shows
        them (Etsy's own spelling from the cached carrier list: "usps" -> "USPS"). The
        generic "other" is left out. Never asks Etsy: this runs after the send."""
        spelled: dict[str, str] = {}
        if country:
            try:
                stored = json.loads((base_home() / "cache" / CARRIER_CACHE).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                stored = {}
            entry = stored.get(country) if isinstance(stored, dict) else None
            carriers = entry.get("carriers") if isinstance(entry, dict) else None
            for carrier in carriers if isinstance(carriers, list) else []:
                name = str((carrier or {}).get("name") or "").strip() if isinstance(carrier, dict) else ""
                if name:
                    spelled.setdefault(name.casefold(), name)
        out: list[str] = []
        seen: set[str] = set()
        for name in sorted(used, key=lambda n: (-used[n], n.casefold())):
            folded = name.strip().casefold()
            if not folded or folded == OTHER_CARRIER or folded in seen:
                continue
            seen.add(folded)
            out.append(spelled.get(folded, name.strip()))
        return out

    # --- CSV ---------------------------------------------------------------------------------

    @_quick
    def export_csv(self, req: Request) -> Response:
        body = req.json_object() if req.method == "POST" else {}
        tab = str(body.get("tab") or req.query.get("tab") or "unshipped").strip().lower()
        if tab not in TABS:
            raise ApiError(422, "invalid", "tab must be unshipped, shipped, delivered or all",
                           field="tab")
        edits = body.get("edits") if isinstance(body.get("edits"), dict) else {}
        client = self.ctx.client()
        receipts, _truncated = self._scan(client, tab, limit=EXPORT_LIMIT)
        hide = self.ctx.anonymise_names
        word = "Alıcı" if self.ctx.language == "tr" else "Buyer"
        out = io.StringIO(newline="")
        writer = csv.DictWriter(out, fieldnames=EXPORT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for index, receipt in enumerate(receipts):
            flat = orders_lib.flatten_receipt(receipt)
            shipment = next(
                (s for s in receipt.get("shipments") or [] if isinstance(s, dict)), {}
            )
            edit = edits.get(str(receipt.get("receipt_id")))
            edit = edit if isinstance(edit, dict) else {}
            writer.writerow({
                "receipt_id": flat["receipt_id"],
                "order_date": flat["order_date"],
                "buyer_name": f"{word} {index + 1}" if hide else sheet_safe(flat["buyer_name"]),
                "ship_city": sheet_safe(flat["ship_city"]),
                "ship_state": sheet_safe(flat["ship_state"]),
                "ship_country": flat["ship_country"],
                "items": sheet_safe(" | ".join(flat["items"])),
                "item_count": flat["item_count"],
                "order_total": flat["order_total"],
                "currency": flat["currency"],
                "status": receipt_status(receipt, tab),
                "carrier_name": str(edit.get("carrier_name") or shipment.get("carrier_name") or ""),
                "tracking_code": str(edit.get("tracking_code") or shipment.get("tracking_code") or ""),
                "note_to_buyer": sheet_safe(edit.get("note_to_buyer") or ""),
            })
        stamp = datetime.now().strftime("%Y-%m-%d")
        name = f"{'siparisler' if self.ctx.language == 'tr' else 'orders'}-{tab}-{stamp}.csv"
        return Response.bytes(
            out.getvalue().encode("utf-8-sig"),
            "text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    def import_tracking(self, req: Request) -> dict[str, Any]:
        if len(req.body) > IMPORT_MAX_BYTES:
            raise ApiError(413, "too_large", "A tracking CSV must be under 2 MB.")
        return parse_tracking_csv(req.body)

    # --- drafts: carriers and numbers not sent yet ---------------------------------------
    #
    # Kept in a small file of their own in the open shop's home (never in the shop prefs,
    # which /api/prefs returns whole), so the page opens again the way it was left. Local
    # only: nothing here reaches Etsy until the seller confirms a send.

    def _drafts_path(self) -> Any:
        return home_dir() / DRAFTS_FILE

    def _read_drafts(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self._drafts_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        rows = data.get("rows") if isinstance(data, dict) else None
        try:
            return draft_rows(rows if isinstance(rows, list) else [])
        except ApiError:
            return []  # a file edited by hand: start again rather than fail the page

    def _write_drafts(self, rows: list[dict[str, Any]]) -> None:
        path = self._drafts_path()
        if not rows:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"rows": rows}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def _forget_drafts(self, receipt_ids: set[int]) -> None:
        """Drop the drafts of orders Etsy took (after a send, whoever started it)."""
        if not receipt_ids:
            return
        with self._drafts_lock:
            rows = self._read_drafts()
            kept = [r for r in rows if r["receipt_id"] not in receipt_ids]
            if len(kept) != len(rows):
                try:
                    self._write_drafts(kept)
                except OSError:
                    pass

    def get_drafts(self, req: Request) -> dict[str, Any]:
        """The saved drafts of orders that still wait for shipment. An order shipped
        meanwhile (from this app or on etsy.com) is dropped; when Etsy cannot be asked,
        or the waiting list is longer than one scan, the drafts are kept as they are."""
        with self._drafts_lock:
            rows = self._read_drafts()
        if not rows:
            return {"rows": [], "checked": True}
        try:
            client = self.ctx.client()
            with client.attempts(QUICK_ATTEMPTS):
                receipts, truncated = self._scan(client, "unshipped")
        except EtsyotomasyonError:  # not connected, offline, Etsy said no: keep them all
            return {"rows": rows, "checked": False}
        if truncated:
            return {"rows": rows, "checked": False}
        waiting = {r.get("receipt_id") for r in receipts}
        gone = {r["receipt_id"] for r in rows if r["receipt_id"] not in waiting}
        if gone:
            with self._drafts_lock:
                # Only what this answer dropped: a save that came in meanwhile stays.
                current = self._read_drafts()
                try:
                    self._write_drafts([r for r in current if r["receipt_id"] not in gone])
                except OSError:
                    pass
        return {"rows": [r for r in rows if r["receipt_id"] not in gone], "checked": True}

    def save_drafts(self, req: Request) -> dict[str, Any]:
        """Replace the drafts with the page's (an empty list clears them)."""
        rows = draft_rows(req.json_object().get("rows"))
        with self._drafts_lock:
            self._write_drafts(rows)
        return {"saved": len(rows)}


def _ship_error(exc: BaseException) -> tuple[str, str, int | None]:
    """(code, message, Etsy's HTTP status) for a row Etsy refused."""
    if isinstance(exc, AuthUnreachable):
        return "offline", str(exc), None
    if not isinstance(exc, EtsyApiError):
        error = to_api_error(exc)
        return error.code, error.message, None
    said = f"{exc.message} {exc.body}".lower()
    on_tracking = exc.status == 403 and exc.path.rstrip("/").endswith("/tracking")
    if on_tracking and "api key" not in said and "scope" in said:
        return "missing_scope", exc.message, exc.status
    code = to_api_error(exc).code
    # Etsy's own words, except where etsyotomasyon knows more (the country restriction).
    message = (exc.hint() or exc.message) if code == "tracking_restricted" else exc.message
    return code, message, exc.status
