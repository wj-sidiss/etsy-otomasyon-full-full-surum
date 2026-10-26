"""Mağaza Bağlantısı as the video draws it: the connected card's shop row reads two counts
(GET /api/connect/counts), and the page's strings match the video's wording."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import httpx
import pytest
from web_helpers import ETSY_SHOP_ID, use_fake_etsy

from etsyotomasyon.web.server import STATIC_DIR

LISTINGS_PATH = f"/shops/{ETSY_SHOP_ID}/listings"
RECEIPTS_PATH = f"/shops/{ETSY_SHOP_ID}/receipts"


def _listings(request: httpx.Request):
    assert request.url.params.get("state") == "active"
    assert request.url.params.get("limit") == "1"
    return {"count": 306, "results": []}


def _receipts(request: httpx.Request):
    params = request.url.params
    assert params.get("limit") == "1"
    assert params.get("was_paid") == "true" and params.get("was_shipped") == "false"
    assert params.get("was_canceled") == "false"
    return {"count": 24, "results": []}


def _fake(web):
    fake = use_fake_etsy(web)
    fake.add("GET", LISTINGS_PATH, _listings)
    fake.add("GET", RECEIPTS_PATH, _receipts)
    return fake


def _count_calls(fake) -> int:
    return sum(1 for _method, path in fake.calls if path in (LISTINGS_PATH, RECEIPTS_PATH))


def test_counts_read_active_listings_and_orders_to_ship(web):
    fake = _fake(web)
    data = web.client.get("/api/connect/counts").json()
    assert data == {"listings": {"value": 306, "error": None}, "to_ship": {"value": 24, "error": None}}
    assert _count_calls(fake) == 2


def test_counts_are_cached_and_dropped_when_the_shop_changes(web):
    fake = _fake(web)
    web.client.get("/api/connect/counts")
    web.client.get("/api/connect/counts")
    assert _count_calls(fake) == 2  # the second read came from the cache
    web.ctx.changed("listings")
    web.client.get("/api/connect/counts")
    assert _count_calls(fake) == 3  # only the listings count is read again
    web.ctx.changed("orders")
    web.client.get("/api/connect/counts")
    assert _count_calls(fake) == 4


def test_one_count_failing_leaves_the_other(web):
    fake = _fake(web)
    fake.error("GET", RECEIPTS_PATH, 403, "no transactions_r")
    data = web.client.get("/api/connect/counts").json()
    assert data["listings"] == {"value": 306, "error": None}
    assert data["to_ship"]["value"] is None
    assert data["to_ship"]["error"]["code"]


def test_counts_need_a_connected_shop(web):
    use_fake_etsy(web, connected=False)
    resp = web.client.get("/api/connect/counts")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "setup_needed"


def test_counts_do_not_wait_for_etsy_while_offline(web):
    fake = _fake(web)
    web.client.get("/api/connect/counts")  # cached while online
    fake.offline = True
    web.ctx.refresh_status(force=True)
    assert web.ctx.status["state"] == "offline"
    web.ctx.changed("orders")
    before = _count_calls(fake)
    data = web.client.get("/api/connect/counts").json()
    assert data["listings"]["value"] == 306  # the cached count, however old
    assert data["to_ship"] == {"value": None, "error": {"code": "offline", "message": "Etsy is not reachable.",
                                                        "params": {}}}
    assert _count_calls(fake) == before


def _strings():
    return json.loads((STATIC_DIR / "i18n" / "connect.json").read_text(encoding="utf-8"))


def test_the_page_speaks_the_video_s_words():
    tr = _strings()["tr"]
    assert tr["connect.button"] == "Etsy mağazanı bağla"
    assert tr["connect.hint"] == "Etsy'nin onay sayfasına yönlendirilirsiniz."
    assert tr["connected.title"] == "Mağaza bağlandı"
    assert tr["connected.next"] == "Devam: Mockuplar"
    assert tr["connect.phase.verifying"] == "İzniniz doğrulanıyor…"
    assert tr["flow.1.sub"] == "Resmî Etsy sayfasında"
    assert [tr[f"chip.{k}"] for k in ("listings", "orders", "tracking", "no_delete")] == [
        "İlan okuma ve taslak", "Sipariş okuma", "Takip no yükleme", "Silme izni yok"]
    # The video uses the ellipsis character, never three dots.
    for lang in ("tr", "en"):
        assert not [k for k, v in _strings()[lang].items() if "..." in v]


def test_no_text_names_the_old_connect_button():
    """The button is "Etsy mağazanı bağla" now; no help text may still say “Bağlan”."""
    for name in ("connect.json", "settings.json"):
        data = json.loads((STATIC_DIR / "i18n" / name).read_text(encoding="utf-8"))
        assert not [k for k, v in data["tr"].items() if "“Bağlan”" in v], name
        assert not [k for k, v in data["en"].items() if "“Connect”" in v], name


def test_the_connect_bar_keeps_the_video_s_thirds():
    """The video changes the badge, the bar, the hint and the spinning step together at
    thirds: step 2 spins below 67% (with "İzniniz doğrulanıyor…"), step 3 only while the
    shop is read. The bar never goes back: the creep stays under the code's mark."""
    js = (STATIC_DIR / "js" / "pages" / "connect.js").read_text(encoding="utf-8")
    body = re.search(r"const PHASE_PCT = \{([^}]*)\}", js).group(1)
    pct = {k: int(v) for k, v in re.findall(r"(\w+): (\d+)", body)}
    creep = int(re.search(r"const CREEP_TO = (\d+)", js).group(1))
    assert pct["opened"] < creep < pct["code_received"] < 200 / 3 < pct["fetching_shop"] < pct["done"] == 100
    assert 'if (p === "opened" || p === "code_received") return ["done", "running", "todo"];' in js


def test_every_finished_step_reads_done():
    """The video's stepper reads "Tamamlandı" under every finished step (no "6 mockup",
    no "Hazır")."""
    for lang in ("tr", "en"):
        keys = _strings()[lang]
        assert not {"steps.mockups_n", "steps.mockups_n_one", "steps.ready"} & set(keys)
    assert _strings()["tr"]["steps.done"] == "Tamamlandı"


NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_connected_card_leads_to_the_first_step_still_to_do():
    """The "Devam" button follows the stepper: Mockuplar until one is ready, then Şablon İlan until
    the template is saved, then Tasarım Yükle (not Mockuplar once setup is finished)."""
    url = (STATIC_DIR / "js" / "pages" / "connect.js").as_uri()
    code = (f"const m = await import({json.dumps(url)});\n"
            "console.log(JSON.stringify([null, {}, {mockups: 0, template: true}, {mockups: 2},"
            " {mockups: 6, template: false}, {mockups: 6, template: true}].map(m.nextSetupStep)));")
    done = subprocess.run([NODE, "--input-type=module", "-e", code], capture_output=True,
                          text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    got = [(x["key"], x["path"]) for x in json.loads(done.stdout)]
    mockups, template = ("", "/kurulum/mockuplar"), ("_template", "/kurulum/sablon")
    upload = ("_upload", "/tasarim-yukle")
    assert got == [mockups, mockups, mockups, template, template, upload]
    tr, en = _strings()["tr"], _strings()["en"]
    for key, _path in (mockups, template, upload):
        for lang in (tr, en):
            assert lang[f"connected.next{key}"] and lang[f"connected.next{key}_hint"]
    assert tr["connected.next_upload"] == "Devam: Tasarım Yükle"
    assert tr["connected.next_template"] == "Devam: Şablon İlan"
