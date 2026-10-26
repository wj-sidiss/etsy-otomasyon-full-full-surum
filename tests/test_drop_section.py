"""The shop section a run's drafts go into: resolving `--section NAME_OR_ID`, the
template copy that carries it, and `drop auto` / `drop run` with it. Etsy is a fake."""

from __future__ import annotations

import json

import pytest
from PIL import Image
from typer.testing import CliRunner

from etsyotomasyon import cli, csvio
from etsyotomasyon.cli import app
from etsyotomasyon.drop import automation, pipeline
from etsyotomasyon.drop.template import Template, resolve_section, section_number
from etsyotomasyon.drop.workspace import Workspace
from etsyotomasyon.errors import ValidationError

TEES, MUGS, GIFTS = 9001, 9002, 9003
SECTIONS = [
    {"shop_section_id": TEES, "title": "Tişörtler", "rank": 1, "active_listing_count": 20},
    {"shop_section_id": MUGS, "title": "Kupalar", "rank": 2, "active_listing_count": 10},
]


# --- resolving a name or an id ------------------------------------------------------------------


@pytest.mark.parametrize("wanted, expected", [
    (str(TEES), TEES), (f" {MUGS} ", MUGS), ("Kupalar", MUGS), ("  kupalar ", MUGS),
    ("Tisortler", None),  # titles match exactly (case aside), never loosely
    ("Tişörtler", TEES), ("none", "none"), ("NONE", "none"),
])
def test_resolve_section_by_id_or_title(wanted, expected):
    if expected is None:
        with pytest.raises(ValidationError):
            resolve_section(SECTIONS, wanted)
        return
    found = resolve_section(SECTIONS, wanted)
    if expected == "none":
        assert found is None
    else:
        assert found["shop_section_id"] == expected


def test_a_deleted_section_is_refused_with_the_shops_sections():
    with pytest.raises(ValidationError) as caught:
        resolve_section(SECTIONS, str(GIFTS))
    message = str(caught.value)
    assert f"id {GIFTS}" in message and "may have been deleted" in message
    assert f"Tişörtler ({TEES})" in message and f"Kupalar ({MUGS})" in message
    with pytest.raises(ValidationError, match="no section 'Hediyeler'"):
        resolve_section(SECTIONS, "Hediyeler")


def test_a_shared_title_needs_the_id_and_a_title_wins_over_the_keyword():
    twins = [*SECTIONS, {"shop_section_id": GIFTS, "title": "kupalar"}]
    with pytest.raises(ValidationError, match="use its id"):
        resolve_section(twins, "Kupalar")
    assert resolve_section(twins, str(GIFTS))["shop_section_id"] == GIFTS
    called_none = [{"shop_section_id": GIFTS, "title": "None"}]
    assert resolve_section(called_none, "none")["shop_section_id"] == GIFTS


def test_a_shop_without_sections():
    with pytest.raises(ValidationError, match="has no sections"):
        resolve_section([], "Kupalar")
    assert resolve_section([], "none") is None
    with pytest.raises(ValidationError, match="needs a section"):
        resolve_section(SECTIONS, "  ")


@pytest.mark.parametrize("value, expected", [
    (TEES, TEES), (str(TEES), TEES), (" 12 ", 12), (0, None), (-3, None), (True, None),
    (None, None), ("", None), ("12a", None), (1.0, None),
])
def test_section_number(value, expected):
    assert section_number(value) == expected


# --- the template copy ----------------------------------------------------------------------


def test_with_section_is_a_copy_the_template_keeps_its_own():
    template = Template(1, fields={"taxonomy_id": 1, "price": 20, "shop_section_id": TEES},
                        tags=["gift"], materials=["cotton"])
    assert template.section_id == TEES
    moved = template.with_section(MUGS)
    assert moved.fields["shop_section_id"] == MUGS and moved.section_id == MUGS
    cleared = template.with_section(None)
    assert "shop_section_id" not in cleared.fields and cleared.section_id is None
    assert template.fields["shop_section_id"] == TEES  # untouched
    moved.tags.append("more")
    assert template.tags == ["gift"]
    assert Template(1, fields={"shop_section_id": "x"}).section_id is None


# --- drop auto / drop run --section ------------------------------------------------------------


@pytest.fixture
def studio(tmp_path):
    ws = Workspace(tmp_path / "studio").create()
    folder = ws.products / "mountain sunset shirt"
    folder.mkdir()
    Image.new("RGBA", (20, 20), (20, 30, 40, 100)).save(folder / "1-front.png")
    template = Template(1, fields={"taxonomy_id": 1, "price": 20, "quantity": 5,
                                    "who_made": "i_did", "when_made": "made_to_order",
                                    "type": "physical", "shop_section_id": TEES},
                        description="Cotton shirt.")
    ws.write_template(template.to_dict())
    return ws


