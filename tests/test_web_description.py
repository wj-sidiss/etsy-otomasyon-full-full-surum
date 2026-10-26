"""Şablon İlan's description template: /api/template/description, the start card's
warning and the run's use of it. Invented data only (ExampleShop, 1000001...)."""

from __future__ import annotations

import json
from pathlib import Path

from test_web_designs import _png, _put, _setup_shop, fast_images  # noqa: F401
from test_web_template import connected, listing
from web_helpers import read_events, wait_for_job

from etsyotomasyon.drop import description as description_mod
from etsyotomasyon.drop.template import DESCRIPTION_TEMPLATE, Template

STATIC = Path(__file__).resolve().parents[1] / "etsyotomasyon" / "web" / "static"

TITLE = "Sage Lemon Wallpaper | Olive Citrus Mural | Peel and Stick"
DESCRIPTION = (
    "Sage Lemon Wallpaper | Olive Citrus Mural\n"
    "\n"
    "Bring a sunny grove of lemons to your walls. Printed on thick paper.\n"
    "Wallpaper samples are available: order a sample first!"
)
FLAGGED = "Bring a sunny grove of lemons to your walls."
TAGS = ["lemon wallpaper", "citrus mural", "wallpaper sample", "peel and stick"]


def _language(web, lang="tr"):
    web.ctx.save_app_prefs({**web.ctx.app_prefs(), "language": lang})


def _saved(web, listing_id=1000011, **extra):
    """A connected shop whose template listing is the lemon wallpaper."""
    records = [
        listing(1000011, TITLE, description=DESCRIPTION, tags=TAGS, **extra),
        listing(1000012, "Retro Mountain Sunset Shirt, Vintage Hiking T-Shirt",
                description="Retro Mountain Sunset Shirt, printed to order. Soft cotton."),
    ]
    fake = connected(web, records)
    resp = web.client.post("/api/template", json={"listing_id": listing_id})
    assert resp.status_code == 200, resp.text
    return fake, resp.json()["template"]


def _product_json(web) -> dict:
    return json.loads(web.ctx.workspace().template_path.read_text(encoding="utf-8"))


def test_without_a_template_there_is_no_description_template(web):
    for method, body in (("GET", None), ("PUT", {"text": "x"}), ("DELETE", None)):
        resp = web.client.request(method, "/api/template/description", json=body)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "no_template"
    resp = web.client.post("/api/template/description/check", json={"text": "x"})
    assert resp.json()["error"]["code"] == "no_template"


def test_it_starts_from_the_listings_description_with_the_title_placeholder(web):
    _language(web, "tr")
    _fake, summary = _saved(web)
    assert summary["description"] == {"custom": False, "flags": 1}
    data = web.client.get("/api/template/description").json()
    assert data["custom"] is False
    assert data["text"] == data["initial"]
    assert data["text"].startswith("{başlık}\n\nBring a sunny grove")
    assert data["source_title"] == TITLE
    assert set(data["words"]) == {"sage", "lemon", "olive", "citrus"}
    assert data["placeholders"] == {"title": "{başlık}", "design": "{tasarım}"}
    [flag] = data["flags"]
    assert flag["text"] == FLAGGED and flag["words"] == ["lemon"]
    assert data["text"][flag["start"]:flag["end"]] == FLAGGED
    assert data["unknown"] == [] and data["max"] == description_mod.MAX_CHARS
    # The category's names were saved with it: never words of one design.
    assert _product_json(web)["category_path"] == ["Clothing", "Tops & Tees", "T-shirts"]
    # The summary of the saved template says the same.
    assert web.client.get("/api/template").json()["template"]["description"] == {
        "custom": False, "flags": 1}


def test_english_uses_the_english_placeholders(web):
    _language(web, "en")
    _saved(web)
    data = web.client.get("/api/template/description").json()
    assert data["text"].startswith("{title}\n\n")
    assert data["placeholders"] == {"title": "{title}", "design": "{design}"}


def test_check_flags_an_edited_text_without_saving_it(web):
    _saved(web)
    before = _product_json(web)
    text = "{başlık}\n\n🌿 Peel and stick. Each citrus branch is painted in olive. Hi {isim}!"
    resp = web.client.post("/api/template/description/check", json={"text": text})
    assert resp.status_code == 200
    data = resp.json()
    [flag] = data["flags"]
    assert flag["words"] == ["citrus", "olive"]
    # start/end count UTF-16 code units, as the page's textarea does: the emoji is two.
    as_js = text.encode("utf-16-le")
    assert as_js[flag["start"] * 2:flag["end"] * 2].decode("utf-16-le") == flag["text"]
    assert flag["text"] == "Each citrus branch is painted in olive."
    assert data["unknown"] == ["{isim}"]
    assert _product_json(web) == before
    empty = web.client.post("/api/template/description/check", json={"text": ""}).json()
    assert empty == {"flags": [], "unknown": []}


