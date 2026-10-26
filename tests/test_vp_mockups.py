"""Mockuplar and Şablon İlan as the video shows them: the strings the pages ask for exist,
the product and category words the server names have Turkish and English strings, the
styles follow MockuplarEkrani.tsx / SablonEkrani.tsx, and the listings carry units sold.

The pages themselves are checked by hand in a browser; here their sources are read.
"""

from __future__ import annotations

import json
import re
import unicodedata

import pytest
import test_web_template
from test_web_template import connected
from web_helpers import ETSY_SHOP_ID

from etsyotomasyon.web.api import profit as profit_api
from etsyotomasyon.web.api import template as template_api
from etsyotomasyon.web.server import STATIC_DIR

I18N_DIR = STATIC_DIR / "i18n"
PAGES_DIR = STATIC_DIR / "js" / "pages"

# t("key") and ctx.t("key"), but not get("/api/...").
_LITERAL_KEY = re.compile(r"""(?<![\w$.])(?:ctx\.)?t\(\s*"([a-z][a-z0-9_.:]*)"\s*[,)]""")


def _strings(name: str) -> dict:
    return json.loads((I18N_DIR / f"{name}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("page", ["mockups", "template"])
def test_every_literal_key_the_page_asks_for_exists(page):
    source = (PAGES_DIR / f"{page}.js").read_text(encoding="utf-8")
    keys = set(_LITERAL_KEY.findall(source))
    assert keys, "no t(...) calls found: the pattern is out of date"
    own = _strings(page)
    common = _strings("common")
    for lang in ("tr", "en"):
        known = set(own[lang]) | set(common[lang])
        missing = sorted(k for k in keys if k not in known)
        assert not missing, f"{page}.js asks for {missing} ({lang})"


def test_the_new_strings_say_what_the_video_says():
    mockups = _strings("mockups")["tr"]
    assert mockups["library.new"] == "Yeni mockup"
    assert mockups["grid_new.sub"] == "Sürükleyip bırakın · JPG, PNG"
    assert mockups["grid_new.hint"].replace("{size}", "2000×2000") == "Önerilen: en az 2000×2000 piksel"
    assert mockups["editor.drawing"] == "Çiziliyor"
    assert mockups["editor.draw_hint"] == "Sürükleyerek baskı alanını çizin"
    assert mockups["library.same_size"] == "aynı ölçü"
    template = _strings("template")["tr"]
    assert template["footnote"] == "Yeni ilanlar bu ayarlarla oluşturulur."
    assert template["source_none"] == "Soldan bir ilan seçin"
    assert template["search"] == "İlan ara…"


def test_every_product_key_has_words_in_both_languages():
    strings = _strings("template")
    for key, _pattern in template_api._PRODUCT_WORDS:
        for lang in ("tr", "en"):
            assert strings[lang].get(f"product.{key}"), f"product.{key} ({lang})"
    # The same words as the Mockuplar page's types.
    types = _strings("mockups")
    for key, _pattern in template_api._PRODUCT_WORDS:
        for lang in ("tr", "en"):
            assert strings[lang][f"product.{key}"] == types[lang][f"type.{key}"]


@pytest.mark.parametrize(
    ("path", "key"),
    [
        (["Clothing", "Tops & Tees", "T-shirts"], "tshirt"),
        (["Clothing", "Hoodies & Sweatshirts", "Hoodies"], "hoodie"),
        (["Clothing", "Hoodies & Sweatshirts", "Sweatshirts"], "sweatshirt"),
        (["Home & Living", "Drinkware", "Mugs"], "mug"),
        (["Home & Living", "Drinkware", "Travel Mugs"], "mug"),
        (["Art & Collectibles", "Prints", "Posters"], "poster"),
        (["Art & Collectibles", "Prints", "Digital Prints"], "poster"),
        (["Art & Collectibles", "Prints", "Giclee"], "poster"),  # under Prints
        (["Art & Collectibles", "Painting", "Canvas"], "canvas"),
        (["Electronics & Accessories", "Phone Accessories", "Phone Cases"], "phone_case"),
        (["Bags & Purses", "Totes"], "tote"),
        (["Home & Living", "Home Decor", "Pillows"], "pillow"),
        (["Paper & Party Supplies", "Stickers, Labels & Tags", "Stickers"], "sticker"),
        (["Jewelry", "Rings"], None),
        (["Prints"], "poster"),
        ([], None),
    ],
)
def test_product_key_names_the_product(path, key):
    assert template_api._product_key(path) == key


# ---------------------------------------------------------------- font weights

CSS_DIR = STATIC_DIR / "css" / "pages"


def _css(name: str) -> str:
    return (CSS_DIR / f"{name}.css").read_text(encoding="utf-8")


def _weight(css: str, selector: str) -> str:
    """The font-weight of the first rule written for exactly this selector."""
    match = re.search(r"(?:^|\n)" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert match, f"no rule for {selector}"
    weight = re.search(r"font-weight:\s*(\d+)", match.group(1))
    assert weight, f"{selector} sets no font-weight"
    return weight.group(1)


@pytest.mark.parametrize("page", ["mockups", "template"])
def test_the_pages_use_only_the_weights_the_video_uses(page):
    # 550 and 650 exist nowhere in the video; with its static fonts they snap one step.
    weights = set(re.findall(r"font-weight:\s*(\d+)", _css(page)))
    assert weights <= {"400", "500", "600", "700"}, sorted(weights)


@pytest.mark.parametrize(
    ("page", "selector", "weight"),
    [
        # MockuplarEkrani.tsx / primitives.tsx
        ("mockups", ".page-mockups .mk-field-input .input", "400"),  # Input value: no weight
        ("mockups", ".page-mockups .mk-field .field-label", "600"),  # Input label
        ("mockups", ".page-mockups .mk-zoom", "400"),  # "%100": mono, no weight
        ("mockups", ".page-mockups .mk-lib-title", "700"),  # "Mockup kütüphanesi"
        ("mockups", ".page-mockups .mk-side-title", "700"),  # editor title
        ("mockups", ".page-mockups .mk-lib-applied", "600"),  # "uygulandı"
        ("mockups", ".page-mockups .mk-lib-new-sub", "400"),  # "· JPG, PNG"
        ("mockups", ".page-mockups .mk-applied-title", "600"),  # "Baskı alanı N mockup'a uygulandı"
        ("mockups", ".page-mockups .mk-state", "500"),  # "baskı alanı ayarlı"
        ("mockups", ".page-mockups .mk-toolbar .chip-count", "400"),  # inactive chip count
        ("mockups", ".page-mockups .mk-toolbar .chip.is-active .chip-count", "600"),
        # SablonEkrani.tsx
        ("template", ".tpl-state", "400"),  # "Aktif"
        ("template", ".tpl-more-btn", "600"),  # "N ilan daha göster"
        ("template", ".tpl-value.mono", "600"),  # copied price
        ("template", ".tpl-bottom .note-text b", "600"),  # "Bunlar ticari kararlar."
    ],
)
def test_the_weights_match_the_video(page, selector, weight):
    assert _weight(_css(page), selector) == weight


# ---------------------------------------------------------------- the parity items


def _rule(css: str, selector: str) -> str:
    """The body of the first rule written for exactly this selector."""
    match = re.search(r"(?:^|\n)" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert match, f"no rule for {selector}"
    return match.group(1)


def _prop(css: str, selector: str, prop: str) -> str:
    found = re.search(r"(?:^|[;\s])" + re.escape(prop) + r":\s*([^;]+);", _rule(css, selector))
    assert found, f"{selector} sets no {prop}"
    return found.group(1).strip()


def _js(name: str) -> str:
    return (PAGES_DIR / f"{name}.js").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("selector", "prop", "value"),
    [
        # MK-05: Kaydet dimmed until a never-set area is drawn (and while a drag is on).
        (".page-mockups .mk-main-foot .btn.is-unset", "opacity", "0.55"),
        # MK-06: the note is 13px, its burst not faded.
        (".page-mockups .mk-autonote", "font-size", "13px"),
        # MK-08: the size, the state and the library count are borderless pills.
        (".page-mockups .mk-size-chip", "border-radius", "999px"),
        (".page-mockups .mk-size-chip", "border", "0"),
        (".page-mockups .mk-size-chip", "background", "rgba(255, 255, 255, 0.06)"),
        (".page-mockups .mk-size-chip", "font-weight", "400"),
        (".page-mockups .mk-side-chips .badge", "height", "21px"),
        (".page-mockups .mk-lib-count", "border", "0"),
        (".page-mockups .mk-lib-count", "background", "rgba(255, 255, 255, 0.06)"),
        # MK-09: applied rows have a mint wash and no outline.
        (".page-mockups .mk-lib-item.is-applied", "border-color", "transparent"),
        (".page-mockups .mk-lib-item.is-applied", "background", "rgba(47, 214, 163, 0.06)"),
        # MK-10: a raised dark notice with a mint border and ring, kept inside the column.
        (".page-mockups .mk-applied", "background", "var(--panel-3)"),
        (".page-mockups .mk-applied", "border", "1px solid rgba(47, 214, 163, 0.4)"),
        (".page-mockups .mk-applied", "box-shadow", "0 0 0 4px rgba(47, 214, 163, 0.07)"),
        (".page-mockups .mk-applied", "margin", "0 4px 4px"),
        # MK-11: the label is a pill, shown only where it fits; the resting fill is faint.
        (".page-mockups .mk-rect-label", "border-radius", "999px"),
        (".page-mockups .mk-rect:not(.has-label) .mk-rect-label", "display", "none"),
        (".page-mockups .mk-rect", "background", "rgba(123, 108, 255, 0.04)"),
        # MK-12: the library's new-mockup outline, the switch's ring, the ghost Vazgeç.
        (".page-mockups .mk-lib-new", "border", "2px dashed var(--border)"),
        (".page-mockups .mk-lib-new", "height", "49px"),
        (".page-mockups .mk-same .toggle.is-on .toggle-track", "box-shadow", "0 0 0 4px rgba(123, 108, 255, 0.12)"),
        (".page-mockups .mk-same .toggle.is-on .toggle-thumb", "transform", "translateX(16px)"),
        (".page-mockups .mk-main-foot .btn-secondary", "color", "var(--text-2)"),
        (".page-mockups .mk-main-foot .btn-secondary", "background", "transparent"),
        # MK-13: the one accent among the extra controls is muted.
        (".page-mockups .mk-select-start .icon", "color", "var(--muted)"),
        # The video's display face on its display titles.
        (".page-mockups .mk-lib-title", "font-family", "var(--font-display)"),
        (".page-mockups .mk-side-title", "font-family", "var(--font-display)"),
        (".page-mockups .mk-grid-new-title", "font-family", "var(--font-display)"),
    ],
)
def test_mockups_css_matches_the_video(selector, prop, value):
    assert _prop(_css("mockups"), selector, prop) == value


def test_mockups_css_drops_what_the_video_does_not_have():
    css = _css("mockups")
    assert "opacity" not in _rule(css, ".page-mockups .mk-autonote .icon")
    # The label is no longer hidden for the whole draw, only while it does not fit.
    assert ".mk-art.drag-draw .mk-rect-label" not in css


def test_mockups_page_uses_the_video_icon_and_states():
    source = _js("mockups")
    assert 'h("p", { class: "mk-autonote" }, icon("sparkle", { size: 15 })' in source
    # MK-05: a class of its own, so the drag's is-waiting is left alone.
    assert 'saveBtn.classList.toggle("is-unset", unset)' in source
    assert "const unset = isGhost();" in source
    # MK-11: measured in CSS px on screen, also after a zoom or a resize.
    assert 'rect.classList.toggle("has-label", w >= LABEL_MIN_W && hh >= LABEL_MIN_H)' in source
    assert source.count("fitLabel()") >= 3  # defined, after renderRect, after layout


@pytest.mark.parametrize(
    ("selector", "prop", "value"),
    [
        # TP-04: the info note in the video's sky tone; the code chips' hairline.
        (".tpl-bottom .note", "padding", "14px 16px"),
        (".tpl-bottom .note", "font-size", "13.5px"),
        (".tpl-bottom .note.tone-info", "background", "var(--info-soft)"),
        (".tpl-bottom .note.tone-info", "border-color", "rgba(90, 184, 255, 0.28)"),
        (".tpl-bottom .note.tone-info .note-icon", "color", "var(--info)"),
        (".tpl-bottom .note.tone-info .note-icon .icon", "width", "19px"),
        (".tpl-code", "background", "rgba(255, 255, 255, 0.05)"),
        (".tpl-code", "border", "1px solid var(--border-soft)"),
        # TP-05: the grey bars fade out under the values.
        (".tpl-bar-out", "position", "absolute"),
        (".tpl-fields-list:not(.is-reveal) .tpl-bar-out", "display", "none"),
        (".page-template .tpl-card .card-title", "font-family", "var(--font-display)"),
    ],
)
def test_template_css_matches_the_video(selector, prop, value):
    assert _prop(_css("template"), selector, prop) == value


def test_template_page_drops_the_dot_and_keeps_the_cue():
    css = _css("template")
    source = _js("template")
    assert "tpl-current-dot" not in css and "tpl-current-dot" not in source
    assert "#6ea8ff" not in css and "64, 132, 238" not in css
    # The saved template is still named for a screen reader and in the tooltip.
    assert 'current ? h("span", { class: "sr-only" }, `${t("current")}: `) : null' in source
    # TP-05: the bar is only put in while the values are revealed.
    assert 'reveal ? h("span", { class: "tpl-bar tpl-bar-out", "aria-hidden": "true" }) : null' in source


def test_template_list_fits_whole_rows():
    source = _js("template")
    # TP-03: before "daha göster" only whole rows; the rows are re-rendered only when the
    # number that fits changes, and the link counts everything not shown.
    assert "return st.expanded ? st.limit : st.fit || FIRST_PAGE;" in source
    assert "if (fitted !== null && fitted !== st.fit) {" in source
    assert "const left = rows.length - count;" in source
    assert "listSizer.observe(listEl)" in source


# ---------------------------------------------------------------- TP-02: category words


def _cat_slug(name: str) -> str:
    """template.js catSlug: lower case, accents off, runs of other characters to _."""
    text = unicodedata.normalize("NFKD", name.lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def test_category_keys_are_etsys_names():
    strings = _strings("template")
    for prefix in ("category_root.", "category_leaf."):
        keys = [k for k in strings["en"] if k.startswith(prefix)]
        assert keys, prefix
        for key in keys:
            # The English value is Etsy's own name, and it slugs back to its key.
            assert _cat_slug(strings["en"][key]) == key[len(prefix):], key
            assert strings["tr"][key], key
    en_roots = {v for k, v in strings["en"].items() if k.startswith("category_root.")}
    assert {"Clothing", "Home & Living", "Art & Collectibles", "Bags & Purses",
            "Electronics & Accessories", "Accessories"} <= en_roots


def test_category_words_are_the_videos():
    tr = _strings("template")["tr"]
    # SablonEkrani.tsx CATEGORY: root › leaf, in Turkish.
    assert f"{tr['category_root.clothing']} › {tr['category_leaf.t_shirts']}" == "Giyim › Tişörtler"
    assert f"{tr['category_root.home_living']} › {tr['category_leaf.mugs']}" == "Ev ve Yaşam › Kupalar"
    assert f"{tr['category_root.art_collectibles']} › {tr['category_leaf.posters']}" == "Sanat › Posterler"
    assert f"{tr['category_root.bags_purses']} › {tr['category_leaf.tote_bags']}" == "Çantalar › Bez Çantalar"
    assert _cat_slug("Books, Movies & Music") == "books_movies_music"
    assert _cat_slug("T-shirts") == "t_shirts"


def test_the_page_shows_root_and_leaf_not_the_last_two():
    source = _js("template")
    assert "v.path.slice(-2)" not in source
    assert 'return { text: categoryText(v.path), title: v.path.join(" › ") };' in source
    # A missing word keeps both names in Etsy's English, never a mix.
    assert "if (t.has(rootKey) && t.has(leafKey)) return `${t(rootKey)} › ${t(leafKey)}`;" in source
    assert "return `${root} › ${leaf}`;" in source


def test_sales_strings():
    strings = _strings("template")
    assert strings["tr"]["sales"] == "{count} satış"
    assert strings["en"]["sales"] == "{count} sold"
    for lang in ("tr", "en"):
        for key in ("sales_one", "sales_hint", "sales_hint_one"):
            assert strings[lang][key], (lang, key)
        assert "{range}" in strings[lang]["sales_hint"] and "{n}" in strings[lang]["sales_hint"]


# ---------------------------------------------------------------- TP-06: units sold


def test_units_sold_counts_listings_only():
    products = {
        "l1000001": {"listing_id": 1000001, "title": "Shirt", "qty": 3},
        "l1000002": {"listing_id": 1000002, "title": "Mug", "qty": 1},
        "tabc": {"listing_id": None, "title": "Gone", "qty": 4},  # no listing any more
        "bad": {"listing_id": 1000003, "qty": "x"},
        "zero": {"listing_id": 1000004, "qty": 0},
        "odd": ["not", "a", "dict"],
    }
    assert template_api._units_sold(products) == {1000001: 3, 1000002: 1}
    assert template_api._units_sold(None) == {}
    assert template_api._units_sold([]) == {}


def _month_file(shop_key: str, ym: str, products: dict) -> None:
    profit_api.save_raw(shop_key, {
        "v": profit_api.RAW_VERSION,
        "month": ym,
        "fetched_at": 1.0,
        "currency": "USD",
        "orders": 1,
        "products": products,
    })


def test_listings_carry_units_sold_from_the_profit_months(web):
    fake = connected(web)
    shop_key = str(ETSY_SHOP_ID)
    now = profit_api.current_month()
    earlier = profit_api.shift_month(now, -2)
    too_old = profit_api.shift_month(now, -template_api.SALES_MONTHS)
    _month_file(shop_key, now, {"l1000002": {"listing_id": 1000002, "qty": 3}})
    _month_file(shop_key, earlier, {
        "l1000002": {"listing_id": 1000002, "qty": 2},
        "l1000001": {"listing_id": 1000001, "qty": 1},
    })
    _month_file(shop_key, too_old, {"l1000001": {"listing_id": 1000001, "qty": 50}})
    _month_file("99999999", now, {"l1000001": {"listing_id": 1000001, "qty": 80}})  # another shop

    data = web.client.get("/api/template/listings").json()
    assert data["sales"] == {"months": [earlier, now]}
    # Best sellers first: the mug (5) before the shirt (1), whatever Etsy's order.
    assert [(it["listing_id"], it["sold"]) for it in data["items"]] == [(1000002, 5), (1000001, 1)]
    # Read from disk only: no receipts are asked for.
    assert not any("/receipts" in path for _method, path in fake.calls)


def test_a_listing_without_sales_counts_zero_and_keeps_its_place(web):
    records = [
        test_web_template.listing(1000001, "First"),
        test_web_template.listing(1000002, "Second"),
        test_web_template.listing(1000003, "Third"),
    ]
    connected(web, records)
    _month_file(str(ETSY_SHOP_ID), profit_api.current_month(), {"l1000003": {"listing_id": 1000003, "qty": 2}})
    items = web.client.get("/api/template/listings").json()["items"]
    assert [(it["listing_id"], it["sold"]) for it in items] == [(1000003, 2), (1000001, 0), (1000002, 0)]


def test_a_changed_month_file_is_read_again(web):
    connected(web)
    shop_key = str(ETSY_SHOP_ID)
    now = profit_api.current_month()
    _month_file(shop_key, now, {"l1000001": {"listing_id": 1000001, "qty": 1}})
    first = web.client.get("/api/template/listings").json()["items"]
    assert {it["listing_id"]: it["sold"] for it in first}[1000001] == 1
    _month_file(shop_key, now, {"l1000001": {"listing_id": 1000001, "qty": 4, "title": "longer file"}})
    again = web.client.get("/api/template/listings").json()["items"]
    assert {it["listing_id"]: it["sold"] for it in again}[1000001] == 4
