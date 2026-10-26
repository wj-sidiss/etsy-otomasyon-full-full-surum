"""Print files: every listing's design kept under its Etsy listing id (drop.printfiles).

All files are invented and live in tmp_path.
"""

from __future__ import annotations

import pytest
from PIL import Image

from etsyotomasyon.drop import printfiles
from etsyotomasyon.drop.workspace import Workspace
from etsyotomasyon.errors import ValidationError


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path / "studio").create()


def png(path, size=(40, 20)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, "white").save(path)
    return path


def test_a_loose_design_is_kept_byte_for_byte_under_the_listing_id(ws):
    design = png(ws.products / "leopard jungle.png")
    kept = printfiles.keep(ws.root, 4567957350, design)
    target = ws.root / "print-files" / "4567957350" / "leopard jungle.png"
    assert kept == [target]
    assert target.read_bytes() == design.read_bytes()
    assert printfiles.files(ws.root, 4567957350) == [target]
    assert printfiles.listing_ids(ws.root) == {4567957350}


def test_keeping_again_copies_nothing_and_another_file_of_that_name_gets_a_number(ws):
    design = png(ws.products / "lemon.png")
    printfiles.keep(ws.root, 1001, design)
    printfiles.keep(ws.root, 1001, design)
    assert [p.name for p in printfiles.files(ws.root, 1001)] == ["lemon.png"]
    png(design, size=(80, 20))  # a different file under the same name
    printfiles.keep(ws.root, 1001, design)
    assert [p.name for p in printfiles.files(ws.root, 1001)] == ["lemon (2).png", "lemon.png"]


def test_a_product_folder_keeps_its_dosyalar_and_ready_photos_alone_keep_nothing(ws):
    folder = ws.products / "planner"
    png(folder / "01.jpg")
    png(folder / "dosyalar" / "planner-a4.png")
    (folder / "dosyalar" / "planner-a4.pdf").write_bytes(b"%PDF-1.4 example")
    kept = printfiles.keep(ws.root, 1002, folder)
    assert [p.name for p in kept] == ["planner-a4.pdf", "planner-a4.png"]
    photos = ws.products / "camp-set"
    png(photos / "01.jpg")
    assert printfiles.keep(ws.root, 1003, photos) == []
    assert printfiles.listing_ids(ws.root) == {1002}


def test_a_copy_that_fails_never_raises(ws, monkeypatch):
    design = png(ws.products / "sun.png")

    def broken(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(printfiles.shutil, "copy2", broken)
    assert printfiles.keep(ws.root, 1004, design) == []
    assert printfiles.files(ws.root, 1004) == []
    # The half-written file was cleaned up and never counts.
    assert printfiles.listing_ids(ws.root) == set()


def test_only_id_folders_holding_a_finished_file_count(ws):
    base = ws.root / "print-files"
    (base / "1005").mkdir(parents=True)  # empty
    (base / "notes").mkdir()
    png(base / "notes" / "x.png")
    (base / "1006").mkdir()
    (base / "1006" / ".part-big.tif").write_bytes(b"half")
    (base / "1006" / "desktop.ini").write_text("x")
    png(base / "1007" / "tile.png")
    assert printfiles.listing_ids(ws.root) == {1007}


def test_add_saves_a_file_and_knows_the_same_one(ws):
    data = (ws.products / "x.png")
    png(data)
    raw = data.read_bytes()
    path, duplicate = printfiles.add(ws.root, 1008, "C:\\Users\\me\\leopard tile.png", raw)
    assert (path.name, duplicate) == ("leopard tile.png", False)
    path, duplicate = printfiles.add(ws.root, 1008, "leopard tile.png", raw)
    assert (path.name, duplicate) == ("leopard tile.png", True)
    path, duplicate = printfiles.add(ws.root, 1008, "leopard tile.png", raw + b"x")
    assert (path.name, duplicate) == ("leopard tile (2).png", False)


@pytest.mark.parametrize("name", ["setup.exe", "run.bat", "notes", "", "..\\..\\evil.py"])
def test_add_refuses_what_is_not_a_print_file(ws, name):
    with pytest.raises(ValidationError):
        printfiles.add(ws.root, 1009, name, b"data")
    assert printfiles.files(ws.root, 1009) == []


def test_backfill_keeps_designs_still_in_products_or_archive(ws):
    png(ws.products / "a.png")
    png(ws.archive / "b.png")
    png(ws.root / "print-files" / "3" / "own.png")
    done = printfiles.backfill(ws.root, {1: "a.png", 2: "b.png", 3: "c.png", 4: "gone.png",
                                         5: "../a.png"})
    assert done == 2
    assert printfiles.listing_ids(ws.root) == {1, 2, 3}
    assert [p.name for p in printfiles.files(ws.root, 3)] == ["own.png"]


def test_describe_gives_pixels_and_whether_a_thumbnail_can_be_made(ws):
    path = png(ws.root / "print-files" / "1010" / "tile.png", size=(345, 2400))
    pdf = ws.root / "print-files" / "1010" / "tile.pdf"
    pdf.write_bytes(b"%PDF-1.4 example")
    info = printfiles.describe(ws.root, path)
    assert (info["name"], info["rel"], info["width"], info["height"]) == (
        "tile.png", "print-files/1010/tile.png", 345, 2400)
    assert info["image"] is True and info["thumb"] is True and info["size"] == path.stat().st_size
    other = printfiles.describe(ws.root, pdf)
    assert (other["image"], other["thumb"], other["width"]) == (False, False, None)
