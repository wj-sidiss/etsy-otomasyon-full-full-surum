"""Round 4: the products-folder guard, for sellers coming from a nested v0.2.0 folder.

v0.2.0 saved "Etsy Studio\\2-PRODUCTS" as the products folder and made a workspace of its
own in there; sellers then kept their template and upload history in it. Switching to
the main folder must carry both over, the nested folder's archive/ is never a product,
and a folder merely NAMED 2-PRODUCTS is neither "inside" another nor allowed to turn an
unrelated parent (the Desktop) into the workspace.
"""

from __future__ import annotations

import io
import json
import shutil

from PIL import Image
from typer.testing import CliRunner

from etsyotomasyon.cli import app
from etsyotomasyon.drop import automation
from etsyotomasyon.drop import workspace as workspace_mod
from etsyotomasyon.drop.template import Template


def _png(colour=(200, 60, 40)) -> bytes:
    image = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    for x in range(10, 30):
        for y in range(10, 30):
            image.putpixel((x, y), (*colour, 255))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _template(price: float) -> dict:
    return Template(1234567, source_title="Retro Mountain Sunset Shirt", fields={
        "taxonomy_id": 482, "price": price, "quantity": 10, "who_made": "i_did",
        "when_made": "made_to_order", "type": "physical", "shipping_profile_id": 5551,
    }, description="Soft ringspun cotton tee.", tags=["gift for him"]).to_dict()


def _v020_nested(web):
    """What v0.2.0 left: "Etsy Studio\\2-PRODUCTS" saved and used as a workspace of its own."""
    outer = web.ctx.workspace()
    web.ctx.update_shop_prefs(workspace=str(outer.products))
    nested = web.ctx.workspace()
    assert nested.root == outer.products and workspace_mod.is_workspace(nested.root)
    return outer, nested


def _history(root):
    return json.loads((root / "upload-history.json").read_text(encoding="utf-8"))


def _pending(web) -> dict:
    return web.client.get("/api/designs/pending").json()


def _use_main_folder(web):
    folders = web.client.get("/api/settings").json()["workspace"]
    assert folders["nested_in"]
    return web.client.post("/api/settings/workspace", json={"path": folders["nested_in"]})


# --- ws-1: "Use the main folder" carries the template and the upload history over ------------


def test_using_the_main_folder_carries_the_template_and_the_history_over(web):
    outer, nested = _v020_nested(web)
    nested.write_template(_template(21.0))
    (nested.root / "upload-history.json").write_text(json.dumps(
        {"99999": {"sunset.png": {"status": "ok", "listing_id": 4000001}}}), encoding="utf-8")
    (nested.products / "sunset.png").write_bytes(_png())            # already a draft
    (nested.products / "moon.png").write_bytes(_png((1, 2, 3)))     # not uploaded yet
    assert [i["name"] for i in _pending(web)["items"]] == ["moon.png"]

    resp = _use_main_folder(web)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["migrated"] == ["history", "template"]
    assert body["workspace"]["root"] == str(outer.root) and body["workspace"]["nested_in"] is None

    after = _pending(web)
    assert "template" not in after["blockers"] and after["template"]["price"] == 21.0
    assert not (nested.root / "product.json").exists()
    assert _history(outer.root)["99999"]["sunset.png"]["listing_id"] == 4000001
    assert (nested.root / "upload-history.json").is_file(), "a history is never deleted"

    # The note says: move the designs into the main 2-PRODUCTS. The drafted one stays known.
    for f in list(nested.products.iterdir()):
        shutil.move(str(f), str(outer.products / f.name))
    again = _pending(web)
    assert [i["name"] for i in again["items"]] == ["moon.png"]
    assert again["already_done"] == 1


