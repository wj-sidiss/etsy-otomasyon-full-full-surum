import time

import pytest

from etsyotomasyon.csvio import read_rows, split_multi, write_rows
from etsyotomasyon.errors import ValidationError
from etsyotomasyon.orders import flatten_receipt, money, parse_since


def test_parse_since_relative():
    now = time.time()
    assert now - parse_since("30d") == pytest.approx(30 * 86400, abs=120)
    assert now - parse_since("2w") == pytest.approx(14 * 86400, abs=120)


def test_parse_since_iso_date():
    assert parse_since("2026-01-01") == 1767225600


def test_parse_since_rejects_nonsense():
    with pytest.raises(ValidationError, match="Could not read"):
        parse_since("last tuesday")


def test_money_uses_the_divisor():
    assert money({"amount": 2450, "divisor": 100, "currency_code": "USD"}) == ("24.50", "USD")


def test_money_handles_missing_values():
    assert money(None) == ("", "")


def test_flatten_receipt_collapses_line_items():
    row = flatten_receipt(
        {
            "receipt_id": 42,
            "created_timestamp": 1767225600,
            "name": "Ada Lovelace",
            "city": "London",
            "country_iso": "GB",
            "grandtotal": {"amount": 4900, "divisor": 100, "currency_code": "GBP"},
            "transactions": [
                {"title": "Ceramic Mug", "quantity": 2, "sku": "MUG-01"},
                {"title": "Saucer", "quantity": 1},
            ],
            "shipments": [{"tracking_code": "AB123"}],
        }
    )
    assert row["receipt_id"] == 42
    assert row["items"] == ["Ceramic Mug x2", "Saucer x1"]
    assert row["skus"] == ["MUG-01"]
    assert row["item_count"] == 3
    assert row["order_total"] == "49.00"
    assert row["tracking_codes"] == ["AB123"]


def test_split_multi_splits_on_pipes():
    assert split_multi("a|b|c") == ["a", "b", "c"]
    assert split_multi("") == []


def test_a_comma_only_separates_where_a_value_cannot_contain_one():
    # Tags opt in — nobody writes a tag with a comma, and sellers type them that way.
    assert split_multi("a, b", allow_comma=True) == ["a", "b"]
    # Paths do not. A single-image cell has no pipe, so a comma fallback would tear
    # "2-DRAFTS/kedi, kopek tablosu--flat.jpg" into two files that do not exist.
    assert split_multi("kedi, kopek tablosu--flat.jpg") == ["kedi, kopek tablosu--flat.jpg"]


def test_csv_round_trip_preserves_unicode_and_lists(tmp_path):
    path = tmp_path / "out.csv"
    write_rows(path, [{"title": "Çiçek düğme", "tags": ["a", "b"]}], columns=["title", "tags"])
    rows = read_rows(path)
    assert rows[0]["title"] == "Çiçek düğme"
    assert split_multi(rows[0]["tags"]) == ["a", "b"]


def test_read_rows_skips_blank_trailing_rows(tmp_path):
    path = tmp_path / "in.csv"
    path.write_text("title,price\nMug,10\n,\n", encoding="utf-8-sig")
    assert len(read_rows(path)) == 1
