"""Output you can screenshot without exposing your shop.

CLI output gets shared far more often than anyone plans for, and a listing title is
enough to find the shop it belongs to on Etsy. `--anonymise` keeps the findings
readable and drops the identity.

Every value below is invented. Test data must never carry a real shop's name, ids or
vocabulary — a public test file is a fingerprint, and this file of all files should
not be one.
"""

import pytest

from etsyotomasyon import cli

SHOP = "ExampleShopName"
SHOP_ID = 11111111
LISTING_ID = 2222222222
USER_ID = 3333333333
TITLE = "Soy Wax Candle Gift Set | Hand Poured Lavender Scented"


@pytest.fixture
def anonymised(monkeypatch):
    monkeypatch.setattr(cli, "ANONYMISE", True)


@pytest.fixture
def plain(monkeypatch):
    monkeypatch.setattr(cli, "ANONYMISE", False)


def test_nothing_is_hidden_by_default(plain):
    assert cli._hide(SHOP, "shop") == SHOP
    assert cli._hide(SHOP_ID, "id") == str(SHOP_ID)
    assert cli._hide(TITLE) == TITLE


def test_a_shop_name_is_replaced(anonymised):
    hidden = cli._hide(SHOP, "shop")
    assert SHOP not in hidden
    assert hidden == "‹your shop›"


def test_placeholders_avoid_rich_markup_syntax(anonymised):
    # "[hidden]" is markup to Rich, which prints nothing at all — a redaction that
    # looks like a missing value instead of a hidden one.
    for kind in ("id", "title", "shop", "url", "unknown"):
        placeholder = cli._hide("secret", kind)
        assert placeholder.strip(), "a redaction must still be visible"
        assert "[" not in placeholder and "]" not in placeholder


def test_ids_are_replaced(anonymised):
    for value in (SHOP_ID, str(LISTING_ID), USER_ID):
        assert str(value) not in cli._hide(value, "id")


def test_titles_are_replaced(anonymised):
    # A title is searchable on Etsy, so it identifies the shop as surely as its name.
    assert cli._hide(TITLE) == "‹hidden›"
    assert "Lavender" not in cli._hide(TITLE)


def test_urls_are_replaced(anonymised):
    assert "etsy.com" not in cli._hide("https://www.etsy.com/shop/Example", "url")


def test_an_unknown_kind_still_hides(anonymised):
    # Fail closed: a caller passing a category we did not anticipate must not leak.
    assert cli._hide("something identifying", "not-a-known-kind") == "‹hidden›"


def test_the_environment_variable_is_read_at_import(monkeypatch):
    import importlib

    monkeypatch.setenv("ETSYOTOMASYON_ANONYMISE", "1")
    reloaded = importlib.reload(cli)
    try:
        assert reloaded.ANONYMISE is True
    finally:
        monkeypatch.delenv("ETSYOTOMASYON_ANONYMISE", raising=False)
        importlib.reload(cli)


@pytest.mark.parametrize("value", ["", "0", "no", "off", "false"])
def test_other_environment_values_leave_it_off(monkeypatch, value):
    import importlib

    monkeypatch.setenv("ETSYOTOMASYON_ANONYMISE", value)
    reloaded = importlib.reload(cli)
    try:
        assert reloaded.ANONYMISE is False
    finally:
        monkeypatch.delenv("ETSYOTOMASYON_ANONYMISE", raising=False)
        importlib.reload(cli)


# Identifiers that must never appear in this repository, stored as truncated SHA-256
# rather than as themselves — a guard written in plaintext would reintroduce exactly
# what it forbids. Add a digest here to ban a term without ever committing the term.
BANNED_DIGESTS = frozenset({
    "57e7cce080ae7eff",
    "8588bbdb4349ae76",
    "0cabec703deebdd8",
    "914a1b4400046d37",
    "3bad5012ca93fd14",
    "3a0a50224525fb4f",
    "bd56c3a38559694a",
    "adfe4f49782e5dcd",
    "62ec42e888ec0d5a",
})


def test_no_test_file_carries_a_real_shop_identity():
    """Nothing in this suite may fingerprint whoever the tool was run against.

    A shop name, a listing id and a niche word all identify a shop just as well as
    each other. This guard exists because the rule was broken twice — once in a
    fixture, once in this very file.
    """
    import hashlib
    import re
    from pathlib import Path

    for path in Path(__file__).parent.glob("test_*.py"):
        tokens = set(re.findall(r"[A-Za-z]{4,}|\d{6,}", path.read_text(encoding="utf-8")))
        for token in tokens:
            digest = hashlib.sha256(token.lower().encode()).hexdigest()[:16]
            assert digest not in BANNED_DIGESTS, (
                f"{path.name} contains a banned identifier (token of length {len(token)})"
            )
