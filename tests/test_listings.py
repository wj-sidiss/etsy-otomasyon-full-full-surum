import pytest

from etsyotomasyon.client import encode_form
from etsyotomasyon.csvio import as_float, split_multi
from etsyotomasyon.errors import EtsyApiError, ValidationError
from etsyotomasyon.listings import bad_tag_chars, build_payload, prepare, push, validate_tags

BASE_ROW = {
    "title": "Handmade Ceramic Mug",
    "description": "A nice mug.",
    "price": "24.00",
    "quantity": "5",
    "who_made": "i_did",
    "when_made": "made_to_order",
    "taxonomy_id": "1633",
    "shipping_profile_id": "12345",
}


def test_minimal_create_row_builds():
    payload = build_payload(dict(BASE_ROW), is_update=False)
    assert payload["title"] == "Handmade Ceramic Mug"
    assert payload["price"] == 24.0
    assert payload["quantity"] == 5
    assert payload["type"] == "physical"


def test_missing_shipping_profile_warns_but_does_not_block_a_draft():
    # etsyotomasyon only creates drafts, and Etsy does not require a shipping profile on a
    # draft. Blocking here would stop a new seller from staging anything at all.
    row = dict(BASE_ROW)
    del row["shipping_profile_id"]
    warnings: list[str] = []
    payload = build_payload(row, is_update=False, warnings=warnings)
    assert payload["title"]
    assert any("shipping_profile_id" in w for w in warnings)


def test_digital_listing_does_not_warn_about_shipping():
    row = dict(BASE_ROW)
    del row["shipping_profile_id"]
    row["type"] = "download"
    warnings: list[str] = []
    assert build_payload(row, is_update=False, warnings=warnings)["type"] == "download"
    assert warnings == []


def test_title_over_limit_rejected():
    row = dict(BASE_ROW, title="x" * 141)
    with pytest.raises(ValidationError, match="141 chars"):
        build_payload(row, is_update=False)


def test_bad_enum_rejected():
    row = dict(BASE_ROW, who_made="me")
    with pytest.raises(ValidationError, match="who_made"):
        build_payload(row, is_update=False)


def test_decimal_comma_price_accepted():
    row = dict(BASE_ROW, price="19,90")
    assert build_payload(row, is_update=False)["price"] == pytest.approx(19.90)


def test_update_row_only_keeps_updatable_fields():
    # price and quantity are deliberately not patchable — they belong to inventory.
    payload = build_payload({"title": "New title", "price": "9.99"}, is_update=True)
    assert payload == {"title": "New title"}


def test_update_requires_at_least_one_field():
    with pytest.raises(ValidationError, match="no updatable fields"):
        build_payload({"listing_id": "1"}, is_update=True)


def test_state_only_on_update():
    row = dict(BASE_ROW, state="active")
    with pytest.raises(ValidationError, match="state can only be set"):
        build_payload(row, is_update=False)


def test_tags_are_split_and_kept():
    row = dict(BASE_ROW, tags="ceramic mug|coffee gift|stoneware")
    assert build_payload(row, is_update=False)["tags"] == ["ceramic mug", "coffee gift", "stoneware"]


def test_too_many_tags_rejected():
    row = dict(BASE_ROW, tags="|".join(f"tag{i}" for i in range(14)))
    with pytest.raises(ValidationError, match="Etsy allows 13"):
        build_payload(row, is_update=False)


def test_tag_over_20_chars_rejected():
    row = dict(BASE_ROW, tags="this tag is far too long to be accepted")
    with pytest.raises(ValidationError, match="max 20"):
        build_payload(row, is_update=False)


def test_unicode_tags_are_allowed():
    # Turkish, German and French letters must survive — str.isalnum is Unicode-aware.
    assert bad_tag_chars("çiçek düğme") == set()
    assert bad_tag_chars("café münze") == set()
    assert validate_tags(["çiçek", "düğme hediyesi"]) == []


def test_disallowed_tag_symbol_flagged():
    assert bad_tag_chars("mug!") == {"!"}
    assert validate_tags(["mug!"])


def test_duplicate_tags_flagged():
    assert any("duplicate" in p for p in validate_tags(["mug", "Mug"]))


def test_more_images_than_etsy_allows_is_an_error_not_a_truncation(tmp_path):
    # Etsy takes the create and then refuses the 21st upload, and etsyotomasyon holds no
    # delete scope to undo it. Dropping the extras silently would pick which photos the
    # seller ships; failing the row leaves that choice with them.
    for n in range(21):
        (tmp_path / f"{n}.jpg").write_bytes(b"x")
    row = dict(BASE_ROW, images="|".join(f"{n}.jpg" for n in range(21)))

    result = prepare([row], base_dir=tmp_path)[0].result

    assert result.failed
    assert "21 images given" in result.message
    assert "Etsy allows 20" in result.message


