"""Kâr-Zarar (profit and loss) endpoints.

    GET  /api/profit?month=YYYY-MM[&refresh=1]  the month's P&L plus the 6-month chart; when
                                                Etsy data is missing or stale a "profit" job
                                                fetches it and the answer is {"state": "loading"};
                                                a malformed or out-of-range month serves this
                                                month with "month_refused" set (no error)
    GET  /api/profit/costs[?month=YYYY-MM]    costs.json + the product types / products to price
    POST /api/profit/costs {"set": {key: number|null}, "currency"?: "USD"}
    GET  /api/profit/fx                         today's TCMB rate (fetched at most once a day)
    POST /api/profit/fx {"rate": number|null} | {"refresh": true}
    POST /api/profit/prefs {"display": "primary"|"secondary"|"both"}

Where the numbers come from:
- Revenue: the month's paid, not cancelled receipts (getShopReceipts, transactions_r):
  subtotal (items after shop coupons) + shipping charged + gift wrap - refunds. Tax is
  not revenue; it is reported separately. A refund gives the buyer back tax too, so
  only its revenue share is taken off revenue (and its tax share off the tax), and a
  fully refunded order counts as nothing (receipt_money; the Panel uses the same).
- Etsy fees: the payment-account ledger (getShopPaymentAccountLedgerEntries, transactions_r)
  grouped into listing / transaction / processing / ads / other. The OAS does not
  enumerate `ledger_type`, so entries are sorted by keywords (see LEDGER_RULES). When the
  ledger cannot be read, fees are estimated from Etsy's standard rates and marked so.
- Product and shipping costs: what the seller typed in, stored per shop in
  `home_dir()/costs.json` ({"listing:<id>": 12.5, "type:<type>": 9, "shipping:<type>": 4,
  "shipping:order": 5, "currency": "USD"}). Etsy shipping labels found in the ledger
  replace the typed shipping costs.
- TRY amounts: TCMB's today.xml (ForexSelling), cached per day in base_home()/cache/fx.json.

Etsy data is cached per month in `cache_dir()/profit/` without any cost applied, so
editing a cost only recomputes; it never refetches.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ...config import base_home, cache_dir, home_dir, read_json
from ...errors import ConfigError, EtsyApiError
from ..router import ApiError, Request

if TYPE_CHECKING:  # pragma: no cover
    from ..context import AppContext
    from ..jobs import Job
    from ..router import Router

log = logging.getLogger("etsyotomasyon.web")

# 2: refunds without their tax share, fully refunded orders as nothing, every receipt
# and ledger entry read (the old reads stopped at 12,000). Older month files are re-read.
RAW_VERSION = 2
CHART_MONTHS = 6
SELECT_MONTHS = 12
OLDEST_MONTHS = 24
JOB_KIND = "profit"
RETRY_AFTER_ERROR = 120.0  # seconds before a failed fetch is retried without being asked

# Etsy's standard seller fees, used only when the ledger cannot be read.
EST_LISTING_FEE_USD = 0.20  # per item sold (the auto-renew after a sale)
EST_TRANSACTION_RATE = 0.065  # of item price + shipping + gift wrap
EST_PROCESSING_RATE = 0.03  # of the order total, tax included
EST_PROCESSING_FIXED_USD = 0.25  # per order

BUCKETS = ("listing", "transaction", "processing", "ads", "other")
DISPLAY_MODES = ("primary", "secondary", "both")

MAX_COST = 1_000_000.0
# One month is read whole up to these (200 requests each at most). One more is asked
# for, so a month that has more is known to be cut: its fees are then estimated, and
# its revenue is marked partial rather than shown as if it were complete.
MAX_LEDGER_ENTRIES = 20_000
MAX_RECEIPTS = 20_000
MAX_REFUSED_MONTH = 40  # characters of a refused ?month= echoed back to the page
REFUND_SKIPPED = ("failed", "canceled", "cancelled")

TCMB_URL = "https://www.tcmb.gov.tr/kurlar/today.xml"
FX_TIMEOUT = 5.0
FX_RETRY_AFTER = 600.0  # after a failed fetch, wait this long before trying again
FX_MAX_BYTES = 1_000_000

# Currencies Etsy writes without minor units (ledger amounts are integers in minor units).
_ZERO_DECIMAL = frozenset({"JPY", "KRW", "VND", "CLP", "ISK", "HUF", "TWD", "UGX", "XAF", "XOF"})

_COST_KEY = re.compile(r"^(listing):([1-9][0-9]{0,18})$|^(type|shipping):([a-z_]{1,20})$")
_MONTH = re.compile(r"^(\d{4})-(\d{2})$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")

_costs_lock = threading.Lock()
_fx_lock = threading.Lock()
_raw_lock = threading.Lock()
_job_lock = threading.Lock()


def register(r: Router, ctx: AppContext) -> None:
    r.get("/api/profit", get_profit)
    r.get("/api/profit/costs", get_costs)
    r.post("/api/profit/costs", post_costs)
    r.get("/api/profit/fx", get_fx_endpoint)
    r.post("/api/profit/fx", post_fx)
    r.post("/api/profit/prefs", post_prefs)


# ================================================================== months


def month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def parse_month(value: str) -> tuple[int, int]:
    """"2026-09" -> (2026, 9); ValueError for anything else."""
    found = _MONTH.match(value or "")
    if not found:
        raise ValueError(f"not a month: {value!r}")
    year, month = int(found.group(1)), int(found.group(2))
    if not (2000 <= year <= 9999 and 1 <= month <= 12):
        raise ValueError(f"not a month: {value!r}")
    return year, month


def shift_month(ym: str, delta: int) -> str:
    year, month = parse_month(ym)
    index = year * 12 + (month - 1) + delta
    return month_key(index // 12, index % 12 + 1)


def current_month(now: float | None = None) -> str:
    moment = datetime.fromtimestamp(time.time() if now is None else now)
    return month_key(moment.year, moment.month)


def month_range(ym: str) -> tuple[int, int]:
    """First and last second of the month in the computer's local time (epoch seconds).

    The seller's own calendar month is what they compare with their bank statement.
    """
    year, month = parse_month(ym)
    start = datetime(year, month, 1)
    nxt = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
    return int(start.timestamp()), int(nxt.timestamp()) - 1


def recent_months(end: str, n: int) -> list[str]:
    """The n months ending with `end`, oldest first."""
    return [shift_month(end, -i) for i in range(n - 1, -1, -1)]


def months_between(a: str, b: str) -> int:
    ya, ma = parse_month(a)
    yb, mb = parse_month(b)
    return (yb * 12 + mb) - (ya * 12 + ma)


# ================================================================== money helpers


def money_value(value: Any) -> float | None:
    """An Etsy Money object ({amount, divisor, currency_code}) as a float; None if absent."""
    if not isinstance(value, dict):
        return None
    amount = value.get("amount")
    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        return None
    divisor = value.get("divisor")
    divisor = divisor if isinstance(divisor, (int, float)) and divisor else 100
    return float(amount) / float(divisor)


def money_currency(value: Any) -> str | None:
    if isinstance(value, dict) and isinstance(value.get("currency_code"), str):
        return value["currency_code"].upper() or None
    return None


def minor_to_major(amount: Any, currency: str | None) -> float:
    """Ledger amounts are integers in minor units (cents)."""
    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        return 0.0
    if (currency or "").upper() in _ZERO_DECIMAL:
        return float(amount)
    return float(amount) / 100.0


def _r2(value: float | None) -> float | None:
    return None if value is None else round(float(value) + 0.0, 2)


class CurrencyError(Exception):
    """Two amounts are in different currencies and there is no rate to convert them."""


Converter = Callable[[float, str, str], float]


def make_converter(rates: dict[str, float] | None) -> Converter:
    """convert(amount, from, to) through TRY with TCMB's rates (TRY per 1 unit)."""
    table = {"TRY": 1.0}
    for code, rate in (rates or {}).items():
        if isinstance(rate, (int, float)) and rate > 0:
            table[str(code).upper()] = float(rate)

    def convert(amount: float, src: str, dst: str) -> float:
        src, dst = (src or "").upper(), (dst or "").upper()
        if src == dst or not amount:
            return amount
        if src not in table or dst not in table:
            raise CurrencyError(f"no rate for {src} -> {dst}")
        return amount * table[src] / table[dst]

    return convert


