"""İlanlar and SEO as the video shows them: every string the two pages ask for exists.

A key the page asks for but no string file has renders as the raw key ("filter.data")
on screen, in both languages. The pages look a key up in their own file first and then
in common.json (i18n.js), so each literal key must be in one of the two.
"""

from __future__ import annotations

import csv
import io
import json
import re

import pytest

from etsyotomasyon import csvio, listings
from etsyotomasyon.errors import ValidationError
from etsyotomasyon.web.api import listings as listings_api
from etsyotomasyon.web.server import STATIC_DIR

PAGES = ("listings", "seo")
# t("key") and t(cond ? "a" : "b"): the literal keys; template keys (t(`tab.${x}`)) are
# built at run time and are left out.
_CALL = re.compile(r"""\bt\(\s*"([^"]+)"\s*[,)]""")
_TERNARY = re.compile(r"""\bt\(\s*[^()"`]*?\?\s*"([^"]+)"\s*:\s*"([^"]+)"\s*[,)]""")


def _strings(name: str) -> dict:
    return json.loads((STATIC_DIR / "i18n" / f"{name}.json").read_text(encoding="utf-8"))


def _keys_used(page: str) -> set[str]:
    source = (STATIC_DIR / "js" / "pages" / f"{page}.js").read_text(encoding="utf-8")
    keys = set(_CALL.findall(source))
    for pair in _TERNARY.findall(source):
        keys.update(pair)
    return keys


@pytest.mark.parametrize("page", PAGES)
def test_every_key_the_page_asks_for_has_words_in_both_languages(page):
    own = _strings(page)
    common = _strings("common")
    used = _keys_used(page)
    assert used, page
    for lang in ("tr", "en"):
        missing = sorted(k for k in used if k not in own[lang] and k not in common[lang])
        assert not missing, f"{page}.js ({lang}): {missing}"


def test_the_listings_page_uses_the_videos_wording():
    strings = _strings("listings")
    for lang in ("tr", "en"):
        # an ellipsis character, not three dots ("Başlık veya etiket ara…")
        assert strings[lang]["search.placeholder"].endswith("…"), lang
        assert "..." not in strings[lang]["search.placeholder"], lang
        # the CSV actions moved under Filtrele, with a heading of their own
        assert strings[lang]["filter.data"], lang
        assert "more.title" not in strings[lang], lang
    assert "filter.data" in _keys_used("listings")


def test_the_seo_page_keeps_its_quiet_notes_and_the_explanation_line_is_the_sentence():
    strings = _strings("seo")
    used = _keys_used("seo")
    for lang in ("tr", "en"):
        assert strings[lang]["ready_minor"] != strings[lang]["no_issues"], lang
        # the video's line under the keyword is the sentence alone: no "+10 etiket"
        assert not any(k.startswith("research.more") for k in strings[lang]), lang
        # the cached answer's button moved into the keyword field; its words stay
        assert strings[lang]["research.cached"] and strings[lang]["research.refresh"], lang
    assert {"ready_minor", "no_issues", "research.cached", "research.refresh"} <= used


def _source(page: str) -> str:
    return (STATIC_DIR / "js" / "pages" / f"{page}.js").read_text(encoding="utf-8")


def test_the_seo_header_is_only_the_rescan_button():
    source = _source("seo")
    # SeoEkrani.tsx: the header's actions are "Yeniden tara" alone; the Aktif / Taslaklar
    # switch, the list search, the shop-wide notes and the CSV report are in a popover.
    assert "ctx.setHeader({ actions: [rescanBtn] })" in source
    assert 'h("div", { class: "seo-head-tools" }, legend, toolsBtn)' in source
    strings = _strings("seo")
    for lang in ("tr", "en"):
        # a draft audit says so, because the switch is out of sight
        assert strings[lang]["audit.sub_draft"] != strings[lang]["audit.sub"], lang
        assert "{n}" in strings[lang]["tools.title_issues"], lang
    assert {"audit.sub_draft", "audit.sub_ready_draft", "tools.title", "tools.title_issues",
            "search.active"} <= _keys_used("seo")


def test_the_fix_dialog_ticks_nothing_the_seller_did_not_pick():
    source = _source("seo")
    # a title change is offered unticked
    assert "let titleOn = false;" in source
    # no loop fills the free tag slots with competitor tags
    assert "if (!a.checked && !has(a.tag)) a.checked = true;" not in source
    assert "fix.pick_hint" in _keys_used("seo")


