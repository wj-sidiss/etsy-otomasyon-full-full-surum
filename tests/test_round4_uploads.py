"""Round 4: where a dropped file lands on Tasarım Yükle.

A download file never opens a "-2" copy of a product (it is sent after the photos, or,
with no photo of its product in the drop, straight into the folder of that name); a
corrected download replaces the old one; "dosyalar" means something only to a digital
template; and a product folder can never be named like the workspace's own folders.
The page's planning code runs under node when it is installed.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image
from test_web_designs import _digital_template, _jpg, _put, _setup_shop

JS_DIR = Path(__file__).resolve().parents[1] / "etsyotomasyon" / "web" / "static" / "js"


def _pending_items(web) -> dict:
    return {i["name"]: i for i in web.client.get("/api/designs/pending").json()["items"]}


# --- ws-3: a folder named like the workspace's own is refused, not saved and hidden ----------


@pytest.mark.parametrize("path", ["2-PRODUCTS/sunset.png", "1-mockups/sunset.png",
                                  "3-Drafts/sunset.png", "2-PRODUCTS/dosyalar/planner.pdf"])
def test_a_product_folder_named_like_a_workspace_folder_is_refused(web, path):
    ws = web.ctx.workspace()
    resp = _put(web, path, b"%PDF" if path.endswith(".pdf") else _png(), batch="b1")
    assert resp.status_code == 422, resp.text
    error = resp.json()["error"]
    assert error["code"] == "reserved_folder" and error["params"]["name"] == path.split("/")[0]
    assert sorted(p.name for p in ws.products.iterdir()) == []


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", (40, 40), (200, 60, 40, 255)).save(buffer, "PNG")
    return buffer.getvalue()


# --- ui-2 / pipe-2: the missing dosyalar of a product already there joins that product -------


def test_a_download_sent_without_a_batch_joins_the_product_already_there(web):
    _fake, ws = _setup_shop(web)
    _digital_template(ws)
    _put(web, "botanical coloring pages/01-front.jpg", _jpg(), batch="b1")
    before = _pending_items(web)["botanical coloring pages"]
    assert before["deliverable_problem"]["code"] == "no_deliverable"

    # What the page sends when only the dosyalar file of that product was dropped.
    resp = _put(web, "botanical coloring pages/dosyalar/coloring-pages.pdf", b"%PDF-1.4")
    assert resp.status_code == 200, resp.text
    assert resp.json()["folder"] == "botanical coloring pages"
    assert sorted(p.name for p in ws.products.iterdir()) == ["botanical coloring pages"]
    after = _pending_items(web)["botanical coloring pages"]
    assert after["deliverable_problem"] is None
    assert [d["name"] for d in after["deliverables"]] == ["coloring-pages.pdf"]


def test_a_redropped_folder_whose_photo_goes_first_stays_one_product(web):
    _fake, ws = _setup_shop(web)
    _digital_template(ws)
    _put(web, "botanical coloring pages/01-front.jpg", _jpg(), batch="b1")
    # The page sends every photo before the first download file (ui-1).
    assert _put(web, "botanical coloring pages/01-front.jpg", _jpg(),
                batch="b2").json()["duplicate"] is True
    pdf = _put(web, "botanical coloring pages/dosyalar/coloring-pages.pdf", b"%PDF",
               batch="b2").json()
    assert pdf["folder"] == "botanical coloring pages"
    assert sorted(p.name for p in ws.products.iterdir()) == ["botanical coloring pages"]


def test_a_corrected_download_replaces_the_old_one_which_goes_to_the_archive(web):
    _fake, ws = _setup_shop(web)
    _digital_template(ws)
    _put(web, "boho planner/01-front.jpg", _jpg(), batch="b1")
    _put(web, "boho planner/dosyalar/planner.pdf", b"%PDF old", batch="b1")
    resp = _put(web, "boho planner/dosyalar/planner.pdf", b"%PDF corrected")
    body = resp.json()
    assert resp.status_code == 200 and body["replaced"] is True and body["file"] == "planner.pdf"
    folder = ws.products / "boho planner" / "dosyalar"
    assert sorted(p.name for p in folder.iterdir()) == ["planner.pdf"], "never two versions"
    assert (folder / "planner.pdf").read_bytes() == b"%PDF corrected"
    kept = ws.archive / "boho planner" / "dosyalar" / "planner.pdf"
    assert kept.read_bytes() == b"%PDF old"
    again = _put(web, "boho planner/dosyalar/planner.pdf", b"%PDF corrected").json()
    assert again["duplicate"] is True and "replaced" not in again


def test_a_drafted_products_download_is_never_replaced(web):
    _fake, ws = _setup_shop(web)
    _digital_template(ws)
    _put(web, "boho planner/01-front.jpg", _jpg(), batch="b1")
    _put(web, "boho planner/dosyalar/planner.pdf", b"%PDF old", batch="b1")
    (ws.root / "upload-history.json").write_text(json.dumps(
        {"12345678": {"boho planner": {"status": "ok", "listing_id": 1}}}), encoding="utf-8")
    body = _put(web, "boho planner/dosyalar/planner.pdf", b"%PDF corrected").json()
    assert body["known"] is True
    assert (ws.products / "boho planner" / "dosyalar" / "planner.pdf").read_bytes() == b"%PDF old"


# --- ui-3: a physical template takes no download files -------------------------------------


def test_a_physical_template_refuses_a_download_file(web):
    _fake, ws = _setup_shop(web)  # its template is physical
    resp = _put(web, "my designs/dosyalar/planner.pdf", b"%PDF", batch="b1")
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "not_digital"
    assert list(ws.products.iterdir()) == []


# --- the page's own planning (designs.js), under node ------------------------------------------

NODE = shutil.which("node")


def _plan(script: str):
    url = (JS_DIR / "pages" / "designs.js").as_uri()
    code = (f"const m = await import({json.dumps(url)});\n"
            "const f = (path) => ({ path, file: { name: path.split('/').pop() } });\n"
            f"const out = (() => {{ {script} }})();\n"
            "console.log(JSON.stringify(out));")
    done = subprocess.run([NODE, "--input-type=module", "-e", code], capture_output=True,
                          text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_download_of_needs_a_digital_template_and_a_file_right_under_dosyalar():
    got = _plan("""return [
      m.downloadOf('Planner/dosyalar/planner.pdf', true),
      m.downloadOf('Planner/Files/planner.pdf', true),
      m.downloadOf('bundle/dosyalar/PNG/cat.png', true),
      m.downloadOf('Planner/dosyalar/planner.pdf', false),
      m.downloadOf('dosyalar/planner.pdf', true),
      m.downloadOf('Planner/01.jpg', true),
    ];""")
    assert got == [
        {"product": "Planner", "sub": "dosyalar", "name": "planner.pdf", "nested": False},
        {"product": "Planner", "sub": "Files", "name": "planner.pdf", "nested": False},
        {"nested": True}, None, None, None]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_digital_drop_sends_photos_first_and_marks_downloads_without_photos():
    got = _plan("""return m.planUploads([
      f('coloring/dosyalar/pages.pdf'),
      f('boho planner/dosyalar/planner.pdf'),
      f('boho planner/01.jpg'),
      f('wall art set/dosyalar/A4/art-a4.png'),
      f('wall art set/01-front.jpg'),
    ], 'products', true).map((e) => [e.path, !!e.deliverable, !!e.attach]);""")
    assert got == [
        ["boho planner/01.jpg", False, False],
        ["wall art set/01-front.jpg", False, False],
        ["coloring/dosyalar/pages.pdf", True, True],  # joins "coloring" already there
        ["boho planner/dosyalar/planner.pdf", True, False],  # its photo claims the folder
    ], "a file nested in dosyalar never becomes a product ('A4') of its own"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_physical_drop_treats_a_dosyalar_folder_as_plain_designs():
    got = _plan("""return [
      m.planUploads([f('My Etsy designs/dosyalar/cat.png'), f('My Etsy designs/dosyalar/dog.png')],
                    'designs', false).map((e) => [e.path, !!e.deliverable]),
      m.planUploads([f('My Etsy designs/files/cat.png')], 'designs', true)
        .map((e) => [e.path, !!e.deliverable]),
    ];""")
    assert got == [[["cat.png", False], ["dog.png", False]], []]