def test_exactly_ten_images_is_a_valid_row(tmp_path):
    # Ten is a full listing, not an error — the boundary has to stay usable.
    for n in range(10):
        (tmp_path / f"{n}.jpg").write_bytes(b"x")
    row = dict(BASE_ROW, images="|".join(f"{n}.jpg" for n in range(10)))

    assert not prepare([row], base_dir=tmp_path)[0].result.failed


def test_image_count_is_not_policed_when_uploads_are_off(tmp_path):
    # --no-images sends nothing, so an eleventh path cannot reach Etsy. Failing the row
    # here would stop a seller fixing their titles over a column this run ignores.
    row = dict(BASE_ROW, images="|".join(f"{n}.jpg" for n in range(11)))

    assert not prepare([row], base_dir=tmp_path, upload_images=False)[0].result.failed


def test_encode_form_joins_lists_with_commas():
    # Etsy documents tags as a comma-separated string; repeated keys silently lose data.
    encoded = encode_form({"tags": ["a", "b", "c"], "quantity": 3, "is_supply": False})
    assert encoded == {"tags": "a,b,c", "quantity": "3", "is_supply": "false"}


def test_encode_form_drops_none_and_empty_lists():
    assert encode_form({"a": None, "b": [], "c": 1}) == {"c": "1"}


def test_a_thousands_separator_is_refused_rather_than_read_as_a_decimal_point():
    # Read as 1.299, '1,299' would list a 1.299 TL poster for one lira thirty, and
    # `price > 0` would wave it through. Nothing about its shape distinguishes it from
    # '19,90', so the ambiguous one has to be an error rather than a guess.
    assert as_float("19,90", "price") == 19.90
    assert as_float("19.90", "price") == 19.90
    assert as_float("1299", "price") == 1299.0
    with pytest.raises(ValidationError) as caught:
        as_float("1,299", "price")
    assert "1299" in str(caught.value)


def test_a_comma_in_an_image_path_does_not_split_the_cell():
    # A one-image row has no pipe, so a comma fallback would tear a perfectly good
    # path in two and push would report both halves as missing files.
    assert split_multi("kedi, kopek tablosu--flat.jpg") == ["kedi, kopek tablosu--flat.jpg"]
    assert split_multi("a.jpg|b.jpg") == ["a.jpg", "b.jpg"]


def test_a_processing_profile_is_sent_and_replaces_the_day_counts():
    # Etsy refuses a physical create without readiness_state_id, and the profile is what
    # defines processing time — the older day counts must not contradict it.
    row = dict(BASE_ROW, readiness_state_id="9001", processing_min="1", processing_max="2")
    payload = build_payload(row, is_update=False)
    assert payload["readiness_state_id"] == 9001
    assert "processing_min" not in payload and "processing_max" not in payload


def test_day_counts_still_go_out_without_a_profile():
    payload = build_payload(dict(BASE_ROW, processing_min="1", processing_max="2"), is_update=False)
    assert payload["processing_min"] == 1 and payload["processing_max"] == 2


def test_a_quantity_over_etsys_limit_is_caught_before_sending():
    problems = []
    try:
        build_payload(dict(BASE_ROW, quantity="12000"), is_update=False)
    except ValidationError as exc:
        problems.append(str(exc))
    assert problems and "limit of 999" in problems[0]


# --- review fixes -------------------------------------------------------------------------


_INVENTORY = {
    "products": [{
        "product_id": 1, "sku": "", "is_deleted": False,
        "property_values": [{"property_id": 513, "property_name": "Size", "value_ids": [21],
                             "values": ["Large"], "scale_id": None}],
        # Not active, or not asked shop-scoped: getListingInventory leaves the profile out.
        "offerings": [{"offering_id": 9, "is_deleted": False, "is_enabled": True, "quantity": 5,
                       "price": {"amount": 1250, "divisor": 100, "currency_code": "USD"}}],
    }],
}


def test_every_copied_offering_names_a_processing_profile():
    # updateListingInventory: offerings require price, quantity, is_enabled AND
    # readiness_state_id (nullable). The key is never left out.
    from etsyotomasyon.listings import inventory_for_copy

    body = inventory_for_copy(_INVENTORY)
    assert body["products"][0]["offerings"] == [
        {"price": 12.5, "quantity": 5, "is_enabled": True, "readiness_state_id": None}
    ]
    body = inventory_for_copy(_INVENTORY, readiness_state_id=801)
    assert body["products"][0]["offerings"][0]["readiness_state_id"] == 801
    own = {"products": [{**_INVENTORY["products"][0], "offerings": [
        {**_INVENTORY["products"][0]["offerings"][0], "readiness_state_id": 7}]}]}
    assert inventory_for_copy(own, readiness_state_id=801)["products"][0]["offerings"][0][
        "readiness_state_id"] == 7


