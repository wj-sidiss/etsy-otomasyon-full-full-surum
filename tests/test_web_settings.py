"""Ayarlar: the products folder and the setup checklist.

Language, hiding names, shops and quitting are core endpoints, tested in
test_web_core.py; here only what the settings page adds.
"""

from __future__ import annotations

import threading

from web_helpers import KEYSTRING, use_fake_etsy

from etsyotomasyon import __version__
from etsyotomasyon.drop import workspace as workspace_mod


def _by_number(items):
    return {item["number"]: item for item in items}


def test_the_overview_names_the_folders(web):
    data = web.client.get("/api/settings").json()
    assert data["version"] == __version__ and data["license"] == "MIT"
    assert data["repo"].startswith("https://github.com/")
    ws = data["workspace"]
    default = str(web.desktop / "Etsy Studio")
    assert ws["root"] == default and ws["default"] == default and ws["custom"] is False
    assert [f["which"] for f in ws["folders"]] == ["workspace", "mockups", "products", "drafts"]
    assert [f["name"] for f in ws["folders"][1:]] == ["1-MOCKUPS", "2-PRODUCTS", "3-DRAFTS"]


def test_another_products_folder_is_created_and_used(web, tmp_path):
    target = tmp_path / "Studio 2"
    resp = web.client.post("/api/settings/workspace", json={"path": f'"{target}"'})
    assert resp.status_code == 200, resp.text
    ws = resp.json()["workspace"]
    assert ws["root"] == str(target) and ws["custom"] is True
    assert all(f["exists"] for f in ws["folders"])
    assert (target / workspace_mod.MOCKUPS_DIR).is_dir() and (target / "README.txt").is_file()
    assert web.client.get("/api/prefs").json()["workspace"] == str(target)
    assert web.ctx.workspace().root == target


def test_an_empty_path_goes_back_to_the_default(web, tmp_path):
    web.client.post("/api/settings/workspace", json={"path": str(tmp_path / "Other")})
    ws = web.client.post("/api/settings/workspace", json={"path": "  "}).json()["workspace"]
    assert ws["custom"] is False and ws["root"] == str(web.desktop / "Etsy Studio")
    assert "workspace" not in web.ctx.shop_prefs()


