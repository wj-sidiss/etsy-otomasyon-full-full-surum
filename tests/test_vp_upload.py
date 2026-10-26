"""Tasarım Yükle and Taslak İlan as the product video shows them.

The words the video uses (the drop zone, the start card, the row lines, "Duraklat"), the
drag count the browser can honestly give, the run table's fixed pages of seven, the
"~2 dk kaldı" estimate, and the category names ("Giyim › Tişörtler"). The pages' small
helpers run under node when it is installed.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image
from test_drop_stream import SHOP, Client, _artwork, _history, studio  # noqa: F401
from test_drop_stream import _run as _drop_run
from test_web_listings import history, shop_with

STATIC = Path(__file__).resolve().parents[1] / "etsyotomasyon" / "web" / "static"
NODE = shutil.which("node")


def _strings(page: str) -> dict:
    return json.loads((STATIC / "i18n" / f"{page}.json").read_text(encoding="utf-8"))


def _run(page: str, script: str):
    url = (STATIC / "js" / "pages" / f"{page}.js").as_uri()
    code = (f"const m = await import({json.dumps(url)});\n"
            f"const out = (() => {{ {script} }})();\n"
            "console.log(JSON.stringify(out));")
    done = subprocess.run([NODE, "--input-type=module", "-e", code], capture_output=True,
                          text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


# --- the video's words -------------------------------------------------------------------------


def test_the_drop_zone_and_the_start_card_say_what_the_video_says():
    tr = _strings("designs")["tr"]
    assert tr["drop.title"] == "Tasarımlarınızı buraya sürükleyin"
    assert tr["drop.sub"] == "PNG · şeffaf zemin önerilir · tek seferde 500'e kadar"
    assert tr["drop.pick"] == "Dosya seç" and tr["drop.pick_or"] == "veya bir klasörü sürükleyin"
    assert tr["drop.over"] == "Bırakın!"
    assert tr["drop.ready_n"].format(n=50) + " " + tr["drop.ready_tail"] == "50 dosya · yüklemeye hazır"
    assert tr["ready.title"].format(n=50) == "50 tasarım hazır"
    assert tr["ready.sub"].format(mockups=6, template="Retro Mountain Sunset Shirt") == \
        "6 mockup · Şablon: Retro Mountain Sunset Shirt"
    assert tr["chip.mockup"] + " " + tr["chip.mockup_ready"].format(n=6) == "Mockup: 6 hazır"


def test_the_steps_and_the_run_read_as_in_the_video():
    tr = _strings("designs")["tr"]
    subs = [tr[f"step.{s}_sub"] for s in ("research", "title", "tags", "check", "draft")]
    assert subs == ["Etsy aramaları", "≤140 karakter", "13 × ≤20 karakter", "Kurallara uygunluk",
                    "Yayınlamadan bekler"]
    assert tr["run.stop"] == "Duraklat" and tr["run.eta_label"] == "Tahmini süre"
    assert tr["run.eta_min"].format(n=2) == "~2 dk kaldı"
    assert tr["row.research"] == "Anahtar kelimeler aranıyor…"
    assert tr["row.draft"] == "Etsy'ye gönderiliyor…"
    assert tr["toast.check"].format(n=0) == "Kontrol tamam: 0 hata · gönderilmeden önce"
    # The video's ellipsis character, never three dots.
    for lang in ("tr", "en"):
        rows = {k: v for k, v in _strings("designs")[lang].items() if k.startswith("row.")}
        assert not [k for k, v in rows.items() if "..." in v], rows


def test_waiting_to_send_is_not_called_sending():
    for lang in ("tr", "en"):
        strings = _strings("designs")[lang]
        assert strings["row.waiting"] != strings["row.draft"]
        assert strings["side.stage.waiting"] != strings["side.stage.draft"]


def test_the_draft_page_says_seo_score_out_of_100():
    ld = _strings("listing-detail")
    assert ld["tr"]["seo.score"].format(n=94) == "SEO skoru 94/100"
    assert ld["en"]["seo.score"].format(n=94) == "SEO score 94/100"


# --- the pages' helpers (node) -------------------------------------------------------------------


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_drag_count_is_given_only_when_every_item_is_an_image():
    got = _run("designs", """
      const f = (type) => ({ kind: 'file', type });
      return [
        m.dragCount([f('image/png'), f('image/jpeg'), f('image/png')]),
        m.dragCount([f('image/png'), f('')]),              // a folder is one item, no type
        m.dragCount([f('application/pdf')]),
        m.dragCount([{ kind: 'string', type: 'text/plain' }]),
        m.dragCount([]),
        m.dragCount(null),
      ];""")
    assert got == [3, None, None, None, None, None]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_running_table_turns_whole_pages_and_keeps_a_picked_row():
    got = _run("designs", """return [
      m.pageStart(50, 0, null), m.pageStart(50, 6, null), m.pageStart(50, 7, null),
      m.pageStart(50, 13, null), m.pageStart(50, 49, null), m.pageStart(50, -1, null),
      m.pageStart(50, 20, 3), m.pageStart(8, 7, null), m.pageStart(5, 3, null),
    ];""")
    assert got == [0, 0, 7, 7, 43, 43, 0, 1, 0]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_time_left_reads_in_minutes_then_in_tens_of_seconds():
    got = _run("designs", "return [m.etaLabel(125), m.etaLabel(60), m.etaLabel(59), m.etaLabel(41), m.etaLabel(3)];")
    assert got == [["run.eta_min", 2], ["run.eta_min", 1], ["run.eta_sec", 60], ["run.eta_sec", 50],
                   ["run.eta_sec", 10]]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_every_category_name_is_found_by_etsys_own_english_name():
    en = _strings("listing-detail")["en"]
    cats = {k: v for k, v in en.items() if k.startswith("cat.")}
    assert "cat.clothing" in cats and "cat.t_shirts" in cats
    names = json.dumps(list(cats.values()))
    slugs = _run("listing-detail", f"return {names}.map((n) => 'cat.' + m.catSlug(n));")
    assert slugs == list(cats), [(k, s) for k, s in zip(cats, slugs) if k != s]
    tr = _strings("listing-detail")["tr"]
    assert f"{tr['cat.clothing']} › {tr['cat.t_shirts']}" == "Giyim › Tişörtler"


# --- the run's words and the draft's address (round 2) -------------------------------------------


def test_a_weight_etsy_was_not_sent_reads_in_the_ui_language():
    for lang in ("tr", "en"):
        text = _strings("designs")[lang]["warn.measure_not_sent"]
        assert text and "item_" not in text
    assert _strings("designs")["tr"]["warn.measure_not_sent"] == \
        "Ağırlık/ölçü Etsy'ye gönderilmedi (0 ya da birimsiz)"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_measure_notes_are_never_shown_in_the_librarys_english():
    got = _run("designs", """return [
      m.warnKey({ code: 'check_warning', message: 'item_weight 0 not sent (Etsy needs a value above 0)' }),
      m.warnKey({ code: 'check_warning', message: 'item_length, item_width not sent: item_dimensions_unit is empty' }),
      m.warnKey({ code: 'measure_not_sent', message: 'item_height 0 not sent (Etsy needs a value above 0)' }),
      m.warnKey({ code: 'check_warning', message: 'something else' }),
      m.warnKey({ code: 'few_tags' }),
    ];""")
    assert got == ["warn.measure_not_sent", "warn.measure_not_sent", "warn.measure_not_sent",
                   "warn.check_warning", "warn.few_tags"]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_empty_tag_slots_are_ragged_but_four_to_a_row():
    got = _run("designs", "return Array.from({ length: 13 }, (_, n) => m.slotWidth(n));")
    fractions = [float(re.search(r"\* ([0-9.]+)\)$", w).group(1)) for w in got]
    assert all(w.startswith("calc((100% - 18px) * ") for w in got)
    rows = [fractions[i:i + 4] for i in range(0, 12, 4)]
    assert all(sum(row) <= 0.95 for row in rows)  # four always fit, with their gaps
    assert all(len(set(row)) > 2 for row in rows) and len({tuple(r) for r in rows}) == 3


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_draft_opens_at_its_own_address():
    got = _run("listing-detail", "return [m.listingPath(1000101, 'draft'), m.listingPath(1000101, 'active'), "
                                 "m.listingPath(1000101, '')];")
    assert got == ["/ilanlar/taslak/1000101", "/ilanlar/1000101", "/ilanlar/1000101"]
    # Tasarım Yükle only ever links the drafts it made, at that address.
    source = (STATIC / "js" / "pages" / "designs.js").read_text(encoding="utf-8")
    assert "`/ilanlar/${" not in source and "`/ilanlar/taslak/${" in source


# --- UP-2 / UP-3: what the history says about a draft's pictures ---------------------------------


class _WithIds(Client):
    def upload_listing_image(self, listing_id, image, *, rank, alt_text=""):
        super().upload_listing_image(listing_id, image, rank=rank, alt_text=alt_text)
        return {"listing_image_id": listing_id * 100 + rank, "rank": rank}


def test_the_history_keeps_the_picture_count_and_each_pictures_file(studio):  # noqa: F811
    ws, template = studio
    _artwork(ws.products / "retro-mountain-sunset.png")
    report = _drop_run(ws, template, _WithIds(ws))
    item = report.items[0]
    entry = _history(ws)[SHOP]["retro-mountain-sunset.png"]
    assert entry["images_total"] == len(item.images) == entry["images_uploaded"] == 3
    assert entry["files_total"] == 0
    assert entry["images"] == {str(item.listing_id * 100 + rank): path.name
                               for rank, path in enumerate(item.images, 1)}


def test_the_detail_page_gets_the_count_and_each_pictures_product(web):
    shop = shop_with(web, drafts=1, active=0, inactive=0)
    ws = web.ctx.workspace()
    for name in ("03-mug-white.jpg", "04-poster-oak-frame.jpg"):
        Image.new("RGB", (40, 40), "white").save(ws.mockups / name)
    (ws.mockups / "mockups.json").write_text(json.dumps({
        "03-mug-white.jpg": {"type": "mug", "color": "Beyaz", "enabled": True},
        "04-poster-oak-frame.jpg": {"type": "poster", "color": "Meşe çerçeve", "enabled": True},
    }), encoding="utf-8")
    listing = shop.by_id[1000001]
    base = dict(listing["images"][0], alt_text="")
    listing["images"] = [dict(base, listing_image_id=11, rank=1), dict(base, listing_image_id=12, rank=2),
                         dict(base, listing_image_id=13, rank=3), dict(base, listing_image_id=14, rank=4)]
    history(web, {"lake-life.png": {
        "status": "ok", "listing_id": 1000001, "images_uploaded": 3, "images_total": 3,
        "images": {"11": "lake-life--03-mug-white.jpg", "12": "lake-life--04-poster-oak-frame-2.jpg",
                   "13": "lake-life--flat.jpg"}}})
    data = web.client.get("/api/listings/1000001").json()
    source = data["source"]
    assert (source["status"], source["images_uploaded"], source["images_total"]) == ("ok", 3, 3)
    views = [(i["type"], i["color"], i["color_key"], i["flat"]) for i in data["images"]]
    assert views == [("mug", "Beyaz", "beyaz", False), ("poster", "Meşe çerçeve", "mese cerceve", False),
                     ("", "", "", True), ("", "", "", False)]


def test_a_listing_etsyotomasyon_did_not_make_keeps_its_alt_text_reading(web):
    shop_with(web, drafts=1, active=0, inactive=0)
    image = web.client.get("/api/listings/1000001").json()["images"][0]
    assert (image["type"], image["color"], image["flat"]) == ("tshirt", "Beyaz", False)


# --- UP-4: a transparent design's thumbnail for the checkerboard ---------------------------------


def test_a_checker_thumbnail_keeps_its_transparency(web):
    ws = web.ctx.workspace()
    Image.new("RGBA", (80, 60), (0, 0, 0, 0)).save(ws.products / "clear.png")
    resp = web.client.get("/api/files/thumb", params={"path": "2-PRODUCTS/clear.png", "w": 64, "bg": "checker"})
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/png"
    with Image.open(io.BytesIO(resp.content)) as image:
        assert image.mode == "RGBA" and image.getpixel((10, 10))[3] == 0
    white = web.client.get("/api/files/thumb", params={"path": "2-PRODUCTS/clear.png", "w": 64})
    assert white.headers["content-type"] == "image/jpeg"
    other = web.client.get("/api/files/thumb", params={"path": "2-PRODUCTS/clear.png", "w": 64, "bg": "red"})
    assert other.headers["content-type"] == "image/jpeg"
