"""Weights and sizes Etsy would refuse are left out, not sent (createDraftListing: "If
set, the value must be greater than 0", and each comes with its unit)."""

from __future__ import annotations

import pytest
from test_drop_stream import Client, _artwork, _run, studio  # noqa: F401

from etsyotomasyon import csvio, listings
from etsyotomasyon.drop import stream
from etsyotomasyon.drop import template as template_mod
from etsyotomasyon.drop.template import Template
from etsyotomasyon.errors import ValidationError

BASE = {
    "title": "Retro Sunset Shirt",
    "description": "A shirt.",
    "price": "19.99",
    "quantity": "5",
    "who_made": "i_did",
    "when_made": "made_to_order",
    "taxonomy_id": "482",
    "shipping_profile_id": "111",
}


def _payload(**extra):
    warnings: list[str] = []
    payload = listings.build_payload({**BASE, **extra}, is_update=False, warnings=warnings)
    return payload, warnings


def test_a_zero_weight_is_not_sent_and_the_seller_is_told():
    payload, warnings = _payload(item_weight="0", item_weight_unit="oz")
    assert "item_weight" not in payload and "item_weight_unit" not in payload
    assert any("item_weight 0 not sent" in w for w in warnings)


def test_a_zero_weight_from_a_saved_template_is_not_sent():
    payload, _ = _payload(item_weight="0.0", item_weight_unit="g")
    assert "item_weight" not in payload


def test_a_real_weight_with_its_unit_is_sent():
    payload, warnings = _payload(item_weight="250", item_weight_unit="g")
    assert payload["item_weight"] == 250.0 and payload["item_weight_unit"] == "g"
    assert not any("item_weight" in w for w in warnings)


def test_a_weight_without_a_unit_is_left_out():
    payload, warnings = _payload(item_weight="12")
    assert "item_weight" not in payload
    assert any("item_weight_unit is empty" in w for w in warnings)


def test_a_unit_without_a_weight_is_left_out():
    payload, _ = _payload(item_weight_unit="oz")
    assert "item_weight_unit" not in payload


def test_sizes_follow_the_same_rules():
    payload, _ = _payload(item_length="30", item_width="0", item_height="2", item_dimensions_unit="cm")
    assert payload["item_length"] == 30.0 and payload["item_height"] == 2.0
    assert "item_width" not in payload and payload["item_dimensions_unit"] == "cm"

    payload, warnings = _payload(item_length="30", item_width="20")
    assert "item_length" not in payload and "item_width" not in payload
    assert any("item_dimensions_unit is empty" in w for w in warnings)

    payload, _ = _payload(item_length="0", item_dimensions_unit="in")
    assert "item_length" not in payload and "item_dimensions_unit" not in payload


def test_capture_does_not_copy_a_weight_nobody_entered():
    listing = {
        "listing_id": 42, "title": "Poster", "price": {"amount": 1500, "divisor": 100},
        "quantity": 3, "who_made": "i_did", "when_made": "2020_2026", "taxonomy_id": 1,
        "shipping_profile_id": 7, "listing_type": "physical",
        "item_weight": 0, "item_weight_unit": "oz",
        "item_length": 0.0, "item_width": 0, "item_height": 0, "item_dimensions_unit": "in",
    }
    fields = template_mod.capture(listing).fields
    for name in ("item_weight", "item_weight_unit", "item_length", "item_width",
                 "item_height", "item_dimensions_unit"):
        assert name not in fields


def test_capture_keeps_a_real_weight():
    listing = {
        "listing_id": 43, "title": "Mug", "price": {"amount": 1500, "divisor": 100},
        "quantity": 3, "who_made": "i_did", "when_made": "2020_2026", "taxonomy_id": 1,
        "shipping_profile_id": 7, "listing_type": "physical",
        "item_weight": 340, "item_weight_unit": "g",
    }
    fields = template_mod.capture(listing).fields
    assert fields["item_weight"] == 340 and fields["item_weight_unit"] == "g"


# --- a template saved before 0.3.1 still holds the 0 Etsy reported ------------------------------