def test_a_relative_path_is_refused(web):
    resp = web.client.post("/api/settings/workspace", json={"path": "Studio"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "workspace_not_absolute"


def test_a_file_is_not_a_folder(web, tmp_path):
    a_file = tmp_path / "notes.txt"
    a_file.write_text("x", encoding="utf-8")
    resp = web.client.post("/api/settings/workspace", json={"path": str(a_file)})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "workspace_not_folder"


def test_the_path_must_be_text(web):
    resp = web.client.post("/api/settings/workspace", json={"path": 5})
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "invalid"


def test_the_folder_is_not_moved_while_a_task_runs(web, tmp_path):
    release = threading.Event()
    job = web.ctx.jobs.start("designs", "x:y", lambda job: release.wait(5))
    try:
        resp = web.client.post("/api/settings/workspace", json={"path": str(tmp_path / "S")})
        assert resp.status_code == 409 and resp.json()["error"]["code"] == "busy"
    finally:
        release.set()
        job.wait(5)
    assert not (tmp_path / "S").exists()


def test_the_checklist_without_keys(web):
    data = web.client.post("/api/settings/doctor", json={}).json()
    steps = _by_number(data["items"])
    assert data["total"] == 10 and len(steps) == 10
    assert steps[1]["key"] == "python" and steps[1]["state"] == "ok"
    assert steps[4]["state"] == "unknown" and steps[4]["question"]
    assert steps[5]["key"] == "credentials" and steps[5]["state"] == "missing"
    assert steps[8]["key"] == "api" and steps[8]["state"] == "missing"
    assert steps[9]["state"] == "missing"
    assert steps[10]["required"] is False
    assert data["next"] == "etsyotomasyon init"
    assert data["ok"] == sum(1 for s in data["items"] if s["state"] == "ok")


def test_the_checklist_asks_etsy_through_the_app(web):
    fake = use_fake_etsy(web)
    web.ctx.workspace()
    data = web.client.post("/api/settings/doctor", json={}).json()
    steps = _by_number(data["items"])
    assert steps[5]["state"] == "ok" and KEYSTRING[:6] in steps[5]["detail"]
    assert steps[8]["state"] == "ok"
    assert steps[9]["state"] == "ok"
    assert ("GET", "/openapi-ping") in fake.calls


def test_the_checklist_when_etsy_refuses_the_keys(web):
    fake = use_fake_etsy(web, connected=False)
    fake.error("GET", "/openapi-ping", 403, "Invalid API key")
    steps = _by_number(web.client.post("/api/settings/doctor", json={}).json()["items"])
    assert steps[8]["state"] == "missing" and "Invalid API key" in steps[8]["detail"]


def test_the_checklist_offline(web):
    fake = use_fake_etsy(web, connected=False)
    fake.offline = True
    steps = _by_number(web.client.post("/api/settings/doctor", json={}).json()["items"])
    assert steps[8]["state"] == "warn"


# --- a folder of a workspace means that workspace (drop.workspace.root_for) ---------------


def test_choosing_2_products_keeps_the_products_folder_it_belongs_to(web):
    # The tester's case: "Etsy Studio\2-PRODUCTS" chosen, "...\2-PRODUCTS\2-PRODUCTS" made.
    ws = web.ctx.workspace()
    resp = web.client.post("/api/settings/workspace", json={"path": str(ws.products)})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["adjusted"] == {"chosen": str(ws.products), "root": str(ws.root),
                                "inside": True}
    # The default folder, however it was reached, stays "the default".
    assert data["workspace"]["root"] == str(ws.root) and data["workspace"]["custom"] is False
    assert "workspace" not in web.ctx.shop_prefs()
    assert not (ws.products / workspace_mod.MOCKUPS_DIR).exists()
    assert not (ws.products / workspace_mod.PRODUCTS_DIR).exists()
    assert web.ctx.workspace().root == ws.root


def test_a_folder_deep_inside_a_custom_workspace_means_that_workspace(web, tmp_path):
    studio = tmp_path / "Studio"
    web.client.post("/api/settings/workspace", json={"path": str(studio)})
    deep = studio / workspace_mod.DRAFTS_DIR / "calibration"
    data = web.client.post("/api/settings/workspace", json={"path": str(deep)}).json()
    assert data["adjusted"] == {"chosen": str(deep), "root": str(studio), "inside": True}
    assert data["workspace"]["root"] == str(studio) and data["workspace"]["custom"] is True
    assert web.ctx.shop_prefs()["workspace"] == str(studio)
    assert not (deep / workspace_mod.MOCKUPS_DIR).exists()


def test_a_new_2_products_folder_makes_its_parent_the_workspace(web, tmp_path):
    chosen = tmp_path / "Etsy" / "2-PRODUCTS"
    data = web.client.post("/api/settings/workspace", json={"path": str(chosen)}).json()
    assert data["adjusted"] == {"chosen": str(chosen), "root": str(tmp_path / "Etsy"),
                                "inside": False}
    assert (tmp_path / "Etsy" / workspace_mod.MOCKUPS_DIR).is_dir() and chosen.is_dir()
    assert not (chosen / workspace_mod.PRODUCTS_DIR).exists()


def test_an_ordinary_folder_is_not_adjusted(web, tmp_path):
    data = web.client.post("/api/settings/workspace", json={"path": str(tmp_path / "S")}).json()
    assert data["adjusted"] is None and data["workspace"]["nested_in"] is None


def test_a_nested_folder_saved_by_an_older_version_is_pointed_out(web):
    ws = web.ctx.workspace()
    web.ctx.update_shop_prefs(workspace=str(ws.products))  # what v0.2.0 saved
    web.ctx.workspace()  # and the second workspace it then made inside
    folders = web.client.get("/api/settings").json()["workspace"]
    assert folders["root"] == str(ws.products) and folders["nested_in"] == str(ws.root)
    # "Ana klasörü kullan" posts the folder it names.
    fixed = web.client.post("/api/settings/workspace", json={"path": folders["nested_in"]}).json()
    assert fixed["adjusted"] is None
    assert fixed["workspace"]["root"] == str(ws.root) and fixed["workspace"]["nested_in"] is None
    assert web.client.get("/api/settings").json()["workspace"]["nested_in"] is None
