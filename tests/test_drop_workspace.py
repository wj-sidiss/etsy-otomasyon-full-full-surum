"""The workspace guard: a folder of a workspace chosen as the workspace means that workspace.

A tester picked "Etsy Studio\\2-PRODUCTS" as the products folder and got a second
workspace inside the first ("...\\2-PRODUCTS\\2-PRODUCTS"), with the mockups left behind
in the outer 1-MOCKUPS. `workspace.root_for` is the rule; the settings API and the CLI's
--path use it (the API side is in test_web_settings.py).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image
from typer.testing import CliRunner

from etsyotomasyon.cli import app
from etsyotomasyon.drop import mockup, workspace
from etsyotomasyon.drop.workspace import Workspace, root_for


def _studio(tmp_path: Path, name: str = "Etsy Studio") -> Workspace:
    return Workspace(tmp_path / name).create()


# --- root_for ------------------------------------------------------------------------


def test_an_ordinary_folder_is_used_as_it_is(tmp_path):
    assert root_for(tmp_path / "Studio") == tmp_path / "Studio"
    ws = _studio(tmp_path)
    assert root_for(ws.root) == ws.root


@pytest.mark.parametrize("sub", ["1-MOCKUPS", "2-PRODUCTS", "3-DRAFTS", "archive", "2-products"])
def test_a_folder_of_an_existing_workspace_means_the_workspace(tmp_path, sub):
    ws = _studio(tmp_path)
    assert root_for(ws.root / sub) == ws.root


@pytest.mark.parametrize("inside", [
    ("2-PRODUCTS", "Mug designs"),
    ("2-PRODUCTS", "Mug designs", "files"),
    ("3-DRAFTS", "calibration"),
    ("1-MOCKUPS", "not there yet"),
])
def test_any_folder_inside_those_means_the_workspace_too(tmp_path, inside):
    ws = _studio(tmp_path)
    assert root_for(ws.root.joinpath(*inside)) == ws.root


def test_a_workspace_nested_by_an_older_version_gives_the_outermost_one(tmp_path):
    # v0.2.0 made "Etsy Studio\2-PRODUCTS" a workspace of its own when it was chosen.
    outer = _studio(tmp_path)
    Workspace(outer.products).create()
    assert workspace.is_workspace(outer.products)
    assert root_for(outer.products) == outer.root
    assert root_for(outer.products / "2-PRODUCTS") == outer.root
    assert root_for(outer.products / "2-PRODUCTS" / "Mug designs") == outer.root


def test_a_numbered_folder_outside_any_workspace_gives_its_parent(tmp_path):
    # Nothing is there yet: the parent becomes the workspace, so the folder the seller
    # picked is its 2-PRODUCTS rather than holding a 2-PRODUCTS of its own.
    assert root_for(tmp_path / "Etsy" / "2-PRODUCTS") == tmp_path / "Etsy"
    assert root_for(tmp_path / "Etsy" / "3-drafts") == tmp_path / "Etsy"


def test_a_numbered_folder_at_a_drive_root_is_left_alone(tmp_path):
    top = Path(tmp_path.anchor) / "2-PRODUCTS"
    assert root_for(top) == top


def test_a_workspace_beside_the_numbered_folders_is_its_own(tmp_path):
    ws = _studio(tmp_path)
    assert root_for(ws.root / "Shop 2") == ws.root / "Shop 2"


def test_a_workspace_under_a_folder_that_happens_to_be_one_is_its_own(tmp_path):
    # Someone ran `drop init` in their home folder; the Desktop's workspace is still its own.
    home = _studio(tmp_path, "home")
    desktop_ws = home.root / "Desktop" / "Etsy Studio"
    assert root_for(desktop_ws) == desktop_ws
    assert root_for(desktop_ws / "2-PRODUCTS") == desktop_ws


def test_dot_dot_is_resolved_before_looking(tmp_path):
    ws = _studio(tmp_path)
    assert root_for(ws.products / "Mug designs" / ".." / "..") == ws.root


def test_root_for_creates_nothing(tmp_path):
    base = tmp_path / "empty"
    base.mkdir()
    root_for(base / "Etsy" / "2-PRODUCTS" / "x")
    assert list(base.iterdir()) == []


# --- what a nested workspace left behind is never a product --------------------------


def test_the_folders_a_nested_workspace_left_in_2_products_are_not_products(tmp_path):
    ws = _studio(tmp_path)
    nested = Workspace(ws.products).create()
    Image.new("RGB", (40, 40), "white").save(nested.mockups / "blank-tee.png")
    Image.new("RGB", (40, 40), "white").save(nested.products / "sunset.png")
    Image.new("RGB", (40, 40), "white").save(nested.drafts / "sunset-1.jpg")
    real = ws.products / "Mug designs"
    real.mkdir()
    Image.new("RGB", (40, 40), "white").save(real / "01.png")
    loose = ws.products / "cat.png"
    Image.new("RGBA", (40, 40)).save(loose)
    assert [(path.name, len(images)) for path, images in ws.product_groups()] == [
        ("cat.png", 0), ("Mug designs", 1)]


def test_a_whole_workspace_dropped_into_2_products_is_not_a_product(tmp_path):
    ws = _studio(tmp_path)
    other = Workspace(ws.products / "Old studio").create()
    Image.new("RGB", (40, 40), "white").save(other.root / "stray.png")
    assert ws.product_groups() == []


# --- the CLI's --path ------------------------------------------------------------------


def test_drop_init_on_2_products_uses_the_workspace(tmp_path):
    ws = _studio(tmp_path)
    result = CliRunner().invoke(app, ["drop", "init", "--path", str(ws.products)])
    assert result.exit_code == 0, result.output
    flat = "".join(result.output.split())  # Rich wraps long paths anywhere
    assert "Workspacereadyat" + "".join(str(ws.root).split()) in flat
    assert "using" in result.output
    assert not (ws.products / "1-MOCKUPS").exists()
    assert not (ws.products / "2-PRODUCTS").exists()


def test_drop_init_on_an_ordinary_folder_says_nothing_extra(tmp_path):
    target = tmp_path / "Studio"
    result = CliRunner().invoke(app, ["drop", "init", "--path", str(target)])
    assert result.exit_code == 0, result.output
    assert "using" not in result.output
    assert (target / "1-MOCKUPS").is_dir()


def test_drop_calibrate_run_from_inside_1_mockups_saves_in_the_workspace(tmp_path):
    ws = _studio(tmp_path)
    Image.new("RGB", (100, 100), "white").save(ws.mockups / "shirt.jpg")
    result = CliRunner().invoke(app, [
        "drop", "calibrate", "--path", str(ws.mockups),
        "--mockup", "shirt.jpg", "--area", "0.2,0.1,0.5,0.4",
    ])
    assert result.exit_code == 0, result.output
    assert mockup.load_positions(ws.positions_path)["shirt.jpg"] == mockup.PrintArea(
        0.2, 0.1, 0.5, 0.4)
    assert not (ws.mockups / "1-MOCKUPS").exists()


def test_doctor_path_on_2_products_checks_the_workspace(tmp_path, monkeypatch):
    # The same guard as the drop commands: 2-PRODUCTS means the workspace around it.
    from etsyotomasyon import setup as setup_mod

    ws = _studio(tmp_path)
    seen = []

    def check(root=None):
        seen.append(root)
        return setup_mod.StepResult(setup_mod.OK, f"workspace at {root}")

    monkeypatch.setattr(setup_mod, "check_workspace", check)
    result = CliRunner().invoke(app, ["doctor", "--path", str(ws.products)])
    assert seen == [ws.root], result.output
    assert "using" in result.output
    seen.clear()
    result = CliRunner().invoke(app, ["doctor", "--path", str(ws.root)])
    assert seen == [ws.root] and "using" not in result.output
    seen.clear()
    CliRunner().invoke(app, ["doctor"])
    assert seen == [None]


# --- a product folder with only its downloads ---------------------------------------------------


@pytest.mark.parametrize("sub", ["dosyalar", "files", "Dosyalar", "FILES"])
def test_a_folder_with_only_a_dosyalar_folder_is_listed_without_images(tmp_path, sub):
    ws = _studio(tmp_path)
    planner = ws.products / "boho planner"
    (planner / sub).mkdir(parents=True)
    (planner / sub / "planner.pdf").write_bytes(b"%PDF-1")
    empty = ws.products / "empty folder"
    empty.mkdir()
    other = ws.products / "notes"
    (other / "misc").mkdir(parents=True)
    assert [(path.name, images) for path, images in ws.product_groups()] == [
        ("boho planner", [])]


def test_the_workspace_readme_explains_the_dosyalar_folder(tmp_path):
    ws = _studio(tmp_path)
    text = (ws.root / "README.txt").read_text(encoding="utf-8")
    english, turkish = text.split("ETSY STUDIO (TR)")
    assert "dosyalar (or files)" in english and "at most 5" in english
    assert "dosyalar (ya da files)" in turkish and "en fazla 5" in turkish