def _saved(**fields):
    """A product.json as 0.3.0 wrote it (all data invented)."""
    return {"source_listing_id": 1000013, "source_title": "Retro Mountain Sunset Shirt",
            "fields": {"taxonomy_id": 482, "price": 24.9, "quantity": 5, "who_made": "i_did",
                       "when_made": "made_to_order", "type": "physical",
                       "shipping_profile_id": 555001, **fields},
            "materials": [], "description": "A shirt.", "tags": [], "plain_text": True}


def test_an_old_template_with_a_zero_weight_reads_like_a_new_capture():
    fields = Template.from_dict(_saved(item_weight=0, item_weight_unit="oz")).fields
    assert "item_weight" not in fields and "item_weight_unit" not in fields
    fields = Template.from_dict(_saved(item_weight=250, item_weight_unit="g")).fields
    assert (fields["item_weight"], fields["item_weight_unit"]) == (250, "g")
    # a value without its unit, a 0 size, a size that is no number, a flag that is no weight
    fields = Template.from_dict(_saved(item_weight=12, item_weight_unit=None,
                                       item_length=30, item_width=0.0, item_height="x",
                                       item_dimensions_unit="cm")).fields
    assert "item_weight" not in fields and "item_weight_unit" not in fields
    assert fields["item_length"] == 30 and fields["item_dimensions_unit"] == "cm"
    assert "item_width" not in fields and "item_height" not in fields
    fields = Template.from_dict(_saved(item_weight=True, item_weight_unit="oz")).fields
    assert "item_weight" not in fields and "item_weight_unit" not in fields
    # the rest of the template is untouched
    assert fields["price"] == 24.9 and fields["shipping_profile_id"] == 555001


def test_drafts_from_an_old_zero_weight_template_are_made_without_a_warning(studio):  # noqa: F811
    ws, _template = studio
    ws.write_template(_saved(item_weight=0, item_weight_unit="oz",
                             item_length=0, item_width=0, item_height=0,
                             item_dimensions_unit="in"))
    template = Template.from_dict(ws.read_template())
    _artwork(ws.products / "retro-mountain-sunset.png")
    client = Client(ws)
    report = _run(ws, template, client)
    item = report.items[0]
    assert item.status == stream.OK and item.steps["check"] == "done"
    assert report.warnings == 0, [w.to_dict() for w in item.warnings]
    sent = client.creates[0]
    for name in ("item_weight", "item_weight_unit", "item_length", "item_width",
                 "item_height", "item_dimensions_unit"):
        assert name not in sent


def test_a_measure_etsy_would_refuse_gets_its_own_warning_code(studio):  # noqa: F811
    ws, template = studio
    # A Template made in code keeps what it is given (from_dict and capture clean it).
    raw = Template(template.source_listing_id,
                   fields={**template.fields, "item_weight": 0, "item_weight_unit": "oz"},
                   description=template.description, tags=list(template.tags))
    _artwork(ws.products / "retro-mountain-sunset.png")
    report = _run(ws, raw, Client(ws), dry_run=True)
    assert [(w.code, w.step) for w in report.items[0].warnings] == [("measure_not_sent", "check")]


# --- a decimal comma in a CSV weight ---------------------------------------------------------------


def test_a_decimal_comma_after_a_lone_zero_is_a_decimal():
    assert csvio.as_float("0,250", "item_weight") == 0.25
    assert csvio.as_float("0,5", "item_weight") == 0.5
    assert csvio.as_float("-0,5", "x") == -0.5
    assert csvio.as_float("1,5", "item_weight") == 1.5
    payload = listings.build_payload({**BASE, "item_weight": "0,250", "item_weight_unit": "kg"},
                                     is_update=False)
    assert payload["item_weight"] == 0.25 and payload["item_weight_unit"] == "kg"


@pytest.mark.parametrize("value", ["1,250", "12,500", "00,250"])
def test_a_comma_that_may_separate_thousands_is_still_refused(value):
    with pytest.raises(ValidationError) as caught:
        csvio.as_float(value, "item_weight")
    message = str(caught.value)
    assert message.startswith(f"item_weight {value!r} is ambiguous")
    # both readings are offered, the decimal one with a point
    assert value.replace(",", "") in message and value.replace(",", ".") in message