# ================================================================== product types


def product_type(title: str) -> str:
    """The product type a listing title names ("... Tote Bag" -> tote), as mockups use."""
    from ...drop import catalog

    # guess() reads file names: keep the whole title in the stem.
    cleaned = re.sub(r"[./\\]+", " ", title or "").strip()
    if not cleaned:
        return "other"
    return catalog.guess(cleaned + ".txt")[0]


def product_key(listing_id: Any, title: str) -> str:
    if isinstance(listing_id, int) and not isinstance(listing_id, bool) and listing_id > 0:
        return f"l{listing_id}"
    digest = hashlib.sha1((title or "").strip().lower().encode("utf-8")).hexdigest()[:12]
    return f"t{digest}"


# ================================================================== receipts -> revenue


def _quantity(tx: dict[str, Any]) -> int:
    qty = tx.get("quantity")
    return int(qty) if isinstance(qty, (int, float)) and not isinstance(qty, bool) else 1


def _plain_value(money: Any) -> float:
    amount = money_value(money)
    return 0.0 if amount is None else amount


def receipt_money(
    receipt: dict[str, Any], val: Callable[[Any], float] | None = None
) -> dict[str, float]:
    """One receipt's money the way the Panel and Kâr-Zarar both count it.

    `val` turns an Etsy Money object into a number in the shop currency (default: its
    amount as it is). ShopReceipt (OAS): subtotal = total_price minus coupon discounts,
    without tax or shipping; total_shipping_cost; gift_wrap_price; total_tax_cost and
    total_vat_cost; refunds[].amount "equal to the refund total".

    Revenue is items + shipping + gift wrap, tax excluded. A refund pays the buyer back
    part of what they paid, tax included (the OAS does not split it), so it is split in
    the proportion tax had in the order: its revenue share comes off revenue
    (`refunded`), its tax share off the tax (`refunded_tax`), neither below zero. A
    fully refunded order is nothing, whatever its refunds list says; a cancelled one
    is nothing at all.
    """
    val = val or _plain_value
    status = str(receipt.get("status") or "").lower()
    out = dict.fromkeys(
        ("items", "shipping", "gift_wrap", "tax", "refunded", "refunded_tax", "revenue"), 0.0
    )
    if status == "canceled":
        return out
    if money_value(receipt.get("subtotal")) is not None:
        items = val(receipt.get("subtotal"))
    else:
        items = sum(
            val(tx.get("price")) * _quantity(tx)
            for tx in receipt.get("transactions") or []
            if isinstance(tx, dict)
        )
    shipping = val(receipt.get("total_shipping_cost"))
    gift_wrap = val(receipt.get("gift_wrap_price"))
    tax = val(receipt.get("total_tax_cost")) + val(receipt.get("total_vat_cost"))
    base = items + shipping + gift_wrap
    if status == "fully refunded":
        refunded, refunded_tax = base, tax
    else:
        total = sum(
            val(refund.get("amount"))
            for refund in receipt.get("refunds") or []
            if isinstance(refund, dict)
            and str(refund.get("status") or "").lower() not in REFUND_SKIPPED
        )
        gross = base + tax
        refunded_tax = min(tax, total * tax / gross) if gross > 0 and tax > 0 else 0.0
        refunded = min(max(base, 0.0), max(total - refunded_tax, 0.0))
    out.update(
        items=items, shipping=shipping, gift_wrap=gift_wrap, tax=tax,
        refunded=refunded, refunded_tax=refunded_tax, revenue=base - refunded,
    )
    return out


