"""Order (receipt) export and bulk tracking upload.

Etsy calls an order a "receipt". One receipt holds one or more transactions (the
individual items). We flatten that into one CSV row per order, with the line items
collapsed into a single readable cell, because that is the shape a seller actually
pastes into a shipping tool.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .client import EtsyClient
from .csvio import as_bool, as_int
from .errors import EtsyApiError, ValidationError

ORDER_COLUMNS = [
    "receipt_id",
    "order_date",
    "status",
    "is_paid",
    "is_shipped",
    "buyer_name",
    "buyer_email",
    "ship_address",
    "ship_city",
    "ship_state",
    "ship_zip",
    "ship_country",
    "items",
    "skus",
    "item_count",
    "order_total",
    "shipping_total",
    "tax_total",
    "currency",
    "is_gift",
    "gift_message",
    "message_from_buyer",
    "tracking_codes",
]

TRACKING_COLUMNS = ["receipt_id", "tracking_code", "carrier_name", "note_to_buyer", "send_bcc"]

_DURATION = re.compile(r"^(\d+)\s*([dwmy])$", re.IGNORECASE)
_UNIT_DAYS = {"d": 1, "w": 7, "m": 30, "y": 365}


def parse_since(value: str) -> int:
    """Accept '30d', '6w', '2026-01-01' or a raw epoch, and return a Unix timestamp."""
    value = value.strip()
    if not value:
        raise ValidationError("--since cannot be empty")

    match = _DURATION.match(value)
    if match:
        amount, unit = int(match.group(1)), match.group(2).lower()
        moment = datetime.now(timezone.utc) - timedelta(days=amount * _UNIT_DAYS[unit])
        return int(moment.timestamp())

    if value.isdigit() and len(value) >= 9:
        return int(value)

    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            parsed = datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
            return int(parsed.timestamp())
        except ValueError:
            continue

    raise ValidationError(
        f"Could not read --since {value!r}. Use 30d, 6w, 3m, or a date like 2026-01-01."
    )


def money(value: Any) -> tuple[str, str]:
    """Etsy money is {amount, divisor, currency_code}; return (formatted, currency)."""
    if not isinstance(value, dict):
        return "", ""
    amount, divisor = value.get("amount"), value.get("divisor") or 100
    if not isinstance(amount, (int, float)):
        return "", str(value.get("currency_code", ""))
    return f"{amount / divisor:.2f}", str(value.get("currency_code", ""))


def _timestamp(receipt: dict[str, Any]) -> str:
    raw = receipt.get("created_timestamp") or receipt.get("create_timestamp")
    if not isinstance(raw, (int, float)):
        return ""
    return datetime.fromtimestamp(raw, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def flatten_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    transactions = receipt.get("transactions") or []
    items = []
    skus = []
    count = 0
    for txn in transactions:
        quantity = txn.get("quantity") or 1
        count += quantity
        items.append(f"{txn.get('title', '?')} x{quantity}")
        if txn.get("sku"):
            skus.append(str(txn["sku"]))

    total, currency = money(receipt.get("grandtotal") or receipt.get("total_price"))
    shipping, _ = money(receipt.get("total_shipping_cost"))
    tax, _ = money(receipt.get("total_tax_cost"))

    tracking = [
        s.get("tracking_code")
        for s in (receipt.get("shipments") or [])
        if isinstance(s, dict) and s.get("tracking_code")
    ]

    return {
        "receipt_id": receipt.get("receipt_id"),
        "order_date": _timestamp(receipt),
        "status": receipt.get("status", ""),
        "is_paid": receipt.get("is_paid"),
        "is_shipped": receipt.get("is_shipped"),
        "buyer_name": receipt.get("name", ""),
        "buyer_email": receipt.get("buyer_email", ""),
        "ship_address": " ".join(
            part for part in (receipt.get("first_line"), receipt.get("second_line")) if part
        ),
        "ship_city": receipt.get("city", ""),
        "ship_state": receipt.get("state", ""),
        "ship_zip": receipt.get("zip", ""),
        "ship_country": receipt.get("country_iso", ""),
        "items": items,
        "skus": skus,
        "item_count": count,
        "order_total": total,
        "shipping_total": shipping,
        "tax_total": tax,
        "currency": currency,
        "is_gift": receipt.get("is_gift"),
        "gift_message": (receipt.get("gift_message") or "").replace("\n", " "),
        "message_from_buyer": (receipt.get("message_from_buyer") or "").replace("\n", " "),
        "tracking_codes": tracking,
    }


def pull(
    client: EtsyClient,
    *,
    since: int | None = None,
    unshipped_only: bool = False,
    paid_only: bool = True,
    max_items: int | None = None,
) -> list[dict[str, Any]]:
    filters: dict[str, Any] = {"sort_on": "created", "sort_order": "desc"}
    if since is not None:
        filters["min_created"] = since
    if unshipped_only:
        filters["was_shipped"] = False
    if paid_only:
        filters["was_paid"] = True
    return [flatten_receipt(r) for r in client.receipts(max_items=max_items, **filters)]


@dataclass
class ShipResult:
    row: int
    receipt_id: int | None = None
    status: str = "ok"
    message: str = ""

    @property
    def failed(self) -> bool:
        return self.status == "error"


@dataclass
class ShipReport:
    results: list[ShipResult] = field(default_factory=list)

    @property
    def shipped(self) -> int:
        return sum(1 for r in self.results if r.status == "ok")

    @property
    def errors(self) -> int:
        return sum(1 for r in self.results if r.failed)


def ship(
    client: EtsyClient | None,
    rows: Sequence[dict[str, str]],
    *,
    dry_run: bool = False,
    valid_carriers: Iterable[str] | None = None,
    on_progress: Callable[[ShipResult], None] | None = None,
) -> ShipReport:
    """Submit tracking numbers. Etsy emails the buyer and marks the order shipped.

    `client` may be None when dry_run is set: validation happens locally.
    """
    if client is None and not dry_run:
        raise ValidationError("A client is required unless dry_run is set.")

    report = ShipReport()
    carriers = {c.lower() for c in (valid_carriers or [])}

    for index, row in enumerate(rows, start=2):
        result = ShipResult(row=index)
        try:
            receipt_id = as_int(row.get("receipt_id", ""), "receipt_id", required=True)
            result.receipt_id = receipt_id

            tracking_code = (row.get("tracking_code") or "").strip()
            carrier = (row.get("carrier_name") or "").strip()
            if not tracking_code:
                raise ValidationError("tracking_code is required")
            if not carrier:
                raise ValidationError(
                    "carrier_name is required (run `etsyotomasyon orders carriers --country TR`)"
                )
            if carriers and carrier.lower() not in carriers:
                raise ValidationError(
                    f"carrier_name {carrier!r} is not in Etsy's list for this origin country"
                )

            payload: dict[str, Any] = {
                "tracking_code": tracking_code,
                "carrier_name": carrier,
            }
            note = (row.get("note_to_buyer") or "").strip()
            if note:
                payload["note_to_buyer"] = note
            send_bcc = as_bool(row.get("send_bcc", ""), "send_bcc")
            if send_bcc is not None:
                payload["send_bcc"] = send_bcc

            if dry_run:
                result.status = "dry-run"
                result.message = f"{carrier} {tracking_code}"
            else:
                client.create_receipt_shipment(receipt_id, payload)  # type: ignore[arg-type]
                result.message = f"{carrier} {tracking_code}"
        except ValidationError as exc:
            result.status = "error"
            result.message = str(exc)
        except EtsyApiError as exc:
            result.status = "error"
            hint = exc.hint()
            result.message = f"{exc.message}{' — ' + hint if hint else ''}"

        report.results.append(result)
        if on_progress:
            on_progress(result)

    return report
