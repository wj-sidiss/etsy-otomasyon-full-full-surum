"""The order's invoice sheet, with the product's picture on it.

What the printer is sent with an order: the sheet Etsy prints for it (ship-to, order
number, items, totals), with the picture of what was sold at the bottom right, at the
proportions of the size ordered. Etsy gives an API client no such PDF, so it is made here
from the order itself; nothing is sent anywhere.

A small PDF is written by hand: the standard Helvetica fonts (no embedding, so no font
files to find on any platform), JPEG pictures stored as they are, one page.
"""

from __future__ import annotations

import io
import re
import unicodedata
import zlib
from datetime import datetime, timezone
from typing import Any

from PIL import Image

PAGE_W, PAGE_H = 612.0, 792.0  # US Letter, in points
MARGIN = 36.0
LEFT_W = 134.0  # the ship-to column
RIGHT_X = 170.0
RIGHT_END = PAGE_W - MARGIN
PICTURE_BOX = (150.0, 200.0)  # one picture's largest size, in points
PICTURE_BOTTOM = 687.0  # a picture's bottom edge, measured from the top of the page
FOOTER_Y = 689.0
CURRENCY = {"USD": "US$", "EUR": "€", "GBP": "£", "CAD": "CA$", "AUD": "A$"}

# Helvetica's glyph widths (1000 units per em) for ASCII 32..126.
_W = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
      556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
      1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
      667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
      333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
      556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584]


def ascii_safe(text: str) -> str:
    """`text` in what Helvetica's WinAnsi encoding holds: others become their base letter."""
    out = []
    for ch in str(text):
        try:
            ch.encode("cp1252")
            out.append(ch)
        except UnicodeEncodeError:
            base = unicodedata.normalize("NFKD", ch.replace("ı", "i").replace("İ", "I"))
            out.append("".join(c for c in base if ord(c) < 128) or "?")
    return "".join(out)


def text_width(text: str, size: float, bold: bool = False) -> float:
    total = sum(_W[ord(c) - 32] if 32 <= ord(c) <= 126 else 556 for c in text)
    return total * size / 1000.0 * (1.06 if bold else 1.0)


def wrap(text: str, size: float, width: float, bold: bool = False) -> list[str]:
    lines: list[str] = []
    line = ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if line and text_width(trial, size, bold) > width:
            lines.append(line)
            line = word
        else:
            line = trial
    if line:
        lines.append(line)
    return lines


def money(value: Any) -> str:
    """"US$16.00" from an Etsy money object; "" when there is none."""
    if not isinstance(value, dict) or not isinstance(value.get("amount"), (int, float)):
        return ""
    divisor = value.get("divisor") or 100
    code = str(value.get("currency_code") or "")
    sign = CURRENCY.get(code, f"{code} " if code else "")
    return f"{sign}{value['amount'] / divisor:,.2f}"


def day(timestamp: Any) -> str:
    if not isinstance(timestamp, (int, float)) or timestamp <= 0:
        return ""
    moment = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return f"{moment:%b} {moment.day}, {moment.year}"


class _Page:
    """The one page's drawing operations, written with the origin at the top left."""

    def __init__(self) -> None:
        self.ops: list[str] = []
        self.images: list[tuple[bytes, int, int]] = []

    def text(self, x: float, y: float, text: str, size: float = 9, bold: bool = False,
             gray: float = 0.0, right: bool = False) -> None:
        text = ascii_safe(text)
        if right:
            x -= text_width(text, size, bold)
        escaped = text.encode("cp1252", "replace").decode("latin-1")
        escaped = escaped.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        self.ops.append(
            f"BT {gray:.2f} g /{'F2' if bold else 'F1'} {size:g} Tf "
            f"{x:.2f} {PAGE_H - y:.2f} Td ({escaped}) Tj ET"
        )

    def rule(self, x1: float, x2: float, y: float, gray: float = 0.35) -> None:
        self.ops.append(f"{gray:.2f} G 0.6 w {x1:.2f} {PAGE_H - y:.2f} m {x2:.2f} {PAGE_H - y:.2f} l S")

    def picture(self, jpeg: bytes, size: tuple[int, int], x: float, y_top: float,
                w: float, h: float) -> None:
        self.images.append((jpeg, size[0], size[1]))
        name = f"Im{len(self.images)}"
        self.ops.append(f"q {w:.2f} 0 0 {h:.2f} {x:.2f} {PAGE_H - y_top - h:.2f} cm /{name} Do Q")


