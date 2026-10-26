"""The production partner: where an order's print files are sent to be made.

Every seller connects their own: a name, the address their partner (or their own Zapier,
Make or server) takes orders at, and an optional token. Nothing here is etsyotomasyon's: no
partner is built in, no account, no key of ours. One click on an order sends it, and
nothing is sent before that.

What is sent is one JSON object (`build_payload`): who to ship to, each item with its size,
paper and file, and the invoice sheet. The files travel inside it (base64) when the seller
turned that on and each is small enough; otherwise their names and sizes go and the
partner (or the seller) takes the files from the order's folder.

The seller's costs (`cost_sqm`: what the partner charges per square metre, `fixed`: per
order) give each order its estimated production cost and say when it costs more than it
brings in.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import home_dir
from .errors import ValidationError

SQ_M_PER_SQ_IN = 0.00064516
MAX_INLINE_BYTES = 25 * 1024 * 1024
TIMEOUT = 60.0
SENT_FILE = "partner-sent.json"


def path() -> Path:
    return home_dir() / "partner.json"


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def check_url(url: str) -> str:
    """The partner's address: https, or http to this computer (for trying it out)."""
    url = str(url or "").strip()
    parsed = urlparse(url)
    local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise ValidationError("The partner's address must start with https://")
    if parsed.scheme == "http" and not local:
        raise ValidationError("The partner's address must be https:// (http only to this computer)")
    return url


def load() -> dict[str, Any]:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {
        "name": str(data.get("name") or ""),
        "url": str(data.get("url") or ""),
        "token": str(data.get("token") or ""),
        "send_files": bool(data.get("send_files")),
        "cost_sqm": _number(data.get("cost_sqm")),
        "fixed": _number(data.get("fixed")),
    }


def configured(settings: dict[str, Any] | None = None) -> bool:
    settings = settings if settings is not None else load()
    return bool(settings["url"])


def public(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """The settings for the page: the token itself is never sent back."""
    s = settings if settings is not None else load()
    return {"configured": configured(s), "name": s["name"], "url": s["url"],
            "token_set": bool(s["token"]), "send_files": s["send_files"],
            "cost_sqm": s["cost_sqm"], "fixed": s["fixed"]}


def save(changes: dict[str, Any]) -> dict[str, Any]:
    """Update the settings; a key left out stays, a token of "" is removed."""
    current = load()
    if "url" in changes:
        url = str(changes["url"] or "").strip()
        current["url"] = check_url(url) if url else ""
    if "name" in changes:
        current["name"] = str(changes["name"] or "").strip()[:60]
    if "token" in changes:
        current["token"] = str(changes["token"] or "").strip()[:500]
    if "send_files" in changes:
        current["send_files"] = bool(changes["send_files"])
    for key in ("cost_sqm", "fixed"):
        if key in changes:
            raw = changes[key]
            value = None if raw in (None, "") else _number(raw)
            if raw not in (None, "") and value is None:
                raise ValidationError(f"{key} must be a number of 0 or more")
            current[key] = value
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    temporary = p.with_suffix(".tmp")
    temporary.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(p)
    return current


def clear() -> None:
    try:
        path().unlink()
    except FileNotFoundError:
        pass


def area_sqm(size: dict[str, Any] | None, quantity: int) -> float:
    if not size:
        return 0.0
    return float(size["width_in"]) * float(size["height_in"]) * SQ_M_PER_SQ_IN * max(1, quantity)


def estimate_cost(items: list[dict[str, Any]], settings: dict[str, Any]) -> dict[str, Any] | None:
    """The partner's charge for the order: square metres x `cost_sqm`, plus `fixed` once.
    None when the seller has entered neither."""
    per, fixed = settings.get("cost_sqm"), settings.get("fixed")
    if per is None and fixed is None:
        return None
    sqm = sum(area_sqm(i.get("size"), i.get("quantity") or 1) for i in items if i.get("plan"))
    total = sqm * (per or 0.0) + (fixed or 0.0)
    return {"sqm": round(sqm, 3), "total": round(total, 2)}


def _inline(path_: Path, enabled: bool) -> dict[str, Any]:
    if not enabled:
        return {}
    try:
        if path_.stat().st_size > MAX_INLINE_BYTES:
            return {"too_large": True}
        return {"base64": base64.b64encode(path_.read_bytes()).decode("ascii")}
    except OSError:
        return {}


def build_payload(receipt: dict[str, Any], items: list[dict[str, Any]], folder: Path,
                  settings: dict[str, Any]) -> dict[str, Any]:
    """The order as the partner is sent it."""
    inline = bool(settings.get("send_files"))
    out_items = []
    for item in items:
        if not item.get("plan") or not item.get("file"):
            continue
        plan, file = item["plan"], folder / item["file"]
        out_items.append({
            "index": item["index"],
            "title": item["title"],
            "quantity": item["quantity"],
            "paper": item.get("material") or "",
            "size": item["size"],
            "file": {"name": item["file"], "width_px": plan["file_width_px"],
                     "height_px": plan["file_height_px"], "dpi": plan["dpi"],
                     "trim_width_px": plan["width_px"], "trim_height_px": plan["height_px"],
                     "bleed_in": plan["bleed_in"],
                     **_inline(file, inline)},
        })
    invoice = folder / f"order-{receipt.get('receipt_id')}-invoice.pdf"
    return {
        "source": "etsyotomasyon",
        "order_id": receipt.get("receipt_id"),
        "ship_by": receipt.get("expected_ship_date"),
        "recipient": {
            "name": str(receipt.get("name") or ""),
            "address1": str(receipt.get("first_line") or ""),
            "address2": str(receipt.get("second_line") or ""),
            "city": str(receipt.get("city") or ""),
            "state": str(receipt.get("state") or ""),
            "zip": str(receipt.get("zip") or ""),
            "country": str(receipt.get("country_iso") or ""),
        },
        "items": out_items,
        "invoice": {"name": invoice.name, **_inline(invoice, inline)} if invoice.is_file() else None,
    }


def sent_record(folder: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((folder / SENT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def send(payload: dict[str, Any], settings: dict[str, Any], folder: Path,
         transport: Any = None) -> dict[str, Any]:
    """POST the order to the partner and record the answer next to the files."""
    headers = {"Content-Type": "application/json", "User-Agent": "etsyotomasyon"}
    if settings.get("token"):
        headers["Authorization"] = f"Bearer {settings['token']}"
    try:
        with httpx.Client(timeout=TIMEOUT, transport=transport, follow_redirects=False) as client:
            response = client.post(check_url(settings["url"]), json=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise ValidationError(f"The partner could not be reached: {exc}") from exc
    record = {"at": int(time.time()), "status": response.status_code,
              "ok": 200 <= response.status_code < 300, "partner": settings.get("name") or "",
              "answer": response.text[:500]}
    if record["ok"]:
        (folder / SENT_FILE).write_text(json.dumps(record, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    return record