def summarise_receipts(
    receipts: Iterable[dict[str, Any]], currency: str, convert: Converter | None = None
) -> dict[str, Any]:
    """Revenue facts of one month's paid receipts (no costs applied).

    Items revenue is the receipt subtotal (price x quantity minus shop coupons); each
    listing gets its share of that subtotal in proportion to price x quantity, so the
    per-product revenues add up to the items revenue exactly.
    """

    def val(money: Any) -> float:
        amount = money_value(money)
        if amount is None:
            return 0.0
        code = money_currency(money) or currency
        if code != currency:
            if convert is None:
                raise CurrencyError(f"receipt in {code}, shop in {currency}")
            amount = convert(amount, code, currency)
        return amount

    out: dict[str, Any] = {
        "orders": 0,
        "items_revenue": 0.0,
        "shipping_charged": 0.0,
        "gift_wrap": 0.0,
        "refunds": 0.0,
        "tax": 0.0,
        "items_sold": 0,
        "products": {},
        "order_items": [],
    }
    products: dict[str, dict[str, Any]] = out["products"]
    for receipt in receipts:
        if not isinstance(receipt, dict):
            continue
        status = str(receipt.get("status") or "").lower()
        if status == "canceled":
            continue
        out["orders"] += 1
        lines = []
        for tx in receipt.get("transactions") or []:
            if not isinstance(tx, dict):
                continue
            qty = _quantity(tx)
            price = val(tx.get("price"))
            title = str(tx.get("title") or "").strip()
            key = product_key(tx.get("listing_id"), title)
            lines.append((key, qty, price * qty, tx))
        line_sum = sum(amount for _k, _q, amount, _t in lines)
        money = receipt_money(receipt, val)
        items = money["items"]
        factor = items / line_sum if line_sum > 0 else 0.0
        out["items_revenue"] += items
        out["shipping_charged"] += money["shipping"]
        out["gift_wrap"] += money["gift_wrap"]
        out["tax"] += money["tax"] - money["refunded_tax"]
        out["refunds"] += money["refunded"]
        keys = []
        for key, qty, amount, tx in lines:
            listing_id = tx.get("listing_id")
            item = products.get(key)
            if item is None:
                item = products[key] = {
                    "listing_id": listing_id if isinstance(listing_id, int) and listing_id > 0 else None,
                    "title": str(tx.get("title") or "").strip(),
                    "qty": 0,
                    "revenue": 0.0,
                    "image_id": tx.get("listing_image_id"),
                }
            item["qty"] += qty
            item["revenue"] += amount * factor
            out["items_sold"] += qty
            if key not in keys:
                keys.append(key)
        out["order_items"].append(keys)
    for item in products.values():
        item["revenue"] = round(item["revenue"], 4)
    for field in ("items_revenue", "shipping_charged", "gift_wrap", "refunds", "tax"):
        out[field] = round(out[field], 4)
    return out


# ================================================================== ledger -> Etsy fees

# The OAS gives `ledger_type` and `description` as free strings (no enum), so entries
# are matched by keywords, first rule wins. `None` = not a fee (sales, refunds to buyers,
# deposits, reserves, bill payments, sales tax passed through). Values seen in practice
# include listing, renew_sold, renew_expired, transaction, transaction_quantity,
# shipping_transaction, PAYMENT_PROCESSING_FEE, prolist, offsite_ads_fee,
# shipping_labels, vat_seller_services, subscription, PAYMENT, DISBURSE, REFUND.
LEDGER_RULES: tuple[tuple[str, str | None], ...] = (
    (r"label|postage", "labels"),
    (r"sales[ _-]?tax|vat[ _-]?tax|tax[ _-]?remit|remittance|marketplace[ _-]?tax", None),
    (r"prolist|promoted|offsite|\bads?\b|_ads?\b|\bads?_|advert", "ads"),
    (r"processing|payment[ _-]?fee|card[ _-]?fee|cc[ _-]?fee", "processing"),
    (r"transaction", "transaction"),
    (r"listing|renew", "listing"),
    (r"fee|vat|subscription|plus|pattern|regulatory|conversion", "other"),
    (r"payment|refund|disburse|deposit|reserve|recoup|payout|credit|sale\b|sales\b|bill", None),
)
_RULES = tuple((re.compile(pattern), bucket) for pattern, bucket in LEDGER_RULES)
# A positive (credit) entry counts against its bucket only when it says it is a give-back.
_CREDIT_WORDS = re.compile(r"refund|credit|revers|return|fee|void|adjust")


def _ledger_text(entry: dict[str, Any]) -> str:
    return " ".join(
        str(entry.get(field) or "") for field in ("ledger_type", "description")
    ).strip().lower()


def classify_ledger(entry: dict[str, Any]) -> str | None:
    """The fee bucket of a ledger entry: listing | transaction | processing | ads | other,
    "labels" for Etsy shipping labels, or None when the entry is not a fee."""
    text = _ledger_text(entry)
    for pattern, bucket in _RULES:
        if pattern.search(text):
            return bucket
    # Unknown: a debit is money Etsy took (counted as other); a credit is ignored.
    amount = entry.get("amount")
    if isinstance(amount, (int, float)) and amount < 0:
        return "other"
    return None


def summarise_ledger(
    entries: Iterable[dict[str, Any]], currency: str, convert: Converter | None = None
) -> dict[str, Any]:
    """Fee buckets (positive = money Etsy took) and Etsy shipping labels bought."""
    buckets = dict.fromkeys(BUCKETS, 0.0)
    labels = 0.0
    fee_entries = label_entries = seen = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        seen += 1
        bucket = classify_ledger(entry)
        if bucket is None:
            continue
        code = str(entry.get("currency") or currency).upper()
        amount = minor_to_major(entry.get("amount"), code)
        if amount > 0 and not _CREDIT_WORDS.search(_ledger_text(entry)):
            # Money in that does not say it gives a fee back (a sale booked under a
            # fee-like type, say) must not cancel out real fees.
            continue
        if code != currency:
            if convert is None:
                raise CurrencyError(f"ledger in {code}, shop in {currency}")
            amount = convert(amount, code, currency)
        if bucket == "labels":
            labels -= amount
            label_entries += 1
        else:
            buckets[bucket] -= amount
            fee_entries += 1
    return {
        "buckets": {k: round(v, 4) for k, v in buckets.items()},
        "labels": round(labels, 4),
        "fee_entries": fee_entries,
        "label_entries": label_entries,
        "entries": seen,
    }


def estimate_fees(raw: dict[str, Any], usd: float = 1.0) -> dict[str, float]:
    """Etsy's standard fees for a month (used when the ledger cannot be read).

    `usd` converts one US dollar into the shop currency (the fixed fees are in USD).
    """
    items = float(raw.get("items_revenue") or 0)
    shipping = float(raw.get("shipping_charged") or 0)
    gift = float(raw.get("gift_wrap") or 0)
    tax = float(raw.get("tax") or 0)
    orders = int(raw.get("orders") or 0)
    sold = int(raw.get("items_sold") or 0)
    return {
        "listing": round(EST_LISTING_FEE_USD * usd * sold, 4),
        "transaction": round(EST_TRANSACTION_RATE * (items + shipping + gift), 4),
        "processing": round(
            EST_PROCESSING_RATE * (items + shipping + gift + tax)
            + EST_PROCESSING_FIXED_USD * usd * orders,
            4,
        ),
        "ads": 0.0,
        "other": 0.0,
    }


# ================================================================== costs.json


def costs_path() -> Path:
    return home_dir() / "costs.json"


def load_costs() -> dict[str, Any]:
    """The open shop's costs; {} when none were entered (or the file is unreadable)."""
    try:
        data = read_json(costs_path())
    except ConfigError:
        log.warning("costs.json is unreadable; ignoring it")
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key == "currency":
            if isinstance(value, str) and _CURRENCY.match(value):
                out[key] = value
        elif _COST_KEY.match(str(key)) and _is_amount(value):
            out[key] = float(value)
    return out