def test_an_older_template_in_the_main_folder_is_kept_aside_not_used(web):
    outer, nested = _v020_nested(web)
    outer.write_template(_template(9.0))      # from before the seller picked 2-PRODUCTS
    nested.write_template(_template(21.0))    # what the seller uses now
    assert _pending(web)["template"]["price"] == 21.0
    assert _use_main_folder(web).json()["migrated"] == ["template"]
    assert _pending(web)["template"]["price"] == 21.0
    kept = json.loads((outer.root / "product.json.bak").read_text(encoding="utf-8"))
    assert kept["fields"]["price"] == 9.0


def test_entries_the_main_history_has_are_never_overwritten(web):
    outer, nested = _v020_nested(web)
    (outer.root / "upload-history.json").write_text(json.dumps(
        {"99999": {"Sunset.png": {"status": "ok", "listing_id": 1}}}), encoding="utf-8")
    (nested.root / "upload-history.json").write_text(json.dumps(
        {"99999": {"sunset.png": {"status": "partial", "listing_id": 2},
                   "moon.png": {"status": "ok", "listing_id": 3}},
         "55555": {"cat.png": {"status": "ok", "listing_id": 4}}}), encoding="utf-8")
    assert _use_main_folder(web).json()["migrated"] == ["history"]
    assert _history(outer.root) == {
        "99999": {"Sunset.png": {"status": "ok", "listing_id": 1},
                  "moon.png": {"status": "ok", "listing_id": 3}},
        "55555": {"cat.png": {"status": "ok", "listing_id": 4}},
    }


def test_a_run_holding_the_main_folder_stops_the_switch_and_loses_nothing(web):
    outer, nested = _v020_nested(web)
    (nested.root / "upload-history.json").write_text(json.dumps(
        {"99999": {"sunset.png": {"status": "ok", "listing_id": 4000001}}}), encoding="utf-8")
    automation.lock_path(outer.root).write_text("1\nhost\ntoken\n", encoding="utf-8")
    resp = _use_main_folder(web)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "workspace_locked"
    assert web.ctx.workspace_root() == nested.root, "still the folder with the history"
    assert not (outer.root / "upload-history.json").exists()


def test_the_cli_carries_them_over_when_path_names_the_nested_folder(tmp_path):
    outer = workspace_mod.Workspace(tmp_path / "Etsy Studio").create()
    nested = workspace_mod.Workspace(outer.products).create()
    nested.write_template(_template(21.0))
    (nested.root / "upload-history.json").write_text(json.dumps(
        {"99999": {"sunset.png": {"status": "ok", "listing_id": 4000001}}}), encoding="utf-8")
    result = CliRunner().invoke(app, ["drop", "init", "--path", str(nested.root)])
    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert "is a folder of the workspace" in flat
    assert "Carried its upload history (upload-history.json) and template (product.json)" in flat
    assert json.loads(outer.template_path.read_text(encoding="utf-8"))["fields"]["price"] == 21.0
    assert _history(outer.root)["99999"]["sunset.png"]["status"] == "ok"
    again = CliRunner().invoke(app, ["drop", "init", "--path", str(nested.root)])
    assert again.exit_code == 0 and "Carried" not in again.output, "nothing new the second time"


# --- ws-2: the nested folder's archive/ is never a product -----------------------------------


def test_a_nested_workspaces_archive_is_not_a_product(tmp_path):
    ws = workspace_mod.Workspace(tmp_path / "Etsy Studio").create()
    nested = workspace_mod.Workspace(ws.products).create()
    Image.new("RGB", (40, 40), "white").save(nested.archive / "removed.png")
    assert ws.product_groups() == []


def test_a_real_product_folder_called_archive_still_counts(tmp_path):
    # Only the nested workspace's own archive/ is skipped, never a seller's folder by name.
    ws = workspace_mod.Workspace(tmp_path / "Etsy Studio").create()
    (ws.products / "archive").mkdir()
    Image.new("RGB", (40, 40), "white").save(ws.products / "archive" / "01.png")
    assert [path.name for path, _ in ws.product_groups()] == ["archive"]


