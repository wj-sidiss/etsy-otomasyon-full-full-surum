"""Mockuplar endpoints: the grid, uploads, catalog facts, print areas and the editor images."""

from __future__ import annotations

import io
import json

import pytest
from PIL import Image

from etsyotomasyon.drop import catalog, mockup
from etsyotomasyon.web.api import mockups as mockups_api


def png(size=(40, 30), colour=(200, 200, 200), mode="RGB") -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, size, colour if mode == "RGB" else (*colour, 0)).save(buffer, format="PNG")
    return buffer.getvalue()


def jpeg(size=(40, 30), orientation=None) -> bytes:
    buffer = io.BytesIO()
    image = Image.new("RGB", size, (180, 170, 160))
    if orientation:
        exif = Image.Exif()
        exif[mockup.ORIENTATION_TAG] = orientation
        image.save(buffer, format="JPEG", exif=exif.tobytes())
    else:
        image.save(buffer, format="JPEG")
    return buffer.getvalue()


def open_image(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


@pytest.fixture
def ws(web):
    return web.ctx.workspace()


def put(ws, name, data):
    ws.mockups.mkdir(parents=True, exist_ok=True)
    (ws.mockups / name).write_bytes(data)


def items_by_name(web):
    resp = web.client.get("/api/mockups")
    assert resp.status_code == 200, resp.text
    return {item["name"]: item for item in resp.json()["items"]}, resp.json()


# --- listing ---------------------------------------------------------------------------------


def test_empty_workspace_lists_nothing_and_needs_no_keys(web):
    resp = web.client.get("/api/mockups")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == [] and body["types"] == {} and body["limit_note"] is None
    assert body["counts"] == {"total": 0, "enabled": 0, "in_use": 0}
    assert (web.desktop / "Etsy Studio" / "1-MOCKUPS").is_dir()


def test_list_guesses_type_and_colour_and_reads_sizes(web, ws):
    put(ws, "tshirt-white.png", png((200, 200)))
    put(ws, "tshirt-black.png", png((200, 200)))
    put(ws, "kupa-beyaz.jpg", jpeg((250, 200)))
    put(ws, "notes.txt", b"not a mockup")
    items, body = items_by_name(web)
    assert list(items) == ["kupa-beyaz.jpg", "tshirt-black.png", "tshirt-white.png"]
    white = items["tshirt-white.png"]
    assert (white["type"], white["color"], white["enabled"]) == ("tshirt", "Beyaz", True)
    assert (white["width"], white["height"]) == (200, 200)
    assert white["path"] == "1-MOCKUPS/tshirt-white.png"
    assert white["area_source"] == "default"
    assert white["area"] == mockup.DEFAULT_PRINT_AREA.to_dict()
    assert white["same_size_count"] == 1 and items["kupa-beyaz.jpg"]["same_size_count"] == 0
    assert white["small"] is True and white["in_use"] is True and white["version"]
    assert items["kupa-beyaz.jpg"]["type"] == "mug"
    assert body["types"] == {"tshirt": 2, "mug": 1}
    assert body["type_order"][0] == "tshirt"


def test_list_reports_the_nineteen_mockup_limit(web, ws):
    for n in range(21):
        put(ws, f"m{n:02d}-tshirt.png", png())
    catalog.update(ws, "m00-tshirt.png", enabled=False)
    items, body = items_by_name(web)
    assert body["limit_note"] == {"enabled": 20, "max": catalog.MAX_ENABLED}
    assert body["counts"] == {"total": 21, "enabled": 20, "in_use": 19}
    assert items["m00-tshirt.png"]["in_use"] is False
    assert items["m01-tshirt.png"]["in_use"] is True and items["m20-tshirt.png"]["in_use"] is False


def test_a_broken_positions_file_does_not_break_the_grid(web, ws):
    put(ws, "tshirt-white.png", png())
    ws.positions_path.write_text("{not json", encoding="utf-8")
    items, body = items_by_name(web)
    assert body["positions_error"]
    assert items["tshirt-white.png"]["area_source"] == "default"


def test_orphan_positions_are_reported(web, ws):
    put(ws, "tshirt-white.png", png())
    mockup.save_positions(ws.positions_path, {"gone.png": mockup.PrintArea(0.1, 0.1, 0.5, 0.5)})
    _, body = items_by_name(web)
    assert body["orphans"] == ["gone.png"]


# --- upload ----------------------------------------------------------------------------------


def test_upload_saves_the_image_and_answers_with_the_item(web, ws):
    resp = web.client.put("/api/mockups/files", params={"name": "Mug White.png"},
                          content=png((1600, 1600)))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "Mug White.png"
    assert body["item"]["type"] == "mug" and body["item"]["color"] == "Beyaz"
    assert body["item"]["small"] is False
    assert (ws.mockups / "Mug White.png").is_file()


def test_upload_never_overwrites(web, ws):
    first = web.client.put("/api/mockups/files", params={"name": "shirt.png"}, content=png())
    second = web.client.put("/api/mockups/files", params={"name": "shirt.png"}, content=png())
    assert first.json()["name"] == "shirt.png"
    assert second.json()["name"] == "shirt-2.png"
    assert sorted(p.name for p in ws.mockup_files()) == ["shirt-2.png", "shirt.png"]


def test_upload_refuses_what_is_not_an_image(web, ws):
    resp = web.client.put("/api/mockups/files", params={"name": "fake.png"}, content=b"hello")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "bad_image"
    assert resp.json()["error"]["params"] == {"name": "fake.png"}
    resp = web.client.put("/api/mockups/files", params={"name": "notes.txt"}, content=b"hello")
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "bad_type"
    resp = web.client.put("/api/mockups/files", params={"name": "empty.png"}, content=b"")
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "bad_image"
    resp = web.client.put("/api/mockups/files", content=png())
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "invalid"
    assert ws.mockup_files() == []


def test_upload_keeps_a_path_out_of_the_name(web, ws, tmp_path):
    resp = web.client.put("/api/mockups/files", params={"name": "../../evil.png"}, content=png())
    assert resp.status_code == 200
    assert resp.json()["name"] == "evil.png"
    assert (ws.mockups / "evil.png").is_file()
    assert not (tmp_path / "evil.png").exists()


def test_upload_updates_the_setup_status(web, ws):
    web.client.put("/api/mockups/files", params={"name": "shirt.png"}, content=png())
    assert web.ctx.refresh_status(force=True)["setup"]["mockups"] == 1


# --- patch and delete ------------------------------------------------------------------------


def test_patch_changes_type_colour_and_enabled(web, ws):
    put(ws, "IMG_0001.png", png())
    resp = web.client.patch("/api/mockups/IMG_0001.png",
                            json={"type": "poster", "color": "Meşe çerçeve", "enabled": False})
    assert resp.status_code == 200, resp.text
    item = resp.json()["item"]
    assert (item["type"], item["color"], item["enabled"]) == ("poster", "Meşe çerçeve", False)
    stored = json.loads((ws.mockups / catalog.CATALOG_FILE).read_text(encoding="utf-8"))
    assert stored["IMG_0001.png"] == {"type": "poster", "color": "Meşe çerçeve", "enabled": False}


@pytest.mark.parametrize("body", [
    {"type": "spaceship"}, {"enabled": "yes"}, {"color": 5}, {}, {"color": "x" * 41},
])
def test_patch_refuses_bad_values(web, ws, body):
    put(ws, "tshirt.png", png())
    resp = web.client.patch("/api/mockups/tshirt.png", json=body)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid"


def test_unknown_mockup_is_not_found(web, ws):
    assert web.client.patch("/api/mockups/nope.png", json={"enabled": False}).status_code == 404
    assert web.client.delete("/api/mockups/nope.png").status_code == 404
    assert web.client.get("/api/mockups/nope.png/area").status_code == 404
    assert web.client.get("/api/mockups/nope.png/image").status_code == 404


def test_delete_removes_the_file_catalog_entry_and_print_area(web, ws):
    put(ws, "tshirt-white.png", png())
    put(ws, "mug.png", png())
    catalog.update(ws, "tshirt-white.png", color="Krem")
    catalog.save_area(ws, "tshirt-white.png", mockup.PrintArea(0.2, 0.2, 0.4, 0.4))
    catalog.save_area(ws, "mug.png", mockup.PrintArea(0.1, 0.1, 0.4, 0.4))
    resp = web.client.delete("/api/mockups/tshirt-white.png")
    assert resp.status_code == 200 and resp.json() == {"removed": "tshirt-white.png"}
    assert not (ws.mockups / "tshirt-white.png").exists()
    assert "tshirt-white.png" not in json.loads(ws.positions_path.read_text(encoding="utf-8"))
    assert "mug.png" in json.loads(ws.positions_path.read_text(encoding="utf-8"))
    stored = json.loads((ws.mockups / catalog.CATALOG_FILE).read_text(encoding="utf-8"))
    assert "tshirt-white.png" not in stored


# --- print areas -----------------------------------------------------------------------------


def test_area_save_and_same_size_apply(web, ws):
    put(ws, "a-tshirt-white.png", png((300, 300)))
    put(ws, "b-tshirt-black.png", png((300, 300)))
    put(ws, "c-tote-cream.png", png((300, 300)))
    put(ws, "d-mug.png", png((400, 300)))
    area = web.client.get("/api/mockups/a-tshirt-white.png/area").json()
    assert area["source"] == "default"
    assert area["same_size"] == ["b-tshirt-black.png", "c-tote-cream.png"]
    assert (area["width"], area["height"]) == (300, 300)

    resp = web.client.post("/api/mockups/a-tshirt-white.png/area",
                           json={"x": 0.34, "y": 0.26, "w": 0.32, "h": 0.37, "same_size": False})
    assert resp.status_code == 200, resp.text
    assert resp.json()["applied_to"] == ["a-tshirt-white.png"]
    items, _ = items_by_name(web)
    assert items["a-tshirt-white.png"]["area_source"] == "own"
    assert items["b-tshirt-black.png"]["area_source"] == "same_size"
    assert items["b-tshirt-black.png"]["area"] == {"x": 0.34, "y": 0.26, "w": 0.32, "h": 0.37}
    assert items["d-mug.png"]["area_source"] == "default"

    resp = web.client.post("/api/mockups/b-tshirt-black.png/area",
                           json={"x": 0.3, "y": 0.2, "w": 0.4, "h": 0.4, "same_size": True})
    assert resp.json()["applied_to"] == ["a-tshirt-white.png", "b-tshirt-black.png", "c-tote-cream.png"]
    positions = mockup.load_positions(ws.positions_path)
    assert set(positions) == {"a-tshirt-white.png", "b-tshirt-black.png", "c-tote-cream.png"}
    assert positions["c-tote-cream.png"] == mockup.PrintArea(0.3, 0.2, 0.4, 0.4)
    # The pipeline's own lookup agrees with what the screen shows.
    assert catalog.effective_area(ws, "c-tote-cream.png") == (
        mockup.PrintArea(0.3, 0.2, 0.4, 0.4), catalog.SOURCE_OWN)
    assert web.ctx.refresh_status(force=True)["setup"]["mockups_calibrated"] == 3


def test_area_rounding_is_forgiven_at_the_edge(web, ws):
    put(ws, "tshirt.png", png((300, 300)))
    resp = web.client.post("/api/mockups/tshirt.png/area",
                           json={"x": 0.6, "y": 0.5, "w": 0.40005, "h": 0.5})
    assert resp.status_code == 200, resp.text
    assert resp.json()["area"] == {"x": 0.6, "y": 0.5, "w": 0.4, "h": 0.5}


@pytest.mark.parametrize("body, code", [
    ({"x": 0.8, "y": 0.1, "w": 0.4, "h": 0.4}, "bad_area"),
    ({"x": 0.1, "y": 0.1, "w": 0, "h": 0.4}, "bad_area"),
    ({"x": -0.1, "y": 0.1, "w": 0.4, "h": 0.4}, "bad_area"),
    ({"x": "a", "y": 0.1, "w": 0.4, "h": 0.4}, "invalid"),
    ({"x": True, "y": 0.1, "w": 0.4, "h": 0.4}, "invalid"),
    ({"y": 0.1, "w": 0.4, "h": 0.4}, "invalid"),
    ({"x": 0.1, "y": 0.1, "w": 0.4, "h": 0.4, "same_size": "yes"}, "invalid"),
])
def test_area_refuses_bad_rectangles(web, ws, body, code):
    put(ws, "tshirt.png", png())
    resp = web.client.post("/api/mockups/tshirt.png/area", json=body)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == code
    assert not ws.positions_path.exists()


def test_clear_area_goes_back_to_the_default(web, ws):
    put(ws, "tshirt.png", png())
    catalog.save_area(ws, "tshirt.png", mockup.PrintArea(0.2, 0.2, 0.4, 0.4))
    resp = web.client.delete("/api/mockups/tshirt.png/area")
    assert resp.status_code == 200
    assert resp.json()["cleared"] is True and resp.json()["source"] == "default"
    assert web.client.delete("/api/mockups/tshirt.png/area").json()["cleared"] is False


# --- images ----------------------------------------------------------------------------------


def test_image_is_an_upright_jpeg_in_the_displayed_orientation(web, ws):
    # Stored 300x200 landscape, tagged "rotate 90": every viewer shows it 200x300 portrait.
    put(ws, "phone-case.jpg", jpeg((300, 200), orientation=6))
    items, _ = items_by_name(web)
    assert (items["phone-case.jpg"]["width"], items["phone-case.jpg"]["height"]) == (200, 300)
    resp = web.client.get("/api/mockups/phone-case.jpg/image", params={"max": 1400})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert open_image(resp.content).size == (200, 300)
    assert "max-age" not in resp.headers.get("cache-control", "")
    cached = web.client.get("/api/mockups/phone-case.jpg/image", params={"v": "1"})
    assert "max-age" in cached.headers["cache-control"]


def test_image_is_scaled_down_and_flattened_on_white(web, ws):
    put(ws, "tote.png", png((1000, 500), colour=(10, 10, 10), mode="RGBA"))
    resp = web.client.get("/api/mockups/tote.png/image", params={"max": 400})
    image = open_image(resp.content)
    assert image.size == (400, 200)
    assert image.getpixel((200, 100)) > (240, 240, 240)
    assert web.client.get("/api/mockups/tote.png/image", params={"max": 50}).status_code == 422


def test_preview_composites_the_sample_design(web, ws):
    put(ws, "tshirt.png", png((400, 400), colour=(40, 40, 40)))
    resp = web.client.get("/api/mockups/tshirt.png/preview",
                          params={"design": "sample", "x": 0.25, "y": 0.25, "w": 0.5, "h": 0.5,
                                  "max": 400})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "image/jpeg"
    image = open_image(resp.content).convert("RGB")
    assert image.size == (400, 400)
    assert image.getpixel((10, 10)) == pytest.approx((40, 40, 40), abs=6)  # outside: untouched
    r, g, b = image.getpixel((200, 140))  # inside, the sample's warm sky
    assert r > 150 and r > b
    # Without x/y/w/h the saved (here: default) area is used.
    assert web.client.get("/api/mockups/tshirt.png/preview").status_code == 200


def test_preview_uses_a_design_from_the_products_folder(web, ws):
    put(ws, "tshirt.png", png((200, 200)))
    art = Image.new("RGBA", (50, 50), (0, 0, 0, 0))
    art.paste((0, 0, 255, 255), (10, 10, 40, 40))
    art.save(ws.products / "blue-square.png")
    resp = web.client.get("/api/mockups/tshirt.png/preview",
                          params={"design": "blue-square.png", "x": 0, "y": 0, "w": 1, "h": 1})
    assert resp.status_code == 200
    r, g, b = open_image(resp.content).convert("RGB").getpixel((100, 100))
    assert b > 200 and r < 60


def test_preview_refuses_a_bad_area_or_design(web, ws, tmp_path):
    put(ws, "tshirt.png", png())
    bad = web.client.get("/api/mockups/tshirt.png/preview", params={"x": 0.9, "y": 0, "w": 0.5, "h": 1})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "bad_area"
    (tmp_path / "outside.png").write_bytes(png())
    for design in ("../../outside.png", "../1-MOCKUPS/tshirt.png", "C:/Windows/win.ini", "nope.png"):
        resp = web.client.get("/api/mockups/tshirt.png/preview", params={"design": design})
        assert resp.status_code == 404, design


def test_design_image_keeps_transparency(web, ws):
    resp = web.client.get("/api/mockups/design-image", params={"design": "sample", "max": 200})
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/png"
    image = open_image(resp.content)
    assert image.mode == "RGBA" and image.size == (200, 200)
    assert image.getpixel((0, 0))[3] == 0  # the corner outside the badge is see-through


def test_designs_list_prefers_a_transparent_design(web, ws):
    Image.new("RGB", (40, 40), (255, 255, 255)).save(ws.products / "a-photo.jpg")
    Image.new("RGBA", (40, 40), (0, 0, 0, 0)).save(ws.products / "retro-mountain-sunset.png")
    Image.new("RGBA", (40, 40), (0, 0, 0, 0)).save(ws.products / "retro-mountain-sunset-preview.png")
    body = web.client.get("/api/mockups/designs").json()
    assert [d["id"] for d in body["items"]] == ["a-photo.jpg", "retro-mountain-sunset.png"]
    assert body["default"] == "retro-mountain-sunset.png"
    sunset = body["items"][1]
    assert sunset["label"] == "Retro Mountain Sunset"
    assert sunset["path"] == "2-PRODUCTS/retro-mountain-sunset.png"


def test_designs_list_falls_back_to_the_sample(web, ws):
    body = web.client.get("/api/mockups/designs").json()
    assert body == {"items": [], "default": "sample", "sample": "sample"}


def test_path_traversal_in_the_name_is_refused(web, ws, tmp_path):
    put(ws, "tshirt.png", png())
    (ws.root / "secret.png").write_bytes(png())
    for raw in ("..%2Fsecret.png", "..%5Csecret.png", "%2E%2E", "tshirt.png%00"):
        for suffix in ("/image", "/area", "/preview"):
            resp = web.client.get(f"/api/mockups/{raw}{suffix}")
            assert resp.status_code == 404, (raw, suffix)
        assert web.client.delete(f"/api/mockups/{raw}").status_code == 404
    assert (ws.root / "secret.png").is_file()
    assert (ws.mockups / "tshirt.png").is_file()


def test_the_sample_design_is_drawn_once(web, monkeypatch):
    first = mockups_api.sample_design()
    stamp = first.stat().st_mtime_ns
    monkeypatch.setattr(mockups_api, "_draw_sample", lambda size=1200: pytest.fail("drawn twice"))
    assert mockups_api.sample_design() == first
    assert first.stat().st_mtime_ns == stamp


# --- which mockups drafts use, and in which order (FIXLIST 9, 10) ----------------------------


def test_list_follows_the_saved_order_and_numbers_the_images(web, ws):
    for name in ("a-tshirt-white.png", "b-tshirt-black.png", "c-mug.png"):
        put(ws, name, png())
    catalog.arrange(ws, order=["c-mug.png", "a-tshirt-white.png"],
                    enabled=["c-mug.png", "b-tshirt-black.png"])
    _, body = items_by_name(web)
    rows = [(i["name"], i["order"], i["position"], i["enabled"], i["over_limit"])
            for i in body["items"]]
    assert rows == [
        ("c-mug.png", 0, 1, True, False),  # the main image
        ("a-tshirt-white.png", 1, None, False, False),
        ("b-tshirt-black.png", 2, 2, True, False),
    ]
    assert body["usage"] == {"used": ["c-mug.png", "b-tshirt-black.png"], "over_limit": [],
                             "left_out": [], "enabled": 2, "total": 3,
                             "max": catalog.MAX_ENABLED, "info": 0}


def test_thirty_seven_colour_mockups_show_exactly_which_are_used(web, ws):
    # The v0.2.0 tester: 37 mockups of one style in different colours.
    for n in range(37):
        put(ws, f"tee-{n:02d}.png", png((300, 300)))
    _, body = items_by_name(web)
    assert body["counts"] == {"total": 37, "enabled": 37, "in_use": 19}
    assert body["limit_note"] == {"enabled": 37, "max": 19}
    used = [i["name"] for i in body["items"] if i["position"]]
    assert used == [f"tee-{n:02d}.png" for n in range(19)]
    assert [i["position"] for i in body["items"][:19]] == list(range(1, 20))
    assert all(i["over_limit"] and not i["in_use"] for i in body["items"][19:])
    assert body["usage"]["over_limit"] == [f"tee-{n:02d}.png" for n in range(19, 37)]
    # All 37 still put the design in the default area; one save fixes them together.
    assert body["default_area"] == {"count": 37, "used": 19, "first": "tee-00.png",
                                    "first_same_size": 36}


def test_arrange_saves_order_and_selection_in_one_write(web, ws):
    names = [f"tee-{n:02d}.png" for n in range(25)]
    for name in names:
        put(ws, name, png())
    wanted = ["tee-24.png", "tee-03.png", "tee-07.png"]
    order = [*wanted, *(n for n in names if n not in wanted)]
    resp = web.client.post("/api/mockups/arrange", json={"order": order, "enabled": wanted})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["usage"]["used"] == wanted and body["counts"]["in_use"] == 3
    assert [i["name"] for i in body["items"][:3]] == wanted
    assert body["items"][0]["position"] == 1 and body["limit_note"] is None
    # The draft run takes exactly these, the first as the main image.
    assert [p.name for p in catalog.enabled_mockups(ws)] == wanted
    stored = json.loads((ws.mockups / catalog.CATALOG_FILE).read_text(encoding="utf-8"))
    assert stored["tee-24.png"]["order"] == 0 and stored["tee-00.png"]["enabled"] is False


def test_arrange_order_only_keeps_the_selection(web, ws):
    for name in ("a.png", "b.png", "c.png"):
        put(ws, name, png())
    catalog.update(ws, "b.png", enabled=False)
    resp = web.client.post("/api/mockups/arrange", json={"order": ["c.png"]})
    assert resp.status_code == 200
    assert [i["name"] for i in resp.json()["items"]] == ["c.png", "a.png", "b.png"]
    assert resp.json()["usage"]["used"] == ["c.png", "a.png"]


@pytest.mark.parametrize("body, status, code", [
    ({}, 422, "invalid"),
    ({"order": "a.png"}, 422, "invalid"),
    ({"enabled": [1, 2]}, 422, "invalid"),
    ({"order": ["a.png", "a.png"]}, 422, "invalid"),
    ({"order": ["a.png", "../product.json"]}, 404, "not_found"),
    ({"enabled": ["gone.png"]}, 404, "not_found"),
])
def test_arrange_refuses_bad_lists_and_saves_nothing(web, ws, body, status, code):
    put(ws, "a.png", png())
    put(ws, "b.png", png())
    resp = web.client.post("/api/mockups/arrange", json=body)
    assert resp.status_code == status
    assert resp.json()["error"]["code"] == code
    assert not (ws.mockups / catalog.CATALOG_FILE).exists()


def test_arrange_needs_the_write_header(web, ws):
    put(ws, "a.png", png())
    resp = web.anonymous().post("/api/mockups/arrange", json={"enabled": []})
    assert resp.status_code in (401, 403)
    resp = web.client.post("/api/mockups/arrange", json={"enabled": []},
                           headers={"X-Etsyotomasyon": "0"})
    assert resp.status_code == 403
    assert catalog.load(ws)["a.png"].enabled is True


def test_default_area_banner_prefers_a_used_mockup(web, ws):
    put(ws, "a-poster.png", png((400, 300)))
    put(ws, "b-tee.png", png((300, 300)))
    put(ws, "c-tee.png", png((300, 300)))
    catalog.update(ws, "a-poster.png", enabled=False)
    _, body = items_by_name(web)
    assert body["default_area"] == {"count": 3, "used": 2, "first": "b-tee.png",
                                    "first_same_size": 1}
    web.client.post("/api/mockups/b-tee.png/area",
                    json={"x": 0.3, "y": 0.2, "w": 0.4, "h": 0.4, "same_size": True})
    _, body = items_by_name(web)
    assert body["default_area"] == {"count": 1, "used": 0, "first": "a-poster.png",
                                    "first_same_size": 0}
    catalog.save_area(ws, "a-poster.png", mockup.PrintArea(0.1, 0.1, 0.5, 0.5))
    _, body = items_by_name(web)
    assert body["default_area"] == {"count": 0, "used": 0, "first": None, "first_same_size": 0}


def test_patch_enabled_keeps_the_order(web, ws):
    for name in ("a.png", "b.png"):
        put(ws, name, png())
    catalog.arrange(ws, order=["b.png", "a.png"])
    web.client.patch("/api/mockups/b.png", json={"enabled": False})
    web.client.patch("/api/mockups/b.png", json={"enabled": True})
    items = web.client.get("/api/mockups").json()["items"]
    assert [i["name"] for i in items] == ["b.png", "a.png"] and items[0]["position"] == 1


# --- pixel limits (review finding design-pixel-bomb-500) -------------------------------------


def big_png(size) -> bytes:
    # 1-bit: small on disk, tens of millions of pixels once decoded.
    buffer = io.BytesIO()
    Image.new("1", size).save(buffer, format="PNG")
    return buffer.getvalue()


def test_upload_refuses_too_many_pixels(web, ws):
    resp = web.client.put("/api/mockups/files", params={"name": "huge.png"},
                          content=big_png((8000, 8000)))
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "too_many_pixels"
    assert err["params"] == {"name": "huge.png", "width": 8000, "height": 8000,
                             "max_edge": catalog.MAX_EDGE, "max_mp": 60}
    resp = web.client.put("/api/mockups/files", params={"name": "strip.png"},
                          content=big_png((12500, 20)))
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "too_many_pixels"
    assert ws.mockup_files() == []


def test_oversize_mockup_already_in_the_folder_answers_422_not_500(web, ws):
    put(ws, "huge.png", big_png((8000, 8000)))
    for path in ("/api/mockups/huge.png/image", "/api/mockups/huge.png/preview"):
        resp = web.client.get(path)
        assert resp.status_code == 422, path
        assert resp.json()["error"]["code"] == "too_many_pixels"
    resp = web.client.get("/api/files/thumb", params={"path": "1-MOCKUPS/huge.png", "w": 600})
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "too_many_pixels"
    # The grid still lists it: only the file header is read there.
    items, _ = items_by_name(web)
    assert (items["huge.png"]["width"], items["huge.png"]["height"]) == (8000, 8000)


def test_pillows_own_bomb_refusal_is_422_too(web, ws, monkeypatch):
    put(ws, "tee.png", png((300, 300)))
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    resp = web.client.get("/api/files/thumb", params={"path": "1-MOCKUPS/tee.png", "w": 200})
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "too_many_pixels"
    resp = web.client.get("/api/mockups/tee.png/image")
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "too_many_pixels"


def test_a_memory_error_while_decoding_is_422_not_500(web, ws, monkeypatch):
    put(ws, "tee.png", png((300, 300)))

    def boom(image, colour):
        raise MemoryError

    monkeypatch.setattr(mockup, "flatten_onto", boom)
    resp = web.client.get("/api/mockups/tee.png/image")
    assert resp.status_code == 422
    assert resp.json()["error"]["params"]["width"] == 300
    resp = web.client.get("/api/files/thumb", params={"path": "1-MOCKUPS/tee.png", "w": 200})
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "too_many_pixels"


def test_a_huge_jpeg_still_gets_a_thumbnail(web, ws):
    # libjpeg decodes at 1/8: 9000x9000 becomes 1125x1125, well inside the limits.
    buffer = io.BytesIO()
    Image.new("RGB", (9000, 9000), (120, 130, 140)).save(buffer, format="JPEG", quality=30)
    put(ws, "big-photo.jpg", buffer.getvalue())
    resp = web.client.get("/api/files/thumb", params={"path": "1-MOCKUPS/big-photo.jpg", "w": 400})
    assert resp.status_code == 200
    assert open_image(resp.content).size == (400, 400)


# --- four corners, realism and curve -------------------------------------------------------------


QUAD = [[0.2, 0.25], [0.8, 0.2], [0.75, 0.8], [0.25, 0.85]]


def test_area_with_four_corners_and_a_realism_is_saved_for_the_same_size_ones(web, ws):
    put(ws, "a-tshirt-white.png", png((300, 300)))
    put(ws, "b-tote-cream.png", png((300, 300)))
    put(ws, "c-mug.png", png((400, 300)))
    resp = web.client.post("/api/mockups/a-tshirt-white.png/area",
                           json={"quad": QUAD, "realism": 30, "curve": None, "same_size": True})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied_to"] == ["a-tshirt-white.png", "b-tote-cream.png"]
    assert body["area"]["quad"] == QUAD and body["area"]["realism"] == 30
    assert "curve" not in body["area"]  # null: the type's default
    assert (body["area"]["x"], body["area"]["w"]) == (0.2, 0.6)
    assert body["style"] == {"kind": "tshirt", "realism": 30, "curve": 0, "realism_default": 65,
                             "curve_default": 0, "curve_offered": False}
    stored = mockup.load_positions(ws.positions_path)
    assert stored["b-tote-cream.png"].quad == tuple(tuple(p) for p in QUAD)
    assert stored["b-tote-cream.png"].realism == 30
    mug = web.client.get("/api/mockups/c-mug.png/area").json()
    assert mug["style"]["realism"] == mockup.REALISM_DEFAULTS["mug"]
    assert mug["style"]["curve"] == mockup.CURVE_DEFAULTS["mug"] and mug["style"]["curve_offered"]
    # Back to a rectangle: the corners go, the settings sent stay.
    resp = web.client.post("/api/mockups/a-tshirt-white.png/area",
                           json={"x": 0.3, "y": 0.2, "w": 0.4, "h": 0.5, "realism": 0, "curve": 0})
    assert resp.json()["area"] == {"x": 0.3, "y": 0.2, "w": 0.4, "h": 0.5, "realism": 0, "curve": 0}


@pytest.mark.parametrize("body, code", [
    ({"quad": [[0.2, 0.2], [0.8, 0.8], [0.8, 0.2], [0.2, 0.8]]}, "bad_area"),  # twisted
    ({"quad": [[0.2, 0.2], [0.5, 0.5], [0.8, 0.8], [0.2, 0.8]]}, "bad_area"),  # flattened
    ({"quad": [[0.2, 0.2], [1.3, 0.2], [0.8, 0.8], [0.2, 0.8]]}, "bad_area"),  # off the edge
    ({"quad": [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8]]}, "invalid"),
    ({"quad": [[0.2, 0.2], [0.8, "a"], [0.8, 0.8], [0.2, 0.8]]}, "invalid"),
    ({"quad": {"tl": [0.2, 0.2]}}, "invalid"),
    ({"x": 0.1, "y": 0.1, "w": 0.4, "h": 0.4, "realism": 150}, "invalid"),
    ({"x": 0.1, "y": 0.1, "w": 0.4, "h": 0.4, "realism": "lots"}, "invalid"),
    ({"x": 0.1, "y": 0.1, "w": 0.4, "h": 0.4, "curve": True}, "invalid"),
    ({"x": 0.1, "y": 0.1, "w": 0.4, "h": 0.4, "curve": -5}, "invalid"),
])
def test_area_refuses_bad_corners_and_settings(web, ws, body, code):
    put(ws, "tshirt.png", png())
    resp = web.client.post("/api/mockups/tshirt.png/area", json=body)
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == code
    assert not ws.positions_path.exists()


def test_preview_draws_four_corners_with_the_settings_asked_for(web, ws):
    put(ws, "mug.png", png((400, 400), colour=(240, 240, 240)))
    quad = ",".join(str(v) for point in QUAD for v in point)
    resp = web.client.get("/api/mockups/mug.png/preview",
                          params={"design": "sample", "quad": quad, "realism": 60, "curve": 40,
                                  "max": 400})
    assert resp.status_code == 200, resp.text
    image = open_image(resp.content).convert("RGB")
    assert image.getpixel((5, 5)) == pytest.approx((240, 240, 240), abs=6)
    r, g, b = image.getpixel((200, 140))
    assert r > 150 and r > b
    for params in ({"quad": "0.1,0.1,0.9"}, {"quad": "0.2,0.2,0.8,0.8,0.8,0.2,0.2,0.8"},
                   {"realism": 101}):
        bad = web.client.get("/api/mockups/mug.png/preview", params=params)
        assert bad.status_code == 422, params
    # Only a realism: the saved (here: default) area with it.
    assert web.client.get("/api/mockups/mug.png/preview", params={"realism": 0}).status_code == 200