def _is_amount(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 <= value <= MAX_COST
    )


def validate_cost_key(key: str) -> str:
    from ...drop import catalog

    found = _COST_KEY.match(key or "")
    if not found:
        raise ApiError(422, "invalid", f"not a cost key: {key!r}", field="set")
    kind, name = found.group(3), found.group(4)
    if kind and not (name in catalog.TYPES or (kind == "shipping" and name == "order")):
        raise ApiError(422, "invalid", f"unknown product type: {name!r}", field="set")
    return key


def update_costs(changes: dict[str, Any], currency: str | None = None) -> dict[str, Any]:
    """Merge validated changes (None removes a key) into costs.json and return it."""
    from ...config import write_json_private

    clean: dict[str, float | None] = {}
    for key, value in changes.items():
        validate_cost_key(str(key))
        if value is None or value == "":
            clean[str(key)] = None
        elif _is_amount(value):
            clean[str(key)] = round(float(value), 4)
        else:
            raise ApiError(422, "invalid", f"{key}: a cost is a number from 0 to {MAX_COST:g}",
                           field=str(key))
    if currency is not None and not (isinstance(currency, str) and _CURRENCY.match(currency)):
        raise ApiError(422, "invalid", "currency must be a 3-letter code", field="currency")
    with _costs_lock:
        costs = load_costs()
        for key, value in clean.items():
            if value is None:
                costs.pop(key, None)
            else:
                costs[key] = value
        if currency is not None:
            costs["currency"] = currency
        write_json_private(costs_path(), costs)
    return costs


# ================================================================== FX (TCMB)


def fx_path() -> Path:
    return base_home() / "cache" / "fx.json"


def parse_tcmb(data: bytes | str) -> dict[str, Any]:
    """TCMB today.xml -> {"date": "YYYY-MM-DD", "rates": {"USD": 34.12, ...}} (TRY per unit).

    ForexSelling divided by Unit (JPY is quoted per 100). Currencies without a
    ForexSelling value are skipped. ValueError when the document is not TCMB's.
    """
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError(f"not XML: {exc}") from exc
    if root.tag != "Tarih_Date":
        raise ValueError("not a TCMB rates document")
    stamp = root.get("Date") or ""  # "09/25/2026"
    bulletin = None
    try:
        bulletin = datetime.strptime(stamp, "%m/%d/%Y").date().isoformat()
    except ValueError:
        try:
            bulletin = datetime.strptime(root.get("Tarih") or "", "%d.%m.%Y").date().isoformat()
        except ValueError:
            bulletin = None
    rates: dict[str, float] = {}
    for node in root.findall("Currency"):
        code = (node.get("CurrencyCode") or node.get("Kod") or "").strip().upper()
        if not _CURRENCY.match(code):
            continue
        try:
            unit = float((node.findtext("Unit") or "1").strip() or "1")
            selling = float((node.findtext("ForexSelling") or "").strip())
        except ValueError:
            continue
        if unit > 0 and selling > 0:
            rates[code] = round(selling / unit, 6)
    if not rates:
        raise ValueError("no rates in the TCMB document")
    return {"date": bulletin, "rates": rates}


def _fetch_tcmb() -> dict[str, Any]:
    """GET today.xml from TCMB (a public page; nothing is sent)."""
    import httpx

    with httpx.Client(timeout=FX_TIMEOUT, follow_redirects=True, trust_env=True) as http:
        resp = http.get(TCMB_URL, headers={"Accept": "application/xml"})
        resp.raise_for_status()
        if len(resp.content) > FX_MAX_BYTES:
            raise ValueError("the TCMB answer is too large")
        return parse_tcmb(resp.content)


