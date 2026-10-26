"""Şablon İlan's info images: /api/info-images*, and what they change elsewhere.

A fake Etsy serves the template listing's photos and Etsy's image CDN. Invented data
only (ExampleShop, template listing 1000013, pictures 4401...).
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import httpx
import pytest
from PIL import Image
from web_helpers import use_fake_etsy

from etsyotomasyon.drop import infoimages
from etsyotomasyon.drop.template import Template
from etsyotomasyon.drop.workspace import INFO_DIR
from etsyotomasyon.web.api import infoimages as info_api
from etsyotomasyon.web.api import listings as listings_api

TEMPLATE_ID = 1000013
STATIC = Path(info_api.__file__).resolve().parents[1] / "static"


def picture(fmt="JPEG", colour=(30, 90, 160), size=(60, 40)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format=fmt)
    return buffer.getvalue()


def cdn_path(image_id: int) -> str:
    return f"/12345678/r/il/0a1b2c/{image_id}/il_fullxfull.{image_id}_ab12.jpg"


def photo(image_id: int, rank: int, alt: str = "") -> dict:
    full = f"https://i.etsystatic.com{cdn_path(image_id)}"
    return {
        "listing_id": TEMPLATE_ID, "listing_image_id": image_id, "rank": rank,
        "url_75x75": full.replace("fullxfull", "75x75"),
        "url_170x135": full.replace("fullxfull", "170x135"),
        "url_570xN": full.replace("fullxfull", "570xN"),
        "url_fullxfull": full, "full_width": 3000, "full_height": 2000, "alt_text": alt,
    }


PHOTOS = [
    photo(4400, 1, "Retro mountain wallpaper in a living room"),
    photo(4401, 2, "Materials: peel and stick vinyl"),
    photo(4402, 3, "Roll size 52 x 300 cm"),
    photo(4403, 4, ""),
]
BYTES = {4400: picture(colour=(1, 2, 3)), 4401: picture(colour=(200, 40, 40)),
         4402: picture(colour=(40, 200, 40)), 4403: picture("PNG", colour=(40, 40, 200))}


@pytest.fixture
def ws(web):
    return web.ctx.workspace()


@pytest.fixture
def shop(web, ws):
    """Connected, with a saved template listing whose photos Etsy serves."""
    fake = use_fake_etsy(web)
    template = Template(TEMPLATE_ID, fields={
        "taxonomy_id": 482, "price": 24.9, "quantity": 999, "who_made": "i_did",
        "when_made": "made_to_order", "type": "physical", "shipping_profile_id": 501,
    }, source_title="Retro Mountain Wallpaper")
    ws.write_template(template.to_dict())
    fake.add("GET", f"/listings/{TEMPLATE_ID}/images", {"count": len(PHOTOS), "results": PHOTOS})
    for image_id, data in BYTES.items():
        fake.add("GET", cdn_path(image_id), httpx.Response(200, content=data))
    return fake


def cdn_calls(fake) -> list[str]:
    return [path for method, path in fake.calls if path.startswith("/12345678/r/il/")]


# --------------------------------------------------------------------------- reading


def test_nothing_yet_and_no_etsy_needed(web):
    resp = web.client.get("/api/info-images")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["items"] == [] and body["count"] == 0 and body["unused"] == []
    assert body["max"] == 10 and body["images_max"] == 20 and body["mockup_max"] == 19
    assert body["template_listing_id"] is None


def test_without_a_template_there_is_nothing_to_tick(web):
    use_fake_etsy(web)
    body = web.client.get("/api/info-images/source").json()
    assert body["problem"] == "no_template" and body["photos"] == []
    resp = web.client.post("/api/info-images/etsy", json={"listing_image_ids": [4401]})
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "no_template"


def test_the_template_listings_photos_come_in_etsys_order(web, shop):
    body = web.client.get("/api/info-images/source").json()
    assert body["listing_id"] == TEMPLATE_ID and body["problem"] is None
    assert [p["listing_image_id"] for p in body["photos"]] == [4400, 4401, 4402, 4403]
    first = body["photos"][1]
    assert first["alt"] == "Materials: peel and stick vinyl" and first["picked"] is None
    assert first["thumb"].endswith("il_170x135.4401_ab12.jpg") and first["url"].startswith("https://")
    assert body["state"]["template_listing_id"] == TEMPLATE_ID
    assert cdn_calls(shop) == []  # nothing is downloaded to show them


def test_the_source_needs_a_connected_shop(web, ws):
    ws.write_template(Template(TEMPLATE_ID, fields={"price": 1}).to_dict())
    resp = web.client.get("/api/info-images/source")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "setup_needed"


# --------------------------------------------------------------------------- Etsy photos


def test_ticked_photos_are_copied_full_size_in_the_order_asked(web, ws, shop):
    resp = web.client.post("/api/info-images/etsy", json={"listing_image_ids": [4402, 4401]})
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [i["name"] for i in items] == ["etsy-4402.jpg", "etsy-4401.jpg"]
    assert items[1] == {**items[1], "alt": "Materials: peel and stick vinyl", "source": "etsy",
                        "listing_id": TEMPLATE_ID, "listing_image_id": 4401,
                        "path": "info-images/etsy-4401.jpg", "width": 60, "height": 40}
    assert (ws.root / INFO_DIR / "etsy-4401.jpg").read_bytes() == BYTES[4401]
    # Etsy's CDN, never with the keys or the sign-in.
    cdn = [r for r in shop.requests if r.url.host == "i.etsystatic.com"]
    assert len(cdn) == 2 and not any("x-api-key" in r.headers or "authorization" in r.headers
                                     for r in cdn)
    source = web.client.get("/api/info-images/source").json()
    assert {p["listing_image_id"]: p["picked"] for p in source["photos"]} == {
        4400: None, 4401: "etsy-4401.jpg", 4402: "etsy-4402.jpg", 4403: None}


def test_a_photo_already_there_is_not_copied_again(web, shop):
    web.client.post("/api/info-images/etsy", json={"listing_image_ids": [4401]})
    before = len(cdn_calls(shop))
    body = web.client.post("/api/info-images/etsy",
                           json={"listing_image_ids": [4401, 4403]}).json()
    assert [i["name"] for i in body["items"]] == ["etsy-4401.jpg", "etsy-4403.png"]
    assert len(cdn_calls(shop)) == before + 1


def test_a_photo_no_longer_on_the_listing_is_said_so(web, ws, shop):
    resp = web.client.post("/api/info-images/etsy", json={"listing_image_ids": [4401, 9999]})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "photo_gone"
    assert resp.json()["error"]["params"] == {"listing_image_id": 9999}
    assert infoimages.load(ws) == [] and cdn_calls(shop) == []


def test_a_photo_address_off_etsys_cdn_is_never_fetched(web, ws, shop):
    odd = dict(PHOTOS[1], url_fullxfull="https://example.com/wallpaper.jpg")
    shop.add("GET", f"/listings/{TEMPLATE_ID}/images", {"count": 1, "results": [odd]})
    resp = web.client.post("/api/info-images/etsy", json={"listing_image_ids": [4401]})
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "bad_image"
    assert infoimages.load(ws) == []
    assert not any(r.url.host == "example.com" for r in shop.requests)


def test_a_download_that_fails_keeps_nothing(web, ws, shop):
    shop.add("GET", cdn_path(4402), httpx.Response(404))
    resp = web.client.post("/api/info-images/etsy", json={"listing_image_ids": [4401, 4402]})
    assert resp.status_code == 404
    assert infoimages.load(ws) == []


@pytest.mark.parametrize("body", [{}, {"listing_image_ids": []}, {"listing_image_ids": ["4401"]},
                                  {"listing_image_ids": [True]}, {"listing_image_ids": 4401}])
def test_the_picture_numbers_are_checked(web, shop, body):
    resp = web.client.post("/api/info-images/etsy", json=body)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "invalid"


def test_at_most_ten(web, ws, shop):
    for n in range(9):
        infoimages.add(ws, f"card-{n}.jpg", picture())
    resp = web.client.post("/api/info-images/etsy", json={"listing_image_ids": [4401, 4402]})
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "too_many_info_images" and error["params"] == {"max": 10, "n": 11}
    assert infoimages.count(ws) == 9 and cdn_calls(shop) == []
    too_long = web.client.post("/api/info-images/etsy",
                               json={"listing_image_ids": list(range(1, 12))})
    assert too_long.json()["error"]["code"] == "too_many_info_images"


# --------------------------------------------------------------------------- own files


def test_a_picture_from_the_computer_is_added_at_the_end(web, ws, shop):
    web.client.post("/api/info-images/etsy", json={"listing_image_ids": [4401]})
    resp = web.client.put("/api/info-images/files", params={"name": "Ölçü alma.webp"},
                          content=picture("WEBP"))
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [i["name"] for i in items] == ["etsy-4401.jpg", "Ölçü alma.jpg"]
    assert items[1]["source"] == "file" and items[1]["listing_image_id"] is None
    thumb = web.client.get("/api/files/thumb", params={"path": items[1]["path"], "w": 160})
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/jpeg"


@pytest.mark.parametrize("name, data, code", [
    ("notes.txt", picture(), "bad_type"),
    ("card.png", b"not a picture", "bad_image"),
    ("card.png", b"", "bad_image"),
    ("", picture(), "invalid"),
])
def test_what_is_not_a_picture_is_refused(web, ws, name, data, code):
    resp = web.client.put("/api/info-images/files", params={"name": name}, content=data)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == code
    assert infoimages.load(ws) == []


def test_a_picture_too_large_to_decode_is_refused(web, ws, monkeypatch):
    from etsyotomasyon.drop import catalog

    monkeypatch.setattr(catalog, "MAX_PIXELS", 100)
    resp = web.client.put("/api/info-images/files", params={"name": "big.png"},
                          content=picture("PNG"))
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "too_many_pixels"


# --------------------------------------------------------------------------- order, removal


def test_the_order_is_saved_and_a_picture_taken_out_goes(web, ws, shop):
    web.client.post("/api/info-images/etsy", json={"listing_image_ids": [4401, 4402]})
    web.client.put("/api/info-images/files", params={"name": "care.png"}, content=picture("PNG"))
    body = web.client.post("/api/info-images/order",
                           json={"names": ["care.png", "etsy-4402.jpg", "etsy-4401.jpg"]}).json()
    assert [i["name"] for i in body["items"]] == ["care.png", "etsy-4402.jpg", "etsy-4401.jpg"]
    assert body["items"][2]["alt"] == "Materials: peel and stick vinyl"
    body = web.client.delete("/api/info-images/etsy-4402.jpg").json()
    assert [i["name"] for i in body["items"]] == ["care.png", "etsy-4401.jpg"]
    assert not (ws.root / INFO_DIR / "etsy-4402.jpg").exists()
    body = web.client.delete("/api/info-images/care.png").json()
    assert (ws.archive / INFO_DIR / "care.png").is_file()  # the seller's own: archived
    assert [i["name"] for i in body["items"]] == ["etsy-4401.jpg"]


@pytest.mark.parametrize("names, status, code", [
    (["etsy-4401.jpg", "gone.jpg"], 404, "not_found"),
    (["etsy-4401.jpg", "etsy-4401.jpg"], 422, "invalid"),
    ("etsy-4401.jpg", 422, "invalid"),
])
def test_an_order_naming_what_is_not_there_changes_nothing(web, ws, shop, names, status, code):
    web.client.post("/api/info-images/etsy", json={"listing_image_ids": [4401, 4402]})
    resp = web.client.post("/api/info-images/order", json={"names": names})
    assert resp.status_code == status and resp.json()["error"]["code"] == code
    assert [i.name for i in infoimages.load(ws)] == ["etsy-4401.jpg", "etsy-4402.jpg"]


def test_removing_what_is_not_there_is_404(web):
    resp = web.client.delete("/api/info-images/nothing.jpg")
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "not_found"


def test_pictures_past_the_limit_stay_when_the_order_changes(web, ws):
    folder = ws.root / INFO_DIR
    folder.mkdir(parents=True, exist_ok=True)
    for n in range(11):
        (folder / f"{n + 1:02d}.jpg").write_bytes(picture())
    body = web.client.get("/api/info-images").json()
    assert body["count"] == 10 and body["unused"] == ["11.jpg"]
    names = [i["name"] for i in body["items"]]
    body = web.client.post("/api/info-images/order", json={"names": names[::-1]}).json()
    assert [i["name"] for i in body["items"]] == names[::-1]
    assert body["unused"] == ["11.jpg"] and (folder / "11.jpg").is_file()


# --------------------------------------------------------------------------- elsewhere


def test_the_mockups_page_counts_their_room(web, ws):
    for n in range(19):
        (ws.mockups / f"m{n:02d}-tshirt.png").write_bytes(picture("PNG"))
    for n in range(3):
        infoimages.add(ws, f"card-{n}.jpg", picture())
    body = web.client.get("/api/mockups").json()
    assert body["max_enabled"] == 16 and body["info_images"] == 3 and body["images_max"] == 20
    assert body["counts"] == {"total": 19, "enabled": 19, "in_use": 16}
    assert body["limit_note"] == {"enabled": 19, "max": 16}
    state = web.client.get("/api/info-images").json()
    assert state["mockup_max"] == 16
    assert state["mockups"] == {"enabled": 19, "used": 16, "over_limit": 3}


def test_the_start_card_counts_them_in_every_draft(web, ws, shop):
    for n in range(3):
        (ws.mockups / f"m{n}-tshirt.png").write_bytes(picture("PNG"))
    Image.new("RGBA", (40, 40), (0, 0, 0, 0)).save(ws.products / "retro-mountain-sunset.png")
    folder = ws.products / "desert cactus print"
    folder.mkdir()
    for n in range(19):
        (folder / f"{n + 1:02d}.jpg").write_bytes(picture())
    web.client.post("/api/info-images/etsy", json={"listing_image_ids": [4401, 4402]})
    body = web.client.get("/api/designs/pending").json()
    assert body["info_images"] == {"count": 2, "names": ["etsy-4401.jpg", "etsy-4402.jpg"],
                                   "cut": 1}
    assert body["images_each"] == 3 + 1 + 2 and body["images_max"] == 20
    assert "info_cut" in body["warnings"]
    assert body["mockups"]["max"] == 17


def test_the_listing_page_names_an_info_image_as_such():
    image = {"listing_image_id": 7, "rank": 4, "url_570xN": "https://i.etsystatic.com/a.jpg",
             "alt_text": "Materials"}
    view = listings_api._image_view(image, "etsy-4401.jpg", "retro.png", {},
                                    info={"etsy-4401.jpg"})
    assert view["info"] is True and view["type"] == "" and view["color"] == ""
    other = listings_api._image_view(image, "retro--mug-white.jpg", "retro.png", {})
    assert other["info"] is False


# --------------------------------------------------------------------------- the page's words


def _strings(page: str) -> dict:
    return json.loads((STATIC / "i18n" / f"{page}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("page, prefixes", [
    ("template", ("info.", "errors.")),
    ("designs", ("ready.info", "ready.sub_info", "ready.mockups_over_info", "warn.info")),
    ("mockups", ("usage.",)),
    ("listing-detail", ("images.info",)),
])
def test_every_key_the_pages_use_is_in_both_languages(page, prefixes):
    source = (STATIC / "js" / "pages" / f"{page}.js").read_text(encoding="utf-8")
    used = {key for key in re.findall(r'\bt\("([a-z_.]+)"', source) if key.startswith(prefixes)}
    strings = _strings(page)
    assert list(strings["tr"]) == list(strings["en"])
    for lang in ("tr", "en"):
        missing = sorted(key for key in used if key not in strings[lang])
        assert not missing, (page, lang, missing)


def test_the_error_codes_have_words_on_the_page():
    source = Path(info_api.__file__).read_text(encoding="utf-8")
    codes = set(re.findall(r'ApiError\(\s*\d+,\s*"([a-z_]+)"', source)) - {"invalid", "not_found"}
    strings = _strings("template")
    for lang in ("tr", "en"):
        assert {c for c in codes if f"errors.{c}" not in strings[lang]} == set(), lang