def test_a_new_draft_gets_its_own_profile_on_offerings_that_have_none():
    from pathlib import Path

    from etsyotomasyon.listings import inventory_for_copy, push

    class Client:
        def __init__(self):
            self.inventories = []

        def create_draft_listing(self, fields):
            return {"listing_id": 1000001}

        def update_listing_inventory(self, listing_id, inventory):
            self.inventories.append(inventory)

    client = Client()
    shared = inventory_for_copy(_INVENTORY)
    row = dict(BASE_ROW, readiness_state_id="801")
    report = push(client, [row], base_dir=Path("."), inventory=shared)
    assert report.results[0].status == "ok", report.results[0].message
    assert client.inventories[0]["products"][0]["offerings"][0]["readiness_state_id"] == 801
    # The body shared by every draft of the run is not changed by one of them.
    assert shared["products"][0]["offerings"][0]["readiness_state_id"] is None


def test_an_image_resolver_refuses_before_anything_is_looked_at(tmp_path):
    seen = []

    def resolver(value):
        seen.append(value)
        if value.startswith("/"):
            raise ValidationError(f"image {value!r}: images must be inside the folder")
        return tmp_path / value

    (tmp_path / "a.jpg").write_bytes(b"x")
    rows = [dict(BASE_ROW, images="a.jpg"), dict(BASE_ROW, images="a.jpg|/etc/b.jpg")]
    good, bad = prepare(rows, base_dir=tmp_path, image_resolver=resolver)
    assert not good.result.failed and good.image_paths == [tmp_path / "a.jpg"]
    assert bad.result.failed and "inside the folder" in bad.result.message
    assert seen == ["a.jpg", "a.jpg", "/etc/b.jpg"]
    # Without a resolver (the CLI) the CSV's own folder and absolute paths still work.
    assert not prepare([rows[0]], base_dir=tmp_path)[0].result.failed


def test_pull_has_no_views_column():
    from etsyotomasyon.listings import pull

    class Source:
        def listings_by_shop(self, state="active", **_kw):
            return iter([{"listing_id": 1000001, "title": "Example", "num_favorers": 3}])

    row = pull(Source())[0]
    assert "views" not in row and row["num_favorers"] == 3


# --- digital listings (type download / both) --------------------------------------------


def test_a_download_create_needs_no_shipping_and_sends_none(tmp_path):
    # createDraftListing (OAS): shipping_profile_id is required "when listing type is
    # physical"; a processing profile only exists for physical listings.
    row = dict(BASE_ROW, type="download", readiness_state_id="801", item_weight="2",
               item_weight_unit="oz")
    warnings: list[str] = []
    payload = build_payload(row, is_update=False, warnings=warnings)
    assert payload["type"] == "download"
    for name in ("shipping_profile_id", "readiness_state_id", "item_weight", "item_weight_unit"):
        assert name not in payload
    assert warnings == [
        "type is download, so nothing is shipped: shipping_profile_id, readiness_state_id, "
        "item_weight, item_weight_unit not sent"
    ]


def test_both_keeps_the_physical_rules():
    row = dict(BASE_ROW, type="both")
    del row["shipping_profile_id"]
    warnings: list[str] = []
    payload = build_payload(row, is_update=False, warnings=warnings)
    assert payload["type"] == "both"
    assert any(w.startswith("no shipping_profile_id") for w in warnings)
    kept = build_payload(dict(BASE_ROW, type="both"), is_update=False)
    assert kept["shipping_profile_id"] == 12345


@pytest.mark.parametrize("title, why", [
    ("Salt & Pepper & Co Mug", "'&' 2 times"),
    ("Sunset Mug \U0001f338", "characters Etsy does not accept"),
    ("Mug for $5", "characters Etsy does not accept"),
    ("50% off, 100% cotton", "'%' 2 times"),
])
def test_a_csv_title_is_held_to_etsys_character_rule(title, why):
    with pytest.raises(ValidationError, match=why):
        build_payload(dict(BASE_ROW, title=title), is_update=False)
    with pytest.raises(ValidationError, match=why):
        build_payload({"title": title}, is_update=True)


def test_titles_etsy_accepts_still_pass():
    for title in ("Mom's Mug | Tea & Coffee ™", "Çiçekli Kupa – Hediye: 11oz"):
        assert build_payload(dict(BASE_ROW, title=title), is_update=False)["title"] == title


def _files_row(tmp_path, *names, listing_type="download", **extra):
    for name in names:
        (tmp_path / name).write_bytes(b"%PDF-1.4 " + name.encode())
    return dict(BASE_ROW, type=listing_type, files="|".join(names), **extra)