def _pdf(page: _Page, title: str) -> bytes:
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    add(b"<< /Type /Catalog /Pages 2 0 R >>")
    add(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    objects.append(b"")  # the page, filled in below
    f1 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    f2 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    xobjects = []
    for index, (jpeg, w, h) in enumerate(page.images, start=1):
        ref = add(
            f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} /ColorSpace /DeviceRGB "
            f"/BitsPerComponent 8 /Filter /DCTDecode /Length {len(jpeg)} >>\nstream\n".encode()
            + jpeg + b"\nendstream"
        )
        xobjects.append(f"/Im{index} {ref} 0 R")
    content = zlib.compress("\n".join(page.ops).encode("latin-1", "replace"))
    cref = add(
        f"<< /Filter /FlateDecode /Length {len(content)} >>\nstream\n".encode()
        + content + b"\nendstream"
    )
    objects[2] = (
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W:g} {PAGE_H:g}] "
        f"/Resources << /Font << /F1 {f1} 0 R /F2 {f2} 0 R >> "
        f"/XObject << {' '.join(xobjects)} >> >> /Contents {cref} 0 R >>"
    ).encode()
    info = add(f"<< /Title ({ascii_safe(title)}) /Producer (etsyotomasyon) >>".encode())
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info {info} 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


def jpeg(image: Image.Image, max_side: int = 1000) -> tuple[bytes, tuple[int, int]]:
    image = image.convert("RGB")
    if max(image.size) > max_side:
        image.thumbnail((max_side, max_side), Image.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90, subsampling=0)
    return buffer.getvalue(), image.size


