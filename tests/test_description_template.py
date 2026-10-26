"""drop.description: a draft's description made from the template listing's own.

The template listing's title is replaced by the draft's, the seller may save a description
template with {başlık} / {tasarım}, and sentences about the template's own design are
flagged. Invented data only (ExampleShop, listing ids 1000001...).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image
from typer.testing import CliRunner

from etsyotomasyon.drop import description, generate, mockup, pipeline, stream, workspace
from etsyotomasyon.drop.seeds import Seed
from etsyotomasyon.drop.template import DESCRIPTION_TEMPLATE, Template, capture

TITLE = "Sage Lemon Wallpaper | Olive Citrus Mural | Peel and Stick Removable Wallpaper"
TAGS = ["lemon wallpaper", "citrus mural", "olive wallpaper", "peel and stick",
        "kitchen wallpaper", "wallpaper sample", "removable wallpaper", "accent wall",
        "housewarming gift", "sage wall decor"]
DESCRIPTION = (
    "Sage Lemon Wallpaper | Olive Citrus Mural\n"
    "\n"
    "Bring a sunny grove of lemons to your walls. Each citrus branch is painted by "
    "hand in soft olive tones.\n"
    "\n"
    "• Printed on thick, matte paper\n"
    "• Peel and stick: easy to install, easy to remove\n"
    "• Wallpaper samples are available: order a sample first!\n"
    "\n"
    "Sizes: 24 x 48 in per panel. Our wallpaper suits kitchens, hallways and accent walls."
)
CATEGORY = ["Home & Living", "Home Improvement", "Wallpaper"]
NEW_TITLE = "Woodland Nursery Mural, Forest Animal Wallpaper, Kids Room Decor"
SEED = Seed("woodland nursery mural", "woodland-nursery-mural.png")


def words():
    return description.design_words(TITLE, TAGS, product_words=CATEGORY)


# --- the template's own title ------------------------------------------------------------


def test_the_title_line_and_the_whole_title_become_the_new_title():
    text = DESCRIPTION + "\n\nThank you for visiting our " + TITLE.lower() + " listing."
    out = description.replace_title(text, TITLE, NEW_TITLE)
    assert out.startswith(NEW_TITLE + "\n\n")
    assert out.endswith(f"Thank you for visiting our {NEW_TITLE} listing.")
    assert "Sage Lemon Wallpaper" not in out and "Olive Citrus Mural" not in out
    # Everything else is the seller's text, word for word.
    assert "Bring a sunny grove of lemons to your walls." in out
    assert "• Peel and stick: easy to install, easy to remove" in out


@pytest.mark.parametrize("line, expected", [
    ("✨ Sage Lemon Wallpaper | Olive Citrus Mural ✨", f"✨ {NEW_TITLE} ✨"),
    ("- Sage Lemon Wallpaper, Olive Citrus Mural.", f"- {NEW_TITLE}."),
    ("Sage Lemon Wallpaper", NEW_TITLE),
    # A title line cut short at a word, as a shortened first line often is.
    ("Sage Lemon Wallpaper | Olive", NEW_TITLE),
    ("Sage Lemon Wallpaper - Olive Citrus Mural", NEW_TITLE),
])
def test_a_line_made_of_the_titles_segments_is_replaced(line, expected):
    assert description.replace_title(line, TITLE, NEW_TITLE) == expected


@pytest.mark.parametrize("line", [
    "Peel and Stick Removable Wallpaper",  # a generic segment alone: not the design's name
    "Olive Citrus Mural",  # the first segment (the design's name) is missing
    "Sage Lemon Wallpaper, printed to order.",  # a sentence that names it
    "Peel and stick: easy to install, easy to remove",
])
def test_other_lines_are_left_alone(line):
    assert description.replace_title(line, TITLE, NEW_TITLE) == line


def test_a_one_word_title_is_replaced_only_as_a_line_of_its_own():
    text = "Poster\nThis poster is printed on thick paper."
    assert description.replace_title(text, "Poster", "Botanical Print") == (
        "Botanical Print\nThis poster is printed on thick paper.")


def test_no_title_or_no_text_changes_nothing():
    assert description.replace_title(DESCRIPTION, "", NEW_TITLE) == DESCRIPTION
    assert description.replace_title("", TITLE, NEW_TITLE) == ""


def test_a_description_without_its_title_gets_the_new_title_first_as_before():
    # What generate.build_description did before 0.3.2, for a template without a title.
    assert description.fallback("Soft cotton tee.", "", "Retro Sunset Shirt") == (
        "Retro Sunset Shirt.\n\nSoft cotton tee.")
    assert description.fallback("", TITLE, "Retro Sunset Shirt") == "Retro Sunset Shirt."
    assert generate.build_description(SEED, "Soft cotton tee.", "Retro Sunset Shirt") == (
        "Retro Sunset Shirt.\n\nSoft cotton tee.")


# --- the description template and its placeholders ---------------------------------------


def test_the_starting_template_holds_the_title_placeholder():
    tr = description.initial_template(DESCRIPTION, TITLE)
    assert tr.startswith("{başlık}\n\nBring a sunny grove")
    en = description.initial_template(DESCRIPTION, TITLE, lang="en")
    assert en.startswith("{title}\n\n")
    # Filled in, it is exactly what a draft gets with no template saved.
    expected = description.fallback(DESCRIPTION, TITLE, NEW_TITLE)
    for text in (tr, en):
        assert description.render(text, title=NEW_TITLE, design="Woodland Nursery Mural") == expected
    no_title = description.initial_template("Soft cotton tee.", "Retro Sunset Shirt")
    assert no_title == "{başlık}.\n\nSoft cotton tee."


def test_placeholders_in_either_language_and_any_case_are_filled():
    text = "{başlık}\n{Başlık} / {BAŞLIK} / {title} / {başlık}\n{tasarım} · { design } · {TASARIM}"
    out = description.render(text, title="Forest Animal Wallpaper", design="Woodland Nursery Mural")
    assert out == ("Forest Animal Wallpaper\nForest Animal Wallpaper / Forest Animal Wallpaper / "
                   "Forest Animal Wallpaper / Forest Animal Wallpaper\n"
                   "Woodland Nursery Mural · Woodland Nursery Mural · Woodland Nursery Mural")


def test_an_unknown_placeholder_is_kept_and_named():
    text = "Hello {isim}, {başlık} {isim} {}"
    assert description.render(text, title="T") == "Hello {isim}, T {isim} {}"
    assert description.unknown_placeholders(text) == ["{isim}"]
    assert description.unknown_placeholders("{başlık} {Tasarım} {title} {design}") == []


def test_a_template_that_fills_to_nothing_still_gives_etsy_a_description():
    assert description.render("   ", title="Forest Mural") == "Forest Mural."
    # {tasarım} without a design name reads the title.
    assert description.render("{tasarım}", title="Forest Mural") == "Forest Mural"


def test_the_design_name_is_the_concept_as_a_title_reads_it():
    assert description.design_name(SEED) == "Woodland Nursery Mural"
    assert description.design_name(None) == ""


def test_build_uses_the_saved_template_else_the_fallback():
    saved = "{başlık}\n\n{tasarım}: a woodland of its own. Peel and stick."
    out = generate.build_description(SEED, DESCRIPTION, NEW_TITLE, source_title=TITLE,
                                     description_template=saved)
    assert out == f"{NEW_TITLE}\n\nWoodland Nursery Mural: a woodland of its own. Peel and stick."
    fallback = generate.build_description(SEED, DESCRIPTION, NEW_TITLE, source_title=TITLE)
    assert fallback == description.fallback(DESCRIPTION, TITLE, NEW_TITLE)
    # A saved template of only spaces is no template.
    assert generate.build_description(SEED, DESCRIPTION, NEW_TITLE, source_title=TITLE,
                                      description_template="  \n") == fallback


# --- sentences about the template's own design --------------------------------------------


def test_the_design_words_come_from_the_title_and_tags_less_product_words():
    assert set(words()) == {"sage", "lemon", "olive", "citrus"}
    # Without the category the product words are still known to be no design.
    assert set(description.design_words(TITLE, TAGS)) == {"sage", "lemon", "olive",
                                                          "citrus"}
    # A category name nobody would call a design is left out when it is known.
    assert "planner" not in description.design_words("Floral Planner Stickers", [],
                                                      product_words=["Paper & Party Supplies",
                                                                     "Planner"])


def test_the_sentences_about_the_templates_design_are_flagged():
    text = description.initial_template(DESCRIPTION, TITLE)
    found = description.flags(text, words())
    assert [text[f.start:f.end] for f in found] == [
        "Bring a sunny grove of lemons to your walls.",
        "Each citrus branch is painted by hand in soft olive tones.",
    ]
    assert [f.words for f in found] == [("lemon",), ("citrus", "olive")]
    assert description.flag_words(found) == ["lemon", "citrus", "olive"]


@pytest.mark.parametrize("sentence", [
    "Wallpaper samples are available: order a sample first!",
    "Our wallpaper suits kitchens, hallways and accent walls.",
    "Peel and stick: easy to install, easy to remove.",
    "A lovely housewarming gift for a new home.",
    "Printed on thick, matte paper in high quality inks.",
    "Sizes: 24 x 48 in per panel, removable and renter friendly.",
    "Order a swatch or a roll; each panel is 2 ft wide.",
])
def test_generic_sentences_are_never_flagged(sentence):
    assert description.flags(sentence, words()) == []


def test_a_word_the_drafts_own_design_shares_is_not_flagged():
    text = "Sunset tones on every mountain. Soft cotton."
    flagged = description.flags(text, ["sunset", "mountain"])
    assert len(flagged) == 1
    assert description.flags(text, ["sunset", "mountain"], allowed={"sunset", "mountain"}) == []
    mountain = Seed("mountain goat trail", "mountain-goat-trail.png")
    left = description.leftover("Mountain views. Soft cotton.", "Retro Mountain Sunset Shirt",
                                [], seed=mountain, title="Mountain Goat Trail Shirt")
    assert left == []


def test_sentences_split_at_line_ends_and_full_stops_with_offsets():
    text = "One. Two!\n  Three? \n\nFour… five"
    spans = [text[a:b] for a, b in description.sentences(text)]
    assert spans == ["One.", "Two!", "Three?", "Four…", "five"]


def test_the_leftover_names_what_a_draft_would_still_carry():
    left = description.leftover(DESCRIPTION, TITLE, TAGS, seed=SEED, title=NEW_TITLE,
                                product_words=CATEGORY)
    assert len(left) == 2
    message = description.leftover_message(left)
    assert message.startswith("the description still has 2 sentences about the template")
    assert "(lemon, citrus, olive)" in message


# --- product.json ---------------------------------------------------------------------------


def test_the_description_template_and_category_are_saved_in_product_json():
    template = Template(1000001, source_title=TITLE, description=DESCRIPTION, tags=TAGS,
                        description_template="{başlık}\n\nPeel and stick.",
                        category_path=CATEGORY)
    data = template.to_dict()
    assert data[DESCRIPTION_TEMPLATE] == "{başlık}\n\nPeel and stick."
    assert data["category_path"] == CATEGORY
    back = Template.from_dict(json.loads(json.dumps(data)))
    assert back.description_template == "{başlık}\n\nPeel and stick."
    assert back.category_path == CATEGORY
    # Nothing saved: neither key is written, and reading an old file gives None / [].
    plain = Template(1000001, source_title=TITLE).to_dict()
    assert DESCRIPTION_TEMPLATE not in plain and "category_path" not in plain
    old = Template.from_dict({"source_listing_id": 1000001, "fields": {}})
    assert old.description_template is None and old.category_path == []
    # A template of only spaces, or not a string, counts as none; & stays as typed.
    assert Template.from_dict({**plain, DESCRIPTION_TEMPLATE: " \n "}).description_template is None
    assert Template.from_dict({**plain, DESCRIPTION_TEMPLATE: 5}).description_template is None
    typed = Template.from_dict({**plain, DESCRIPTION_TEMPLATE: "Salt &amp; Pepper"})
    assert typed.description_template == "Salt &amp; Pepper"


def test_effective_is_the_saved_template_or_the_starting_one():
    template = Template(1000001, source_title=TITLE, description=DESCRIPTION, tags=TAGS,
                        category_path=CATEGORY)
    text, custom = description.effective(template)
    assert not custom and text == description.initial_template(DESCRIPTION, TITLE)
    assert len(description.template_flags(template)) == 2
    template.description_template = "{başlık}\n\nPeel and stick."
    assert description.effective(template) == ("{başlık}\n\nPeel and stick.", True)
    assert description.template_flags(template) == []


# --- generate.generate (the CLI's run) -----------------------------------------------------


def test_generate_warns_while_design_sentences_remain_and_not_once_a_template_is_saved():
    result = generate.generate(SEED, None, template_description=DESCRIPTION,
                               fallback_tags=TAGS, template_title=TITLE, product_words=CATEGORY)
    assert result.description.startswith(result.title + "\n\n")
    assert "Sage Lemon" not in result.description
    assert any("about the template listing's own design" in w for w in result.warnings)
    saved = generate.generate(SEED, None, template_description=DESCRIPTION, fallback_tags=TAGS,
                              template_title=TITLE,
                              description_template="{başlık}\n\n{tasarım}, peel and stick.")
    assert saved.description == f"{saved.title}\n\nWoodland Nursery Mural, peel and stick."
    assert not any("own design" in w for w in saved.warnings)


# --- the app's run (drop.stream) and the CLI's run (drop.pipeline) -------------------------


def _artwork(path: Path) -> Path:
    image = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    for x in range(10, 30):
        for y in range(10, 30):
            image.putpixel((x, y), (40, 120, 60, 255))
    image.save(path)
    return path


class _Client:
    """The Etsy calls a run makes, answered locally (an invented market sample)."""

    def __init__(self):
        self.creates: list[dict] = []
        self.next_id = 1000101

    def shop_id(self):
        return 12345678

    def listing_inventory(self, listing_id):
        return {"products": [{"property_values": [], "offerings": [
            {"price": {"amount": 2100, "divisor": 100}, "quantity": 5, "is_enabled": True}]}]}

    def search_active_listings(self, *, keywords, **_kw):
        first = keywords.split()[0]
        return iter([
            {"title": f"{keywords} wallpaper peel and stick mural {n}",
             "tags": [f"{first} wallpaper", "nursery decor", f"extra tag {n % 12}"],
             "price": {"amount": 2000, "divisor": 100, "currency_code": "USD"},
             "num_favorers": n}
            for n in range(30)
        ])

    def create_draft_listing(self, fields):
        self.creates.append(dict(fields))
        self.next_id += 1
        return {"listing_id": self.next_id}

    def upload_listing_image(self, listing_id, image, *, rank, alt_text=""):
        return {}


@pytest.fixture
def studio(tmp_path, monkeypatch):
    monkeypatch.setenv("ETSYOTOMASYON_HOME", str(tmp_path / "home"))
    ws = workspace.Workspace(tmp_path / "studio").create()
    Image.new("RGB", (40, 40), (240, 240, 240)).save(ws.mockups / "poster-white.jpg")
    _artwork(ws.products / "woodland-nursery-mural.png")

    # Compositing at the real output size is slow and beside the point here.
    def small(out):
        out.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (30, 30), (200, 200, 200)).save(out, "JPEG")
        return out

    monkeypatch.setattr(mockup, "compose", lambda design, template_image, out, **kw: small(out))
    monkeypatch.setattr(mockup, "flatten_design", lambda design, out, **kw: small(out))
    monkeypatch.setattr(stream, "SMALL_IMAGE_EDGE", 0)
    monkeypatch.setattr(mockup, "MAX_UPSCALE", float("inf"))
    template = Template(1000001, source_title=TITLE, fields={
        "taxonomy_id": 1, "price": 21, "quantity": 5, "who_made": "i_did",
        "when_made": "made_to_order", "type": "physical", "shipping_profile_id": 55,
    }, description=DESCRIPTION, tags=TAGS, category_path=CATEGORY)
    ws.write_template(template.to_dict())
    return ws, template


def _app_run(ws, template):
    from etsyotomasyon.drop import catalog

    client = _Client()
    report = stream.run_stream(ws, template, client, mockups=catalog.enabled_mockups(ws))
    return report.items[0], client


def test_the_app_run_replaces_the_title_and_warns_on_each_product(studio):
    ws, template = studio
    item, client = _app_run(ws, template)
    assert item.status == stream.OK
    sent = client.creates[0]["description"]
    assert sent == item.description
    assert sent.startswith(item.title + "\n\n")
    assert "Sage Lemon Wallpaper" not in sent and "Olive Citrus Mural" not in sent
    warning = next(w for w in item.warnings if w.code == "description_design")
    assert warning.step == "tags"
    assert warning.params == {"n": 2, "words": "lemon, citrus, olive"}
    # Thirteen tags: the step warns for the description alone.
    assert len(item.tags) == 13 and item.steps["tags"] == "warn"
    assert [w.code for w in item.warnings if w.step == "tags"] == ["description_design"]


def test_the_app_run_uses_the_saved_description_template(studio):
    ws, template = studio
    template.description_template = "{başlık}\n\n{tasarım} · peel and stick, easy to remove."
    ws.write_template(template.to_dict())
    item, client = _app_run(ws, Template.from_dict(ws.read_template()))
    assert item.status == stream.OK
    assert client.creates[0]["description"] == (
        f"{item.title}\n\nWoodland Nursery Mural · peel and stick, easy to remove.")
    assert len(item.tags) == 13 and item.steps["tags"] == "done"


def test_the_cli_run_replaces_the_title_and_warns_without_a_saved_template(studio):
    ws, template = studio
    report = pipeline.run(ws, template, client=_Client(), mockups_per_product=1)
    row = report.ready[0]
    assert "Sage Lemon Wallpaper" not in row.description
    assert row.description.startswith(row.title)
    assert any("own design (lemon, citrus, olive)" in w for w in row.warnings)


def test_the_cli_run_uses_the_saved_description_template(studio):
    ws, template = studio
    template.description_template = "{title}\n\nMade for {design}."
    ws.write_template(template.to_dict())
    report = pipeline.run(ws, Template.from_dict(ws.read_template()), client=_Client(),
                          mockups_per_product=1)
    row = report.ready[0]
    assert row.description == f"{row.title}\n\nMade for Woodland Nursery Mural."
    assert not any("own design" in w for w in row.warnings)


# --- the CLI's `drop template` ---------------------------------------------------------------


class _ListingClient:
    def __init__(self, listing):
        self._listing = listing

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def listing(self, listing_id):
        return dict(self._listing, listing_id=listing_id)


LISTING = {
    "listing_id": 1000001, "title": TITLE, "description": DESCRIPTION, "tags": TAGS,
    "taxonomy_id": 1633, "shipping_profile_id": 999, "who_made": "i_did",
    "when_made": "made_to_order", "listing_type": "physical",
    "price": {"amount": 2400, "divisor": 100, "currency_code": "USD"}, "quantity": 5,
}


def test_drop_template_keeps_the_description_template_of_the_same_listing(studio, monkeypatch):
    from etsyotomasyon import cli

    ws, _template = studio
    monkeypatch.setattr(cli, "_client", lambda **_kw: _ListingClient(LISTING))
    saved = capture(LISTING).to_dict()
    saved[DESCRIPTION_TEMPLATE] = "{başlık}\n\nMine."
    ws.write_template(saved)

    result = CliRunner().invoke(cli.app, ["drop", "template", "--from-listing", "1000001",
                                          "--path", str(ws.root)])
    assert result.exit_code == 0, result.output
    assert json.loads(ws.template_path.read_text(encoding="utf-8"))[DESCRIPTION_TEMPLATE] == (
        "{başlık}\n\nMine.")
    # Saved, it has no design-specific sentence left: no warning.
    assert "specific to this listing" not in " ".join(result.output.split())

    # Another listing starts from its own description, and says what would be copied.
    result = CliRunner().invoke(cli.app, ["drop", "template", "--from-listing", "1000002",
                                          "--path", str(ws.root)])
    assert result.exit_code == 0, result.output
    data = json.loads(ws.template_path.read_text(encoding="utf-8"))
    assert DESCRIPTION_TEMPLATE not in data
    said = " ".join(result.output.split())
    assert "2 sentence(s) of the description look specific to this listing's own design" in said
    assert "{title} and {design}" in said