def test_the_files_column_goes_with_a_digital_create(tmp_path):
    row = _files_row(tmp_path, "planner.pdf", "extras.zip")
    item = prepare([row], base_dir=tmp_path)[0]
    assert not item.result.failed
    assert [p.name for p in item.file_paths] == ["planner.pdf", "extras.zip"]
    both = prepare([_files_row(tmp_path, "planner.pdf", listing_type="both")], base_dir=tmp_path)
    assert [p.name for p in both[0].file_paths] == ["planner.pdf"]


def test_files_on_a_physical_row_are_refused(tmp_path):
    # Attaching a file to a physical listing turns it digital and drops its shipping and
    # variations (OAS uploadListingFile), so it is never done by accident.
    item = prepare([_files_row(tmp_path, "planner.pdf", listing_type="physical")],
                   base_dir=tmp_path)[0]
    assert item.result.failed and "type is physical" in item.result.message


def test_files_on_an_update_row_are_left_out_with_a_warning(tmp_path):
    row = _files_row(tmp_path, "planner.pdf", listing_id="1000001")
    item = prepare([row], base_dir=tmp_path)[0]
    assert not item.result.failed and item.file_paths == []
    assert any("only attached to new drafts" in w for w in item.result.warnings)


def test_a_download_without_files_is_created_with_a_warning(tmp_path):
    item = prepare([dict(BASE_ROW, type="download", when_made="2020_2026")],
                   base_dir=tmp_path)[0]
    assert not item.result.failed and item.file_paths == []
    assert any("no files are given" in w for w in item.result.warnings)


def test_files_etsy_would_refuse_fail_the_row_before_anything_is_sent(tmp_path, monkeypatch):
    from etsyotomasyon import client as client_mod

    many = _files_row(tmp_path, *(f"page-{n}.pdf" for n in range(6)))
    assert "Etsy allows 5" in prepare([many], base_dir=tmp_path)[0].result.message
    missing = dict(BASE_ROW, type="download", files="gone.pdf")
    assert "file not found" in prepare([missing], base_dir=tmp_path)[0].result.message
    program = _files_row(tmp_path, "setup.exe")
    assert "cannot be sold as a download" in prepare([program], base_dir=tmp_path)[0].result.message
    monkeypatch.setattr(client_mod, "MAX_FILE_BYTES", 5)
    big = _files_row(tmp_path, "planner.pdf")
    assert "limit for a digital file" in prepare([big], base_dir=tmp_path)[0].result.message


class FileClient:
    def __init__(self, fail_file=None):
        self.calls = []
        self.fail_file = fail_file

    def create_draft_listing(self, fields):
        self.calls.append(("create", fields["type"]))
        return {"listing_id": 1000001}

    def upload_listing_image(self, listing_id, image, *, rank):
        self.calls.append(("image", image.name, rank))

    def upload_listing_file(self, listing_id, path, *, rank):
        if path.name == self.fail_file:
            raise EtsyApiError(400, "refused", method="POST", path="/files")
        self.calls.append(("file", path.name, rank))


def test_push_uploads_the_files_after_the_images_in_order(tmp_path):
    (tmp_path / "front.jpg").write_bytes(b"x")
    row = _files_row(tmp_path, "planner.pdf", "extras.zip", images="front.jpg")
    client = FileClient()
    report = push(client, [row], base_dir=tmp_path, upload_images=True)
    result = report.results[0]
    assert result.status == "ok" and result.files_uploaded == 2 and report.files == 2
    assert result.message == "created as draft; 2 download files attached"
    assert client.calls == [("create", "download"), ("image", "front.jpg", 1),
                            ("file", "planner.pdf", 1), ("file", "extras.zip", 2)]


def test_a_file_etsy_refuses_after_the_create_is_partial(tmp_path):
    row = _files_row(tmp_path, "planner.pdf", "extras.zip")
    client = FileClient(fail_file="extras.zip")
    result = push(client, [row], base_dir=tmp_path).results[0]
    assert result.status == "partial" and result.files_uploaded == 1
    assert "file 2 of 2 (extras.zip) failed" in result.message
    assert "add the download file in Etsy" in result.message


def test_a_dry_run_counts_the_files(tmp_path):
    report = push(None, [_files_row(tmp_path, "planner.pdf")], base_dir=tmp_path, dry_run=True)
    assert report.results[0].message.endswith("0 image(s), 1 file(s)")


def test_files_are_left_alone_when_file_uploads_are_off(tmp_path):
    row = dict(BASE_ROW, type="download", files="not-there.pdf")
    item = prepare([row], base_dir=tmp_path, upload_files=False)[0]
    assert not item.result.failed and item.file_paths == []
