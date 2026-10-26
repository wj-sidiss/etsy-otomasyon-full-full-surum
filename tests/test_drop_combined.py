"""0.3.2's four run features together: a watermark, the shop's info images, a description
template and a chosen shop section, in the app's run (drop.stream) and the CLI's
(drop.automation). Each feature has its own tests; these check they do not step on
each other: only the product's own photos are stamped and flagged is_watermarked, the
info images come last with their alt texts and their own bytes, and the draft carries
the description template's text and the chosen section."""

from __future__ import annotations

import json

import test_drop_stream
from test_drop_stream import SHOP, _artwork, _history, _run
from test_infoimages import picture
from test_watermark import MarkClient, _set_mark, _stamped

from etsyotomasyon.drop import automation, infoimages, stream
from etsyotomasyon.drop.template import Template
from etsyotomasyon.drop.workspace import INFO_DIR, Workspace

studio = test_drop_stream.studio

SECTION = 9004
DESCRIPTION = "{başlık}\n\n{tasarım} · printed to order, shipped in 3 days."


def _prepare(ws: Workspace, template: Template) -> Template:
    """Watermark on for every listing photo, two info images, a description template and
    the run's section: the combination one Tasarım Yükle run can have."""
    _set_mark(ws, scope="all", position="corner")
    infoimages.add(ws, "how-to-order.jpg", picture(), alt="How to order")
    infoimages.add(ws, "care.png", picture("PNG", colour=(160, 40, 90)))
    template.description_template = DESCRIPTION
    ws.write_template(template.to_dict())
    return Template.from_dict(ws.read_template()).with_section(SECTION)


def test_the_apps_run_uses_all_four_together(studio):
    ws, template = studio
    template = _prepare(ws, template)
    _artwork(ws.products / "retro-mountain-sunset.png")
    client = MarkClient(ws)
    item = _run(ws, template, client).items[0]

    assert item.status == stream.OK
    # The draft: the chosen section and the description template's text.
    [fields] = client.creates
    assert int(fields["shop_section_id"]) == SECTION
    assert fields["description"] == (
        f"{item.title}\n\nRetro Mountain Sunset · printed to order, shipped in 3 days.")
    # The product's own pictures first, stamped and flagged; the info images last, in
    # their order, as they are (never stamped, never flagged), with their alt texts.
    names = [name for _listing, name, _rank in client.images]
    assert [rank for *_x, rank in client.images] == list(range(1, len(names) + 1))
    assert names[-2:] == ["how-to-order.jpg", "care.png"]
    own, info = client.image_paths[:-2], client.image_paths[-2:]
    assert own and all(_stamped(p) for p in own)
    assert all(client.marked[p.name] for p in own)
    assert not any(client.marked[p.name] for p in info)
    assert not any(_stamped(p) for p in info)
    for sent in info:
        assert sent.read_bytes() == (ws.root / INFO_DIR / sent.name).read_bytes()
    assert client.alts["how-to-order.jpg"] == "How to order"
    assert client.alts["care.png"] == ""
    assert len(item.stamped) == len(item.images) == len(own)
    # The history knows the info images apart from the product's own pictures.
    entry = _history(ws)[SHOP]["retro-mountain-sunset.png"]
    assert entry["info_images"] == ["how-to-order.jpg", "care.png"]
    assert entry["images_total"] == len(names)


class AutoClient:
    """drop auto's calls: the draft's fields, and each picture's alt text and flag."""

    def __init__(self):
        self.creates: list[dict] = []
        self.images: list[tuple[str, int, str, bool]] = []

    def shop_id(self):
        return 123

    def listing_inventory(self, listing_id):
        return {"products": [{"property_values": [], "offerings": [
            {"price": {"amount": 2000, "divisor": 100}, "quantity": 5, "is_enabled": True}]}]}

    def search_active_listings(self, **kwargs):
        return iter([])

    def create_draft_listing(self, fields):
        self.creates.append(dict(fields))
        return {"listing_id": 900}

    def upload_listing_image(self, listing_id, image, *, rank, alt_text="", is_watermarked=False):
        self.images.append((image.name, rank, alt_text, is_watermarked, image.parent.name))
        return {"listing_image_id": 5000 + rank}


def test_drop_auto_uses_all_four_together(tmp_path):
    ws = Workspace(tmp_path / "studio").create()
    folder = ws.products / "mountain sunset shirt"
    folder.mkdir()
    for name in ("1-front.jpg", "2-back.jpg"):
        (folder / name).write_bytes(picture(size=(80, 80)))
    template = Template(1000013, source_title="Mountain Sunset Shirt", fields={
        "taxonomy_id": 1, "price": 20, "quantity": 5, "who_made": "i_did",
        "when_made": "made_to_order", "type": "physical", "shop_section_id": 9001,
    }, description="Mountain Sunset Shirt\n\nSoft cotton.")
    ws.write_template(template.to_dict())
    template = _prepare(ws, template)
    client = AutoClient()

    report = automation.run(ws, template, client=client)

    assert report.uploaded.created == 1
    [fields] = client.creates
    assert int(fields["shop_section_id"]) == SECTION
    assert fields["description"].endswith("· printed to order, shipped in 3 days.")
    assert "{" not in fields["description"]
    assert [(n, r, a, w) for n, r, a, w, _dir in client.images] == [
        ("1-front.jpg", 1, "", True), ("2-back.jpg", 2, "", True),
        ("how-to-order.jpg", 3, "How to order", False), ("care.png", 4, "", False),
    ]
    # The stamped copies went up for the photos, the batch's own copies for the info images.
    assert [d for *_x, d in client.images] == ["watermarked", "watermarked", INFO_DIR, INFO_DIR]
    entry = json.loads(automation.history_path(ws.root).read_text(encoding="utf-8"))["123"][
        "mountain sunset shirt"]
    assert entry["info_images"] == ["how-to-order.jpg", "care.png"]