class Client:
    """What `drop auto` sends to Etsy, with the shop's sections."""

    def __init__(self, sections=SECTIONS):
        self.sections = sections
        self.fields: list[dict] = []
        self.section_reads = 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def close(self):
        self.closed = True

    def shop_id(self):
        return 123

    def shop_sections(self):
        self.section_reads += 1
        return [dict(s) for s in self.sections]

    def listing_inventory(self, listing_id):
        return {"products": [{"property_values": [], "offerings": [
            {"price": {"amount": 2000, "divisor": 100}, "quantity": 5, "is_enabled": True}]}]}

    def search_active_listings(self, **kwargs):
        return iter([])

    def create_draft_listing(self, fields):
        self.fields.append(dict(fields))
        return {"listing_id": 900 + len(self.fields)}

    def upload_listing_image(self, listing_id, image, *, rank, alt_text=""):
        return {}


def _auto(ws, *extra):
    return CliRunner().invoke(app, ["drop", "auto", "--path", str(ws.root), *extra])


@pytest.mark.parametrize("wanted, sent", [
    ("Kupalar", MUGS), (str(MUGS), MUGS), ("none", None), (None, TEES),
])
def test_drop_auto_puts_every_draft_in_the_section(studio, monkeypatch, wanted, sent):
    client = Client()
    monkeypatch.setattr(cli, "_client", lambda **_kw: client)
    result = _auto(studio, *(["--section", wanted] if wanted is not None else []))
    assert result.exit_code == 0, result.output
    assert "Created 1 draft(s)" in result.output
    [fields] = client.fields
    assert fields.get("shop_section_id") == sent
    assert client.section_reads == (0 if wanted is None else 1)
    if wanted == "Kupalar":
        assert f"Shop section: Kupalar ({MUGS})" in result.output
    if wanted == "none":
        assert "Shop section: none" in result.output
    # The template keeps its own section for the next run.
    assert json.loads(studio.template_path.read_text(encoding="utf-8"))["fields"][
        "shop_section_id"] == TEES


def test_drop_auto_refuses_a_deleted_section_before_anything_is_sent(studio, monkeypatch):
    client = Client()
    monkeypatch.setattr(cli, "_client", lambda **_kw: client)
    result = _auto(studio, "--section", str(GIFTS))
    assert result.exit_code != 0 and isinstance(result.exception, ValidationError)
    assert "may have been deleted" in str(result.exception)
    assert client.fields == [] and client.closed
    assert not (studio.root / "upload-history.json").exists()


def test_drop_auto_in_a_shop_without_sections(studio, monkeypatch):
    client = Client(sections=[])
    monkeypatch.setattr(cli, "_client", lambda **_kw: client)
    result = _auto(studio, "--section", "Kupalar")
    assert result.exit_code != 0 and "has no sections" in str(result.exception)
    assert client.fields == []
    assert _auto(studio, "--section", "none").exit_code == 0
    assert "shop_section_id" not in client.fields[0]


def test_drop_auto_dry_run_looks_the_section_up_read_only(studio, monkeypatch):
    client = Client()
    asked = {}

    def make(**kwargs):
        asked.update(kwargs)
        return client

    monkeypatch.setattr(cli, "_client", make)
    seen = {}

    def fake_run(workspace, template, **kwargs):
        seen["template"] = template
        seen.update(kwargs)
        return automation.AutoReport()

    monkeypatch.setattr(automation, "run", fake_run)
    result = _auto(studio, "--dry-run", "--section", "Kupalar")
    assert result.exit_code == 0, result.output
    assert asked == {"require_auth": False} and seen["dry_run"] is True
    assert seen["template"].fields["shop_section_id"] == MUGS
    assert client.fields == [] and client.closed


def test_drop_run_writes_the_section_into_the_review_rows(studio, monkeypatch):
    client = Client()
    monkeypatch.setattr(cli, "EtsyClient", lambda *_a, **_kw: client)
    monkeypatch.setattr(cli.Config, "load", classmethod(lambda cls, *a, **kw: object()))
    seen = {}

    def fake_run(workspace, template, **kwargs):
        seen["template"] = template
        return pipeline.DropReport(batch="b", out_dir=workspace.drafts / "b")

    monkeypatch.setattr(pipeline, "run", fake_run)
    result = CliRunner().invoke(app, ["drop", "run", "--path", str(studio.root),
                                      "--section", "none"])
    assert "shop_section_id" not in seen["template"].fields, result.output
    assert client.closed


def test_drop_run_with_a_section_needs_the_keys(studio):
    # conftest's home has no Etsy keys: without --section the run goes on without
    # research; with it, it stops, since the sections cannot be read.
    result = CliRunner().invoke(app, ["drop", "run", "--path", str(studio.root),
                                      "--section", "Kupalar"])
    assert result.exit_code != 0
    assert result.exception is not None


def test_a_row_carries_the_runs_section(studio):
    template = Template.from_dict(json.loads(studio.template_path.read_text(encoding="utf-8")))
    report = pipeline.run(studio, template.with_section(MUGS))
    [row] = csvio.read_rows(report.csv_path)
    assert row["shop_section_id"] == str(MUGS)
    report = pipeline.run(studio, template.with_section(None))
    [row] = csvio.read_rows(report.csv_path)
    assert row["shop_section_id"] == ""