def _read_fx() -> dict[str, Any]:
    try:
        data = json.loads(fx_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def get_fx(*, fetch: bool = True, force: bool = False) -> dict[str, Any]:
    """The rate table: fx.json, refreshed from TCMB once per local day.

    {"rates", "date" (bulletin), "fetched_on", "fetched_at", "manual": {code: rate},
     "error": None | "offline"}. Never raises.
    """
    with _fx_lock:
        data = _read_fx()
        today = date.today().isoformat()
        due = force or data.get("fetched_on") != today or not data.get("rates")
        recently_failed = time.time() - float(data.get("failed_at") or 0) < FX_RETRY_AFTER
        data["error"] = None
        if fetch and due and (force or not recently_failed):
            try:
                fresh = _fetch_tcmb()
            except Exception as exc:  # noqa: BLE001 — any failure falls back to the cache
                log.info("TCMB rates could not be fetched: %s", exc)
                data["failed_at"] = round(time.time(), 3)
                data["error"] = "offline"
            else:
                data.update(
                    rates=fresh["rates"], date=fresh["date"], fetched_on=today,
                    fetched_at=round(time.time(), 3),
                )
                data.pop("failed_at", None)
            try:
                _write_json(fx_path(), {k: v for k, v in data.items() if k != "error"})
            except OSError:
                log.warning("could not save the exchange rates")
        elif due and recently_failed:
            data["error"] = "offline"
        return data


def fx_rates(fx: dict[str, Any]) -> dict[str, float]:
    """TCMB's rates with any manual rate on top."""
    rates = dict(fx.get("rates") or {})
    for code, rate in (fx.get("manual") or {}).items():
        if isinstance(rate, (int, float)) and rate > 0:
            rates[str(code).upper()] = float(rate)
    return rates


def fx_pair(fx: dict[str, Any], currency: str) -> dict[str, Any] | None:
    """How to show a second currency next to the shop's: TL for most shops, USD for a TL shop.

    {"base", "quote", "rate" (quote per 1 base), "source": tcmb|manual, "date", "stale"}.
    """
    currency = (currency or "USD").upper()
    base, quote = (currency, "TRY") if currency != "TRY" else ("USD", "TRY")
    manual = (fx.get("manual") or {}).get(base)
    today = date.today().isoformat()
    if isinstance(manual, (int, float)) and manual > 0:
        rate, source, when, stale = float(manual), "manual", None, False
    else:
        rate = (fx.get("rates") or {}).get(base)
        if not isinstance(rate, (int, float)) or rate <= 0:
            return None
        rate, source, when = float(rate), "tcmb", fx.get("date")
        stale = fx.get("fetched_on") != today
    return {
        "base": base,
        "quote": quote,
        "rate": round(rate, 6),
        "source": source,
        "date": when,
        "stale": stale,
        "shop_is_quote": currency == "TRY",
    }


def set_manual_rate(base: str, rate: float | None) -> dict[str, Any]:
    with _fx_lock:
        data = _read_fx()
        manual = dict(data.get("manual") or {})
        if rate is None:
            manual.pop(base, None)
        else:
            manual[base] = float(rate)
        data["manual"] = manual
        _write_json(fx_path(), {k: v for k, v in data.items() if k != "error"})
        return data


# ================================================================== month cache


def _cache_folder() -> Path:
    return cache_dir() / "profit"


def raw_path(shop_key: str, ym: str) -> Path:
    safe = re.sub(r"[^0-9A-Za-z_-]", "", str(shop_key)) or "shop"
    return _cache_folder() / f"{safe}-{ym}.json"


def images_path(shop_key: str) -> Path:
    safe = re.sub(r"[^0-9A-Za-z_-]", "", str(shop_key)) or "shop"
    return _cache_folder() / f"{safe}-images.json"


def load_raw(shop_key: str, ym: str) -> dict[str, Any] | None:
    try:
        data = json.loads(raw_path(shop_key, ym).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("v") != RAW_VERSION or data.get("month") != ym:
        return None
    return data


def save_raw(shop_key: str, raw: dict[str, Any]) -> None:
    with _raw_lock:
        _write_json(raw_path(shop_key, raw["month"]), raw)


def load_images(shop_key: str) -> dict[str, str]:
    try:
        data = json.loads(images_path(shop_key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)} if isinstance(data, dict) else {}


def raw_is_fresh(raw: dict[str, Any] | None, ym: str, now: float | None = None) -> bool:
    """Current month: 15 minutes. A month that had not ended (+2 days for late fees) when
    it was fetched: 6 hours. A settled month: a week."""
    if not raw:
        return False
    now = time.time() if now is None else now
    fetched = float(raw.get("fetched_at") or 0)
    age = now - fetched
    if ym == current_month(now):
        return age < 15 * 60
    settled_after = month_range(ym)[1] + 2 * 86400
    if fetched < settled_after:
        return age < 6 * 3600
    return age < 7 * 86400


# ================================================================== fetching (runs in a job)


def _fatal(exc: EtsyApiError) -> bool:
    """Errors that must stop the whole calculation instead of falling back."""
    return exc.status in (0, 401, 429)


def fetch_month(
    client: Any,
    ym: str,
    *,
    currency: str,
    scopes: Iterable[str],
    fx: Callable[[], dict[str, Any]],
    progress: Callable[[str, int], None] = lambda step, n: None,
    check_cancel: Callable[[], None] = lambda: None,
) -> dict[str, Any]:
    """Everything the P&L of month `ym` needs from Etsy, without costs."""
    start, end = month_range(ym)
    rates: dict[str, float] | None = None

    def convert(amount: float, src: str, dst: str) -> float:
        nonlocal rates
        if rates is None:
            rates = fx_rates(fx())
        return make_converter(rates)(amount, src, dst)

    receipts: list[dict[str, Any]] = []
    progress("receipts", 0)
    for receipt in client.receipts(
        min_created=start, max_created=end, was_paid=True, was_canceled=False,
        max_items=MAX_RECEIPTS + 1,
    ):
        receipts.append(receipt)
        if len(receipts) % 100 == 0:
            check_cancel()
            progress("receipts", len(receipts))
    receipts_truncated = len(receipts) > MAX_RECEIPTS
    summary = summarise_receipts(receipts[:MAX_RECEIPTS], currency, convert)
    check_cancel()

    fees: dict[str, Any] = {"source": "ledger", "reason": None, "status": None,
                            "buckets": dict.fromkeys(BUCKETS, 0.0), "labels": 0.0}
    if "transactions_r" not in set(scopes):
        fees.update(source="estimate", reason="scope")
    else:
        progress("ledger", 0)
        entries: list[dict[str, Any]] = []
        try:
            for entry in client.ledger_entries(start, end, max_items=MAX_LEDGER_ENTRIES + 1):
                entries.append(entry)
                if len(entries) % 100 == 0:
                    check_cancel()
                    progress("ledger", len(entries))
        except EtsyApiError as exc:
            if _fatal(exc):
                raise
            log.info("ledger unavailable for %s: %s", ym, exc)
            fees.update(source="estimate", reason="forbidden" if exc.status == 403 else "unavailable",
                        status=exc.status)
        else:
            try:
                ledger = summarise_ledger(entries, currency, convert)
            except CurrencyError:
                fees.update(source="estimate", reason="currency")
            else:
                if len(entries) > MAX_LEDGER_ENTRIES:
                    # A cut-off ledger would under-count fees: estimate instead.
                    fees.update(source="estimate", reason="too_many")
                else:
                    fees.update(buckets=ledger["buckets"], labels=ledger["labels"],
                                entries=ledger["entries"])
                    if ledger["fee_entries"] == 0 and summary["orders"] > 0:
                        fees.update(source="estimate", reason="no_fees")
    if fees["source"] == "estimate":
        usd = 1.0
        if currency != "USD":
            try:
                usd = convert(1.0, "USD", currency)
            except CurrencyError:
                usd = 1.0
        fees["buckets"] = estimate_fees(summary, usd)
        fees["labels"] = 0.0
    check_cancel()
    return {
        "v": RAW_VERSION,
        "month": ym,
        "fetched_at": round(time.time(), 3),
        "currency": currency,
        **summary,
        # More paid orders than MAX_RECEIPTS: revenue covers the newest MAX_RECEIPTS only.
        "orders_truncated": receipts_truncated,
        "fees": fees,
    }


def fetch_images(client: Any, listing_ids: list[int]) -> dict[str, str]:
    """listing_id -> a small image URL, from getListingsByListingIds (key only)."""
    found: dict[str, str] = {}
    if not listing_ids:
        return found
    try:
        listings = client.listings_batch(listing_ids, includes=["Images"])
    except EtsyApiError as exc:
        if _fatal(exc):
            raise
        log.info("listing images unavailable: %s", exc)
        return found
    for listing in listings:
        if not isinstance(listing, dict):
            continue
        images = [i for i in listing.get("images") or [] if isinstance(i, dict)]
        images.sort(key=lambda i: i.get("rank") or 0)
        if images:
            url = images[0].get("url_170x135") or images[0].get("url_75x75") or images[0].get("url_570xN")
            if isinstance(url, str) and url.startswith("https://"):
                found[str(listing.get("listing_id"))] = url
    return found


# ================================================================== the calculation


def cost_for(costs: dict[str, Any], listing_id: Any, kind: str) -> tuple[float | None, str | None]:
    """Unit cost (in the costs currency) and where it came from: listing | type | None."""
    if listing_id is not None:
        value = costs.get(f"listing:{listing_id}")
        if value is not None:
            return float(value), "listing"
    value = costs.get(f"type:{kind}")
    if value is not None:
        return float(value), "type"
    return None, None


def compute_month(
    raw: dict[str, Any],
    costs: dict[str, Any],
    *,
    convert: Converter | None = None,
    images: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The P&L of one month from its Etsy facts and the seller's costs."""
    currency = raw.get("currency") or "USD"
    images = images or {}
    costs_currency = costs.get("currency") or currency
    factor: float | None = 1.0
    if costs_currency != currency:
        try:
            factor = (convert or make_converter({}))(1.0, costs_currency, currency)
        except CurrencyError:
            factor = None
    has_product_costs = any(k.startswith(("listing:", "type:")) for k in costs)
    has_shipping_costs = any(k.startswith("shipping:") for k in costs)

    revenue = (
        float(raw.get("items_revenue") or 0)
        + float(raw.get("shipping_charged") or 0)
        + float(raw.get("gift_wrap") or 0)
        - float(raw.get("refunds") or 0)
    )
    fees_raw = raw.get("fees") or {}
    buckets = {k: float((fees_raw.get("buckets") or {}).get(k) or 0) for k in BUCKETS}
    fees_total = sum(buckets.values())

    products = []
    types: dict[str, str] = {}
    product_cost = 0.0
    missing_qty = 0
    for key, item in (raw.get("products") or {}).items():
        kind = product_type(item.get("title") or "")
        types[key] = kind
        listing_id = item.get("listing_id")
        unit_in, source = cost_for(costs, listing_id, kind)
        unit = unit_in * factor if unit_in is not None and factor is not None else None
        qty = int(item.get("qty") or 0)
        item_revenue = float(item.get("revenue") or 0)
        cost = unit * qty if unit is not None else None
        net = item_revenue - cost if cost is not None else None
        if cost is None:
            missing_qty += qty
        else:
            product_cost += cost
        products.append({
            "key": key,
            "listing_id": listing_id,
            "title": item.get("title") or "",
            "type": kind,
            "qty": qty,
            "revenue": _r2(item_revenue),
            "unit_cost": _r2(unit),
            "unit_input": unit_in,
            "cost_source": source,
            "cost": _r2(cost),
            "net": _r2(net),
            "margin": round(net / item_revenue, 4) if net is not None and item_revenue > 0 else None,
            "image": images.get(str(listing_id)) if listing_id is not None else None,
        })

    labels = float(fees_raw.get("labels") or 0)
    shipping_cost: float | None
    missing_orders = 0
    if labels > 0:
        shipping_source = "labels"
        shipping_cost = labels
    elif has_shipping_costs and factor is not None:
        shipping_source = "costs"
        shipping_cost = 0.0
        flat = costs.get("shipping:order")
        for keys in raw.get("order_items") or []:
            values = [
                costs[f"shipping:{types.get(k, 'other')}"]
                for k in keys
                if f"shipping:{types.get(k, 'other')}" in costs
            ]
            value = max(values) if values else flat
            if value is None:
                missing_orders += 1
            else:
                shipping_cost += float(value) * factor
    else:
        shipping_source = None
        shipping_cost = None
        missing_orders = int(raw.get("orders") or 0)

    product_ready = has_product_costs and factor is not None
    net = None
    if product_ready and shipping_cost is not None:
        net = revenue - fees_total - product_cost - shipping_cost
    orders = int(raw.get("orders") or 0)
    return {
        "month": raw.get("month"),
        "currency": currency,
        "orders": orders,
        "items_sold": int(raw.get("items_sold") or 0),
        "revenue": _r2(revenue),
        # True when the month had more paid orders than could be read (see MAX_RECEIPTS).
        "partial": bool(raw.get("orders_truncated")),
        "partial_limit": MAX_RECEIPTS if raw.get("orders_truncated") else None,
        "items_revenue": _r2(raw.get("items_revenue") or 0),
        "shipping_charged": _r2(raw.get("shipping_charged") or 0),
        "gift_wrap": _r2(raw.get("gift_wrap") or 0),
        "refunds": _r2(raw.get("refunds") or 0),
        "tax": _r2(raw.get("tax") or 0),
        "avg_order": _r2(revenue / orders) if orders else None,
        "fees": {
            "total": _r2(fees_total),
            "source": fees_raw.get("source") or "ledger",
            "reason": fees_raw.get("reason"),
            "status": fees_raw.get("status"),
            "buckets": {k: _r2(v) for k, v in buckets.items()},
        },
        "product_cost": _r2(product_cost) if product_ready else None,
        "missing_cost_qty": missing_qty if product_ready else int(raw.get("items_sold") or 0),
        "shipping_cost": _r2(shipping_cost),
        "shipping_source": shipping_source,
        "missing_shipping_orders": missing_orders,
        "net": _r2(net),
        "margin": round(net / revenue, 4) if net is not None and revenue > 0 else None,
        "products": products,
        "fetched_at": raw.get("fetched_at"),
    }


# ================================================================== handlers


def _month_param(req: Request, name: str = "month") -> str:
    now_month = current_month()
    value = (req.query.get(name) or "").strip() or now_month
    try:
        parse_month(value)
    except ValueError as exc:
        raise ApiError(422, "invalid", f"{name} must look like 2026-09", field=name) from exc
    ahead = months_between(now_month, value)
    if ahead > 0 or ahead < -(OLDEST_MONTHS - 1):
        raise ApiError(422, "invalid", f"{name} is out of range", field=name)
    return value


def _month_or_latest(req: Request) -> tuple[str, str | None]:
    """The asked month, or this month when the asked one is malformed or out of range.

    An old bookmark (?month=2019-01) then opens the latest month with a notice on the
    page instead of an error. Returns (month, the refused value or None).
    """
    try:
        return _month_param(req), None
    except ApiError:
        refused = (req.query.get("month") or "").strip()
        return current_month(), refused[:MAX_REFUSED_MONTH]


def _shop_key(ctx: AppContext, client: Any) -> str:
    """The Etsy shop id (cache files are per Etsy shop), without a network call if known."""
    known = ctx.status["shop"].get("etsy_shop_id")
    if known:
        return str(known)
    try:
        info = read_json(home_dir() / "shop.json") or {}
    except ConfigError:
        info = {}
    if isinstance(info, dict) and info.get("etsy_shop_id"):
        return str(info["etsy_shop_id"])
    return str(client.shop_id())


def _display(ctx: AppContext) -> str:
    value = ctx.shop_prefs().get("profit_display")
    return value if value in DISPLAY_MODES else "both"


def _scopes(client: Any) -> list[str]:
    token = getattr(client, "token", None)
    return list(getattr(token, "scopes", None) or [])


def _active_job(ctx: AppContext) -> Any:
    shop = ctx.shop_id
    for job in ctx.jobs.list(kind=JOB_KIND, active=True):
        if job.params.get("shop") == shop:
            return job
    return None


def _last_job(ctx: AppContext) -> Any:
    """The newest profit job of the open shop, whatever its status."""
    shop = ctx.shop_id
    for job in ctx.jobs.list(kind=JOB_KIND):
        if job.params.get("shop") == shop:
            return job
    return None


def _start_job(ctx: AppContext, shop_key: str, month: str, missing: list[str]) -> Any:
    shop = ctx.shop_id

    def work(job: Job) -> dict[str, Any]:
        client = ctx.client()
        currency = (ctx.status["shop"].get("currency") or "").upper()
        if not currency:
            currency = str(client.shop().get("currency_code") or "USD").upper()
        scopes = _scopes(client)
        fx_cache: dict[str, Any] = {}

        def fx() -> dict[str, Any]:
            if not fx_cache:
                fx_cache.update(get_fx())
            return fx_cache

        # Ten progress units per month (1 = reading orders, 4..9 = reading the ledger,
        # which is the long part) and ten for the thumbnails.
        steps = (len(missing) + 1) * 10
        for index, ym in enumerate(missing):
            job.check_cancel()

            def progress(step: str, n: int, index: int = index, ym: str = ym) -> None:
                part = 1 if step == "receipts" else 4 + min(5, n // 400)
                job.progress(index * 10 + part, steps, label=f"{ym}|{step}|{n}")

            raw = fetch_month(client, ym, currency=currency, scopes=scopes, fx=fx,
                              progress=progress, check_cancel=job.check_cancel)
            save_raw(shop_key, raw)
            job.set_state(done=missing[: index + 1])
        # Thumbnails for every product of the chart's months that has none yet.
        job.progress(len(missing) * 10, steps, label=f"{month}|images|0")
        images = load_images(shop_key)
        wanted: list[int] = []
        for ym in recent_months(month, CHART_MONTHS):
            raw = load_raw(shop_key, ym) or {}
            for item in (raw.get("products") or {}).values():
                listing_id = item.get("listing_id")
                if isinstance(listing_id, int) and str(listing_id) not in images:
                    wanted.append(listing_id)
        wanted = list(dict.fromkeys(wanted))[:500]
        if wanted:
            job.check_cancel()
            images.update(fetch_images(client, wanted))
            # Remember misses too, so a deleted listing is not asked for again and again.
            for listing_id in wanted:
                images.setdefault(str(listing_id), "")
            try:
                with _raw_lock:
                    _write_json(images_path(shop_key), images)
            except OSError:
                log.warning("could not save listing thumbnails")
        job.progress(steps, steps, label=f"{month}|done|0")
        return {"months": missing}

    # Only reads Etsy (and writes this app's own cache): the read lane, so the numbers
    # do not wait behind a long Tasarım Yükle upload on the write lane.
    return ctx.jobs.start(
        JOB_KIND, "profit:job.title", work,
        params={"shop": shop, "month": month, "months": missing}, cancellable=True,
        lane="read",
    )


def _costs_view(costs: dict[str, Any], currency: str) -> dict[str, Any]:
    return {
        "costs": costs,
        "currency": costs.get("currency") or currency,
        "configured": {
            "product": any(k.startswith(("listing:", "type:")) for k in costs),
            "shipping": any(k.startswith("shipping:") for k in costs),
        },
    }


PROFIT_NOTE_PREF = "panel_profit_note"  # {month: signature of the totals last announced}
PROFIT_NOTE_MONTHS = 12


def _note_month(ctx: AppContext, month: str, summary: dict[str, Any]) -> None:
    """Tell the Panel a month's P&L is ready ("Eylül kâr-zarar özeti güncellendi"): once per
    shop and month, and again only when that month's totals change."""
    fees = summary.get("fees") or {}
    totals = [summary.get("revenue"), fees.get("total"), summary.get("product_cost"),
              summary.get("shipping_cost"), summary.get("net"), summary.get("orders")]
    sig = hashlib.sha1(json.dumps(totals).encode("utf-8")).hexdigest()[:16]
    try:
        stored = ctx.shop_prefs().get(PROFIT_NOTE_PREF)
        notes = {str(k): str(v) for k, v in stored.items()} if isinstance(stored, dict) else {}
        if notes.get(month) == sig:
            return
        notes[month] = sig
        kept = dict(sorted(notes.items())[-PROFIT_NOTE_MONTHS:])
        ctx.update_shop_prefs(**{PROFIT_NOTE_PREF: kept})
        ctx.notify("panel", "notify.profit_month", {"month": month}, tone="success",
                   link="/kar-zarar", replace=True)
    except Exception:  # noqa: BLE001 — a note must never break the page
        log.exception("could not note the %s profit summary", month)


def get_profit(req: Request) -> dict[str, Any]:
    ctx = req.ctx
    assert ctx is not None
    month, refused = _month_or_latest(req)
    refresh = req.bool_query("refresh")
    client = ctx.client()
    shop_key = _shop_key(ctx, client)
    months = recent_months(month, CHART_MONTHS)
    now = time.time()
    raws = {ym: load_raw(shop_key, ym) for ym in months}
    status_currency = (ctx.status["shop"].get("currency") or "").upper() or None
    currency = (raws.get(month) or {}).get("currency") or status_currency or "USD"
    fx = get_fx(fetch=False)
    pair = fx_pair(fx, currency) if fx.get("rates") or fx.get("manual") else None
    base = {
        "month": month,
        # The ?month= that could not be shown (the latest month is served instead).
        "month_refused": refused,
        "months": recent_months(current_month(now), SELECT_MONTHS)[::-1],
        "currency": currency,
        "display": _display(ctx),
        "fx": pair,
        "fx_due": fx.get("fetched_on") != date.today().isoformat(),
    }

    # Stale while revalidate: months never fetched make the page wait for the job; months
    # that are only stale are shown from the cache while the job refreshes them.
    # A job that failed a moment ago is not restarted by itself (that would loop while
    # Etsy is unreachable); refresh=1 (the page's "try again") always starts one.
    with _job_lock:
        running = _active_job(ctx)
        last = _last_job(ctx)
        failed = (
            last if last is not None and last.status == "error"
            and now - float(last.finished_at or 0) < RETRY_AFTER_ERROR else None
        )
        absent = [ym for ym in months if raws[ym] is None]
        stale = months if refresh else [ym for ym in months if not raw_is_fresh(raws[ym], ym, now)]
        job = running
        if job is None and stale and (refresh or failed is None):
            # The selected month first, then the newest ones.
            ordered = [month] + [ym for ym in reversed(months) if ym != month]
            job = _start_job(ctx, shop_key, month, [ym for ym in ordered if ym in stale])
        if absent:
            shown = job or failed
            return {**base, "state": "loading", "job": shown.summary() if shown else None}
    refreshing = job.summary() if job is not None else None
    refresh_error = failed.summary()["error"] if job is None and failed is not None else None

    costs = load_costs()
    convert = make_converter(fx_rates(fx))
    images = load_images(shop_key)
    series = []
    results: dict[str, dict[str, Any]] = {}
    for ym in months:
        raw = raws[ym]
        if raw is None:
            series.append({"month": ym, "net": None, "revenue": None, "orders": None})
            continue
        result = compute_month(raw, costs, convert=convert, images=images)
        results[ym] = result
        series.append({"month": ym, "net": result["net"], "revenue": result["revenue"],
                       "orders": result["orders"]})
    summary = results[month]
    if refreshing is None:
        _note_month(ctx, month, summary)
    previous = results.get(shift_month(month, -1))
    return {
        **base,
        "state": "ready",
        "refreshing": refreshing,
        "refresh_error": refresh_error,
        "summary": summary,
        "previous": None if previous is None else {
            "month": previous["month"], "net": previous["net"], "revenue": previous["revenue"],
        },
        "series": series,
        "costs": _costs_view(costs, currency),
        "fetched_at": summary.get("fetched_at"),
    }


def get_costs(req: Request) -> dict[str, Any]:
    """costs.json, the product types seen in the chart's months and their products."""
    ctx = req.ctx
    assert ctx is not None
    month = _month_param(req)
    costs = load_costs()
    status_currency = (ctx.status["shop"].get("currency") or "").upper() or None
    products: dict[str, dict[str, Any]] = {}
    currency = status_currency
    try:
        client = ctx.client()
        shop_key = _shop_key(ctx, client)
    except (ApiError, EtsyApiError):
        shop_key = None
    if shop_key is not None:
        images = load_images(shop_key)
        for ym in recent_months(month, CHART_MONTHS):
            raw = load_raw(shop_key, ym)
            if not raw:
                continue
            currency = currency or raw.get("currency")
            for key, item in (raw.get("products") or {}).items():
                entry = products.get(key)
                if entry is None:
                    listing_id = item.get("listing_id")
                    entry = products[key] = {
                        "key": key,
                        "listing_id": listing_id,
                        "title": item.get("title") or "",
                        "type": product_type(item.get("title") or ""),
                        "qty": 0,
                        "image": images.get(str(listing_id)) or None if listing_id else None,
                    }
                entry["qty"] += int(item.get("qty") or 0)
    from ...drop import catalog

    seen_types = {p["type"] for p in products.values()}
    seen_types.update(k.split(":", 1)[1] for k in costs if k.startswith(("type:", "shipping:")))
    seen_types.discard("order")
    types = [t for t in catalog.TYPES if t in seen_types]
    items = sorted(products.values(), key=lambda p: (-p["qty"], p["title"].lower()))
    for item in items:
        listing_id = item["listing_id"]
        item["unit"] = costs.get(f"listing:{listing_id}") if listing_id else None
    currency = currency or "USD"
    return {
        **_costs_view(costs, currency),
        "shop_currency": currency,
        "types": [
            {
                "id": t,
                "unit": costs.get(f"type:{t}"),
                "shipping": costs.get(f"shipping:{t}"),
                "products": sum(1 for p in products.values() if p["type"] == t),
            }
            for t in types
        ],
        "shipping_order": costs.get("shipping:order"),
        "products": items,
    }


def post_costs(req: Request) -> dict[str, Any]:
    body = req.json_object()
    changes = body.get("set") or {}
    if not isinstance(changes, dict):
        raise ApiError(422, "invalid", "set must be an object", field="set")
    currency = body.get("currency")
    if currency is not None and not isinstance(currency, str):
        raise ApiError(422, "invalid", "currency must be a 3-letter code", field="currency")
    costs = update_costs(changes, currency.upper() if isinstance(currency, str) else None)
    ctx = req.ctx
    assert ctx is not None
    status_currency = (ctx.status["shop"].get("currency") or "").upper() or "USD"
    return _costs_view(costs, status_currency)


def _fx_view(fx: dict[str, Any], currency: str) -> dict[str, Any]:
    return {
        "pair": fx_pair(fx, currency),
        "error": fx.get("error"),
        "fetched_at": fx.get("fetched_at"),
        "manual": bool(fx.get("manual") or {}),
    }


def _currency(ctx: AppContext) -> str:
    return (ctx.status["shop"].get("currency") or "").upper() or "USD"


def get_fx_endpoint(req: Request) -> dict[str, Any]:
    ctx = req.ctx
    assert ctx is not None
    return _fx_view(get_fx(), _currency(ctx))


def post_fx(req: Request) -> dict[str, Any]:
    ctx = req.ctx
    assert ctx is not None
    body = req.json_object()
    currency = _currency(ctx)
    base = currency if currency != "TRY" else "USD"
    if body.get("refresh"):
        return _fx_view(get_fx(force=True), currency)
    if "rate" not in body:
        raise ApiError(422, "invalid", "send rate (a number, or null to clear it)", field="rate")
    rate = body.get("rate")
    if rate is not None and not (
        isinstance(rate, (int, float)) and not isinstance(rate, bool)
        and math.isfinite(rate) and 0 < rate < 1_000_000
    ):
        raise ApiError(422, "invalid", "rate must be a positive number", field="rate")
    set_manual_rate(base, None if rate is None else float(rate))
    return _fx_view(get_fx(fetch=False), currency)


def post_prefs(req: Request) -> dict[str, Any]:
    ctx = req.ctx
    assert ctx is not None
    body = req.json_object()
    display = body.get("display")
    if display not in DISPLAY_MODES:
        raise ApiError(422, "invalid", "display must be primary, secondary or both", field="display")
    ctx.update_shop_prefs(profit_display=display)
    return {"display": display}