def test_a_saved_template_is_kept_in_product_json_and_used(web):
    _language(web, "tr")  # the starting text uses the UI language's placeholders
    _saved(web)
    text = "{başlık}\r\n\r\nA {tasarım} print. Wallpaper samples are available."
    resp = web.client.put("/api/template/description", json={"text": text})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["custom"] is True and data["flags"] == []
    assert data["text"] == "{başlık}\n\nA {tasarım} print. Wallpaper samples are available."
    saved = _product_json(web)
    assert saved[DESCRIPTION_TEMPLATE] == data["text"]
    template = Template.from_dict(saved)
    assert template.description_template == data["text"]
    assert web.client.get("/api/template").json()["template"]["description"] == {
        "custom": True, "flags": 0}
    again = web.client.get("/api/template/description").json()
    assert again["custom"] is True and again["text"] == data["text"]
    assert again["initial"].startswith("{başlık}\n\nBring a sunny grove")


def test_saving_the_listings_own_text_unchanged_saves_nothing(web):
    _language(web, "tr")
    _saved(web)
    initial = web.client.get("/api/template/description").json()["initial"]
    data = web.client.put("/api/template/description", json={"text": initial + "\n"}).json()
    assert data["custom"] is False
    assert DESCRIPTION_TEMPLATE not in _product_json(web)
    # The English starting text is the same text.
    english = description_mod.initial_template(DESCRIPTION, TITLE, lang="en")
    assert web.client.put("/api/template/description", json={"text": english}).json()[
        "custom"] is False


def test_reset_goes_back_to_the_listings_own_description(web):
    _saved(web)
    web.client.put("/api/template/description", json={"text": "{başlık}\n\nMine."})
    resp = web.client.delete("/api/template/description")
    assert resp.status_code == 200
    assert resp.json()["custom"] is False and len(resp.json()["flags"]) == 1
    assert DESCRIPTION_TEMPLATE not in _product_json(web)


def test_an_empty_too_long_or_odd_text_is_refused(web):
    _saved(web)
    empty = web.client.put("/api/template/description", json={"text": " \n "})
    assert empty.status_code == 422 and empty.json()["error"]["code"] == "description_empty"
    long = "x" * (description_mod.MAX_CHARS + 1)
    for method, path in (("PUT", "/api/template/description"),
                         ("POST", "/api/template/description/check")):
        resp = web.client.request(method, path, json={"text": long})
        assert resp.status_code == 422
        error = resp.json()["error"]
        assert error["code"] == "description_too_long"
        assert error["params"]["max"] == description_mod.MAX_CHARS
    odd = web.client.put("/api/template/description", json={"text": 5})
    assert odd.status_code == 422 and odd.json()["error"]["code"] == "invalid"
    assert DESCRIPTION_TEMPLATE not in _product_json(web)


def test_the_same_listing_saved_again_keeps_it_another_listing_starts_over(web):
    _language(web, "tr")  # the starting text uses the UI language's placeholders
    _saved(web)
    web.client.put("/api/template/description", json={"text": "{başlık}\n\nMine."})
    assert web.client.post("/api/template", json={"listing_id": 1000011}).status_code == 200
    assert _product_json(web)[DESCRIPTION_TEMPLATE] == "{başlık}\n\nMine."
    other = web.client.post("/api/template", json={"listing_id": 1000012}).json()["template"]
    assert DESCRIPTION_TEMPLATE not in _product_json(web)
    assert other["description"] == {"custom": False, "flags": 1}
    data = web.client.get("/api/template/description").json()
    assert data["text"].startswith("{başlık}.\n\nRetro Mountain Sunset Shirt, printed to order.")
    assert [f["words"] for f in data["flags"]] == [["mountain", "sunset"]]


def test_a_malformed_product_json_is_reported(web):
    connected(web)
    web.ctx.workspace().template_path.write_text("{not json", encoding="utf-8")
    resp = web.client.get("/api/template/description")
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "invalid"


# --- the start card and the run -----------------------------------------------------------------


def _flagged_shop(web):
    fake, ws = _setup_shop(web)
    template = Template.from_dict(ws.read_template())
    template.source_title = TITLE
    template.description = DESCRIPTION
    template.tags = TAGS
    ws.write_template(template.to_dict())
    return fake, ws