def build(receipt: dict[str, Any], shop_name: str, pictures: dict[int, Image.Image]) -> bytes:
    """The invoice sheet of `receipt` (an Etsy receipt with its transactions) as a PDF.

    `pictures`: transaction index (1-based, as the order's items are numbered) -> the
    picture of what was sold, shown at the bottom right.
    """
    page = _Page()
    shop = str(shop_name or "").strip()
    page.text(MARGIN, 50, shop or "Order", 16, bold=True)
    if shop:
        page.text(MARGIN, 65, f"{re.sub(r'[^A-Za-z0-9]', '', shop).lower()}.etsy.com", 8.5, gray=0.25)

    # --- the left column: ship to, dates, order number
    y = 112.0

    def block(label: str, lines: list[str]) -> None:
        nonlocal y
        lines = [line for line in lines if line]
        if not lines:
            return
        page.text(MARGIN, y, label, 8.5, bold=True)
        y += 12
        for line in lines:
            page.text(MARGIN, y, line, 8.5)
            y += 11.5
        y += 10

    city = ", ".join(p for p in (str(receipt.get("city") or "").upper(),
                                 str(receipt.get("state") or "")) if p)
    country = str(receipt.get("formatted_address") or "")
    block("Ship to", [str(receipt.get("name") or ""), str(receipt.get("first_line") or ""),
                      str(receipt.get("second_line") or ""), city, str(receipt.get("zip") or ""),
                      str(receipt.get("country_iso") or "") if not country else ""])
    ship_by = day(receipt.get("expected_ship_date"))
    if ship_by:
        block("Scheduled to ship by", [ship_by])
        page.rule(MARGIN, MARGIN + LEFT_W - 10, y - 12)
    block("Order", [f"#{receipt.get('receipt_id')}"])
    block("Order date", [day(receipt.get("created_timestamp") or receipt.get("create_timestamp"))])
    if receipt.get("is_paid"):
        block("Payment method", ["Paid via Etsy Payments"])

    # --- the right column: items and totals
    transactions = [t for t in receipt.get("transactions") or [] if isinstance(t, dict)]
    count = sum(t.get("quantity") if isinstance(t.get("quantity"), int) else 1 for t in transactions)
    page.text(RIGHT_X, 112, f"{count} item{'s' if count != 1 else ''}", 9, bold=True)
    page.rule(RIGHT_X, RIGHT_END, 120)
    y = 134.0
    for txn in transactions:
        top = y
        for line in wrap(str(txn.get("title") or ""), 8.5, RIGHT_END - RIGHT_X - 120, bold=True):
            page.text(RIGHT_X, y + 8, line, 8.5, bold=True)
            y += 11.5
        y += 3
        for variation in txn.get("variations") or []:
            if isinstance(variation, dict) and variation.get("formatted_value"):
                label = f"{variation.get('formatted_name') or ''}: {variation['formatted_value']}"
                page.text(RIGHT_X, y + 8, label.strip(": "), 8.5)
                y += 11.5
        quantity = txn.get("quantity") if isinstance(txn.get("quantity"), int) else 1
        price = money(txn.get("price"))
        page.text(RIGHT_END, top + 8, f"{quantity} x {price}".strip(), 8.5, right=True)
        y = max(y, top + 16) + 10
        page.rule(RIGHT_X, RIGHT_END, y - 2, gray=0.7)
        y += 6
    totals = [("Item total", money(receipt.get("subtotal") or receipt.get("total_price"))),
              ("Tax", money(receipt.get("total_tax_cost"))),
              ("Shipping total", money(receipt.get("total_shipping_cost"))),
              ("Order total", money(receipt.get("grandtotal")))]
    for label, value in totals:
        if not value:
            continue
        bold = label == "Order total"
        page.text(RIGHT_END - 90, y + 8, label, 8.5, bold=bold, right=True)
        page.text(RIGHT_END, y + 8, value, 8.5, bold=bold, right=True)
        y += 13

    # --- the pictures of what was sold, bottom right
    shown = [(i, img) for i, img in sorted(pictures.items()) if img is not None]
    if shown:
        gap = 8.0
        avail = RIGHT_END - RIGHT_X
        box_w = min(PICTURE_BOX[0], (avail - gap * (len(shown) - 1)) / len(shown))
        x_end = RIGHT_END
        for _index, img in reversed(shown):
            ratio = img.width / img.height
            w, h = (box_w, box_w / ratio) if box_w / ratio <= PICTURE_BOX[1] else (PICTURE_BOX[1] * ratio, PICTURE_BOX[1])
            data, size = jpeg(img)
            page.picture(data, size, x_end - w, PICTURE_BOTTOM - h, w, h)
            x_end -= w + gap

    page.rule(MARGIN, RIGHT_END, FOOTER_Y)
    page.text(MARGIN, FOOTER_Y + 16, "Thank you for your order!", 8.5, bold=True)
    return _pdf(page, f"Order {receipt.get('receipt_id')}")


def picture_for(design: Image.Image, plan: Any, size: Any, *, max_box: int = 1000) -> Image.Image:
    """The picture of one item for the sheet: the item as it prints, cut to the proportions
    of the size ordered (a long roll shows its top, a tall strip no thinner than 1 : 2)."""
    from . import production

    longest = max(size.width_in, size.height_in)
    dpi = max(production.MIN_DPI // 2, min(150, int(max_box / longest)))
    small = production.plan(size, design.size, dpi=dpi, repeats=plan.repeats or None,
                            mode=plan.mode)
    image = production.render(design, small)
    ratio = max(0.5, min(2.0, size.width_in / size.height_in))
    height = min(image.height, round(image.width / ratio))
    return image.crop((0, 0, image.width, height))