def test_designs_taken_out_while_nested_stay_out_after_the_switch(web):
    _outer, nested = _v020_nested(web)
    (nested.products / "unwanted.png").write_bytes(_png())
    (nested.products / "keep.png").write_bytes(_png((9, 9, 9)))
    resp = web.client.request("DELETE", "/api/designs/files", params={"path": "unwanted.png"})
    assert resp.status_code == 200 and (nested.archive / "unwanted.png").is_file()
    _use_main_folder(web)
    assert "archive" not in [i["name"] for i in _pending(web)["items"]]


# --- ws-4: nested_in only for a folder that really is inside another workspace --------------


def test_a_workspace_merely_named_2_products_is_not_reported_as_nested(web):
    outer, nested = _v020_nested(web)
    shutil.rmtree(outer.mockups)
    shutil.rmtree(outer.drafts)
    assert not workspace_mod.is_workspace(outer.root)
    assert web.client.get("/api/settings").json()["workspace"]["nested_in"] is None


def test_enclosing_root_needs_an_existing_workspace_around(tmp_path):
    outer = workspace_mod.Workspace(tmp_path / "Etsy Studio").create()
    assert workspace_mod.enclosing_root(outer.products) == outer.root
    assert workspace_mod.enclosing_root(outer.products / "Mug designs") == outer.root
    assert workspace_mod.enclosing_root(tmp_path / "Docs" / "2-PRODUCTS") is None
    assert workspace_mod.enclosing_root(outer.root) is None


# --- ws-5: a new "2-PRODUCTS" never takes over a parent holding other things -----------------


def test_a_new_2_products_in_a_busy_folder_is_used_as_it_is(web, tmp_path):
    desk = tmp_path / "SellerDesktop"          # stands in for the seller's Desktop
    desk.mkdir()
    (desk / "README.txt").write_text("my own notes", encoding="utf-8")
    (desk / "invoice.pdf").write_bytes(b"%PDF")
    chosen = desk / "2-PRODUCTS"
    data = web.client.post("/api/settings/workspace", json={"path": str(chosen)}).json()
    assert data["adjusted"] is None and data["workspace"]["root"] == str(chosen)
    assert sorted(p.name for p in desk.iterdir()) == ["2-PRODUCTS", "README.txt", "invoice.pdf"]
    assert (desk / "README.txt").read_text(encoding="utf-8") == "my own notes"


def test_a_new_2_products_in_an_empty_folder_still_makes_the_parent_the_workspace(web, tmp_path):
    parent = tmp_path / "Etsy"
    parent.mkdir()
    chosen = parent / "2-PRODUCTS"
    chosen.mkdir()
    data = web.client.post("/api/settings/workspace", json={"path": str(chosen)}).json()
    assert data["adjusted"] == {"chosen": str(chosen), "root": str(parent), "inside": False}
    assert workspace_mod.is_workspace(parent)


def test_create_never_replaces_a_readme_it_did_not_write(tmp_path):
    root = tmp_path / "Studio"
    root.mkdir()
    (root / "README.txt").write_text("my own notes", encoding="utf-8")
    workspace_mod.Workspace(root).create()
    assert (root / "README.txt").read_text(encoding="utf-8") == "my own notes"
    (root / "README.txt").write_text("ETSY STUDIO\n===\nold text", encoding="utf-8")
    workspace_mod.Workspace(root).create()
    assert (root / "README.txt").read_text(encoding="utf-8") == workspace_mod.README_TEXT


def test_the_cli_says_when_only_the_name_moved_the_workspace_up(tmp_path):
    chosen = tmp_path / "Etsy" / "2-PRODUCTS"
    result = CliRunner().invoke(app, ["drop", "init", "--path", str(chosen)])
    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert "is named like a workspace folder" in flat and "is a folder of the workspace" not in flat
    assert workspace_mod.is_workspace(tmp_path / "Etsy")