def test_the_start_card_warns_once_while_design_sentences_remain(web):
    _fake, ws = _flagged_shop(web)
    _put(web, "woodland-nursery-mural.png", _png())
    _put(web, "desert-cactus-print.png", _png((30, 160, 60)))
    data = web.client.get("/api/designs/pending").json()
    assert data["warnings"].count("description_flagged") == 1
    assert data["template"]["description_flags"] == 1
    assert data["template"]["description_custom"] is False
    # A description template without them: no warning.
    web.client.put("/api/template/description", json={"text": "{başlık}\n\nPeel and stick."})
    data = web.client.get("/api/designs/pending").json()
    assert "description_flagged" not in data["warnings"]
    assert data["template"]["description_custom"] is True
    assert data["template"]["description"] is True


def test_the_start_card_does_not_warn_when_nothing_would_run(web):
    _flagged_shop(web)
    data = web.client.get("/api/designs/pending").json()
    assert "empty" in data["blockers"] and "description_flagged" not in data["warnings"]


def _run(web):
    def trigger():
        resp = web.client.post("/api/designs/start", json={})
        assert resp.status_code == 200, resp.text

    events = read_events(
        web,
        lambda evs: any(t == "job" and d["kind"] == "designs" and d["status"] == "done"
                        for t, d in evs),
        after_connect=trigger,
        timeout=20,
    )
    job_id = next(d["id"] for t, d in events if t == "job" and d["kind"] == "designs")
    return wait_for_job(web, job_id)


def test_the_run_sends_the_new_title_and_warns_on_each_product(web, fast_images):  # noqa: F811
    fake, _ws = _flagged_shop(web)
    _put(web, "woodland-nursery-mural.png", _png())
    final = _run(web)
    assert final["status"] == "done"
    [item] = final["state"]["items"]
    [form] = fake.created
    sent = form["description"]
    assert sent.startswith(item["title"] + "\n\n")
    assert "Sage Lemon" not in sent and "Olive Citrus Mural" not in sent
    [warning] = [w for w in item["warnings"] if w["code"] == "description_design"]
    assert warning["params"] == {"n": 1, "words": "lemon"}
    assert warning["step"] == "tags"


def test_the_run_uses_the_saved_description_template(web, fast_images):  # noqa: F811
    fake, _ws = _flagged_shop(web)
    resp = web.client.put("/api/template/description",
                          json={"text": "{başlık}\n\n{tasarım}: peel and stick, easy to remove."})
    assert resp.status_code == 200
    _put(web, "woodland-nursery-mural.png", _png())
    final = _run(web)
    [item] = final["state"]["items"]
    [form] = fake.created
    assert form["description"] == (
        f"{item['title']}\n\nWoodland Nursery Mural: peel and stick, easy to remove.")
    assert not [w for w in item["warnings"] if w["code"] == "description_design"]


# --- the page's words ------------------------------------------------------------------------


def _strings(name):
    return json.loads((STATIC / "i18n" / name).read_text(encoding="utf-8"))


def test_the_new_words_are_there_in_both_languages_with_the_same_keys():
    template = _strings("template.json")
    designs = _strings("designs.json")
    for strings in (template, designs):
        assert set(strings["tr"]) == set(strings["en"])
    for key in ("desc.open", "desc.title", "desc.flag.message", "desc.hint_flagged",
                "desc.ph.title", "desc.ph.design", "errors.no_template",
                "errors.description_empty", "errors.description_too_long"):
        assert key in template["tr"], key
    assert template["tr"]["desc.flag.message"] == (
        "Bu cümle şablon ilanın desenine özel görünüyor; her ilana kopyalanır.")
    for key in ("warn.description_design", "ready.description_flagged", "ready.go_template"):
        assert key in designs["tr"], key
    # The placeholders the page inserts are the ones the server fills in.
    for lang in ("tr", "en"):
        for placeholder in description_mod.PLACEHOLDERS[lang].values():
            assert description_mod.placeholder_name(placeholder[1:-1]) is not None


def test_the_page_calls_the_description_endpoints():
    page = (STATIC / "js" / "pages" / "template.js").read_text(encoding="utf-8")
    for path in ("/api/template/description", "/api/template/description/check"):
        assert path in page
    start = (STATIC / "js" / "pages" / "designs.js").read_text(encoding="utf-8")
    assert "description_flagged" in start and "/kurulum/sablon?aciklama=1" in start