def test_import_notes_about_weights_and_sizes_have_words_in_both_languages():
    strings = _strings("listings")
    codes = {
        "warn": ("weight_zero", "size_zero", "weight_no_unit", "size_no_unit"),
        "err": ("nothing_to_update", "comma", "comma_decimal", "not_a_number",
                "measure_not_a_number", "weight_unit", "size_unit"),
    }
    for lang in ("tr", "en"):
        for group, names in codes.items():
            for name in names:
                assert strings[lang][f"import.{group}.{name}"], (lang, group, name)
    for lang in ("tr", "en"):
        assert "{field}" in strings[lang]["import.warn.size_zero"]
        assert "{fields}" in strings[lang]["import.warn.size_no_unit"]
        assert "{dot}" in strings[lang]["import.err.comma_decimal"]
    source = _source("listings")
    assert "`import.${group}.${note.code}`" in source
    # a skipped publish reads "zaten yayında", not the server's English "already active"
    assert 'if (panel.kind === "publish" && item.status === "skipped") return t("result.skipped");' in source


# --- the weight and size notes of a CSV check, in the seller's language --------------------


def _csv(rows) -> bytes:
    out = io.StringIO()
    columns = list(dict.fromkeys(key for row in rows for key in row))
    writer = csv.DictWriter(out, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode("utf-8-sig")


def test_a_csv_check_gives_weight_and_size_notes_a_code(web):
    rows = [
        {"listing_id": "1000001", "item_weight": "0", "item_weight_unit": "g", "item_length": "",
         "item_width": "", "item_dimensions_unit": ""},
        {"listing_id": "1000002", "item_weight": "250"},
        {"listing_id": "1000003", "item_length": "30", "item_width": "20"},
        {"listing_id": "1000004", "item_weight": "250g", "item_weight_unit": "g"},
        {"listing_id": "1000005", "item_weight": "250", "item_weight_unit": "gram"},
    ]
    check = web.client.post("/api/listings/import", content=_csv(rows)).json()
    by_row = {r["listing_id"]: r for r in check["results"]}

    zero = by_row[1000001]
    assert [w["code"] for w in zero["warnings"]] == ["weight_zero"]
    assert "item_weight 0 not sent" in zero["warnings"][0]["text"]  # the CLI's words stay
    # nothing else was in the row: the refusal is coded too
    assert [p["code"] for p in zero["problems"]] == ["nothing_to_update"]

    no_unit = by_row[1000002]
    assert [w["code"] for w in no_unit["warnings"]] == ["weight_no_unit"]

    sizes = by_row[1000003]
    assert sizes["warnings"] == [{
        "text": "item_length, item_width not sent: item_dimensions_unit is empty",
        "code": "size_no_unit", "params": {"fields": "item_length, item_width"},
    }]

    glued = by_row[1000004]
    assert glued["problems"][0]["code"] == "measure_not_a_number"
    assert glued["problems"][0]["params"] == {"field": "item_weight", "value": "250g",
                                              "unit_field": "item_weight_unit"}

    unit = by_row[1000005]
    assert unit["problems"][0]["code"] == "weight_unit"
    assert unit["problems"][0]["params"]["units"] == "g, kg, lb, oz"


def test_every_measure_note_the_library_writes_has_a_code():
    """The codes are read from etsyotomasyon.listings' English notes: a reworded note would
    fall back to English on the page, and this test says so first."""
    base = {"title": "Mug", "description": "A mug.", "price": "10", "quantity": "1",
            "who_made": "i_did", "when_made": "made_to_order", "taxonomy_id": "1",
            "shipping_profile_id": "1"}
    for extra in (
        {"item_weight": "0", "item_weight_unit": "g"},
        {"item_height": "0", "item_dimensions_unit": "cm"},
        {"item_weight": "12"},
        {"item_height": "2"},
    ):
        notes: list[str] = []
        listings.build_payload({**base, **extra}, is_update=False, warnings=notes)
        measure_notes = [n for n in notes if n.startswith("item_")]
        assert measure_notes, extra
        for note in measure_notes:
            assert listings_api._coded(note, listings_api._NOTE_CODES)["code"], note


def test_an_ambiguous_comma_gets_a_hint_that_keeps_the_decimal():
    with pytest.raises(ValidationError) as caught:
        csvio.as_float("1,250", "item_weight")
    coded = listings_api._coded(str(caught.value), listings_api._PROBLEM_CODES)
    assert coded["code"] == "comma"
    assert coded["params"] == {"field": "item_weight", "value": "1,250", "dot": "1.250",
                               "plain": "1250"}
    # "0,250" is a decimal (no thousands separator follows a lone 0): never "0250".
    try:
        assert csvio.as_float("0,250", "item_weight") == 0.25
    except ValidationError as exc:
        coded = listings_api._coded(str(exc), listings_api._PROBLEM_CODES)
        assert coded["code"] == "comma_decimal" and coded["params"]["dot"] == "0.250"
