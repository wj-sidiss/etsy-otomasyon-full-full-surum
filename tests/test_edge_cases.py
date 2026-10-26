"""Edge cases in validation, pushing and market research.

Each test names the failure it prevents. None of them touch the network.
"""

from pathlib import Path

import httpx
import pytest

from etsyotomasyon.client import MAX_IMAGE_BYTES, EtsyClient, _mime_for
from etsyotomasyon.config import Config
from etsyotomasyon.csvio import as_float, as_int
from etsyotomasyon.errors import ValidationError
from etsyotomasyon.listings import build_payload, prepare, push
from etsyotomasyon.seo import MarketReport, audit_listing, research, suggest_tags

# --- fractional values must not be silently truncated -----------------------


def test_a_fractional_quantity_is_rejected_not_rounded():
    # Rounding `quantity=3.9` to 3 would set a stock level the seller never typed.
    with pytest.raises(ValidationError, match="whole number"):
        as_int("3.9", "quantity")


def test_whole_numbers_written_as_floats_still_work():
    assert as_int("3.0", "quantity") == 3
    assert as_int("3", "quantity") == 3


# --- numeric lower bounds --------------------------------------------------


def test_negative_values_are_rejected_by_the_caster():
    with pytest.raises(ValidationError, match="0 or more"):
        as_int("-3", "quantity", minimum=0)
    with pytest.raises(ValidationError, match="0 or more"):
        as_float("-5", "price", minimum=0)


BASE = {
    "title": "Mug",
    "description": "A mug.",
    "price": "10",
    "quantity": "1",
    "who_made": "i_did",
    "when_made": "made_to_order",
    "taxonomy_id": "1633",
    "shipping_profile_id": "12345",
}


def test_a_negative_price_no_longer_passes_validation():
    with pytest.raises(ValidationError, match="price"):
        build_payload(dict(BASE, price="-5"), is_update=False)


def test_a_zero_price_is_rejected():
    with pytest.raises(ValidationError, match="greater than 0"):
        build_payload(dict(BASE, price="0"), is_update=False)


def test_a_negative_quantity_no_longer_passes_validation():
    with pytest.raises(ValidationError, match="quantity"):
        build_payload(dict(BASE, quantity="-3"), is_update=False)


def test_a_zero_id_is_rejected():
    with pytest.raises(ValidationError, match="taxonomy_id"):
        build_payload(dict(BASE, taxonomy_id="0"), is_update=False)


def test_zero_quantity_is_allowed_because_sold_out_is_real():
    assert build_payload(dict(BASE, quantity="0"), is_update=False)["quantity"] == 0


# --- silently dropped update fields must be reported -------------------------


def test_updating_a_price_warns_that_it_will_not_be_sent():
    warnings: list[str] = []
    payload = build_payload(
        {"title": "New title", "price": "29.99", "quantity": "5"},
        is_update=True,
        warnings=warnings,
    )
    assert payload == {"title": "New title"}
    assert any("price" in w and "quantity" in w for w in warnings)
    assert any("inventory" in w for w in warnings)


# --- --no-images must not police image paths --------------------------------


def test_missing_images_are_ignored_when_uploads_are_off(tmp_path):
    row = dict(BASE, images="nope.jpg")
    with_uploads = prepare([row], base_dir=tmp_path, upload_images=True)
    assert with_uploads[0].result.failed

    without = prepare([row], base_dir=tmp_path, upload_images=False)
    assert not without[0].result.failed
    assert without[0].image_paths == []


# --- the whole file is validated before anything is written -----------------


class _RecordingClient:
    """Stands in for EtsyClient and counts what would have been sent."""

    def __init__(self, fail_images: bool = False):
        self.created: list[dict] = []
        self.images: list[Path] = []
        self.fail_images = fail_images

    def create_draft_listing(self, payload):
        self.created.append(payload)
        return {"listing_id": 700 + len(self.created)}

    def update_listing(self, listing_id, payload):
        return {"listing_id": listing_id}

    def upload_listing_image(self, listing_id, image, *, rank=1, alt_text=""):
        if self.fail_images:
            raise OSError("upload refused")
        self.images.append(image)
        return {"listing_image_id": rank}


def test_one_bad_row_stops_the_whole_file(tmp_path):
    # Pushing row by row, row 1 would become a real draft before row 2 failed, leaving
    # a shop half-populated from a file the seller would never have pushed.
    client = _RecordingClient()
    rows = [dict(BASE), dict(BASE, who_made="me")]
    report = push(client, rows, base_dir=tmp_path)

    assert report.aborted
    assert client.created == []
    assert report.created == 0
    assert report.skipped == 1
    assert report.errors == 1
    assert "Nothing was sent" in report.aborted_reason


def test_partial_opts_back_into_row_by_row(tmp_path):
    client = _RecordingClient()
    rows = [dict(BASE), dict(BASE, who_made="me")]
    report = push(client, rows, base_dir=tmp_path, allow_partial=True)

    assert not report.aborted
    assert len(client.created) == 1
    assert report.created == 1
    assert report.errors == 1


def test_a_clean_file_writes_every_row(tmp_path):
    client = _RecordingClient()
    report = push(client, [dict(BASE), dict(BASE)], base_dir=tmp_path)
    assert not report.aborted
    assert report.created == 2 and report.errors == 0


def test_dry_run_never_aborts_and_still_reports_every_row(tmp_path):
    report = push(None, [dict(BASE), dict(BASE, who_made="me")], base_dir=tmp_path, dry_run=True)
    assert not report.aborted
    assert report.errors == 1
    assert sum(1 for r in report.results if r.status == "dry-run") == 1


# --- a created draft is never reported as nothing ---------------------------


def test_an_image_failure_after_create_reports_the_listing_that_exists(tmp_path):
    image = tmp_path / "a.jpg"
    image.write_bytes(b"x")
    client = _RecordingClient(fail_images=True)

    report = push(client, [dict(BASE, images="a.jpg")], base_dir=tmp_path)
    result = report.results[0]

    assert result.status == "partial"
    assert not result.failed, "the draft was created; calling it a failure hides it"
    assert result.listing_id == 701
    assert "IS in your shop" in result.message
    assert report.created == 1, "a created draft must not be counted as zero"
    assert report.partial == 1


# --- phrase and tag counts are per listing, not per occurrence ----------------


class _FakeSearch:
    def __init__(self, listings):
        self._listings = listings

    def search_active_listings(self, **_kw):
        return iter(self._listings)


def _listing(title, tags, amount=10):
    return {
        "listing_id": abs(hash(title)) % 10000,
        "title": title,
        "tags": tags,
        "price": {"amount": amount * 100, "divisor": 100, "currency_code": "USD"},
        "num_favorers": 1,
    }


def test_a_repeated_phrase_in_a_title_counts_once_per_listing():
    # "appears in 41 of 200 listings" has to count listings, not occurrences, or a
    # share could exceed 100% and the label on screen would be untrue. Two listings that
    # each say it twice must total 2, not 4.
    listings = [
        _listing("ceramic mug ceramic mug", ["mug"]),
        _listing("ceramic mug ceramic mug extra", ["mug"]),
    ]
    report = research(_FakeSearch(listings), "mug")
    assert dict(report.phrases)["ceramic mug"] == 2
    assert report.sampled == 2


def test_a_phrase_seen_in_only_one_listing_is_treated_as_noise():
    # Unchanged by the counting fix, and worth pinning: a single sighting is not
    # evidence of a pattern, so it never reaches the report.
    listings = [_listing("ceramic mug unique phrase", ["mug"]), _listing("other thing", ["x"])]
    report = research(_FakeSearch(listings), "mug")
    assert "unique phrase" not in dict(report.phrases)


def test_a_duplicated_tag_in_one_listing_counts_once():
    report = research(_FakeSearch([_listing("mug", ["gift", "gift", "gift"])]), "mug")
    assert dict(report.tags)["gift"] == 1


def test_no_share_can_exceed_the_sample_size():
    listings = [_listing("ceramic mug ceramic mug ceramic mug", ["mug", "mug"]) for _ in range(3)]
    report = research(_FakeSearch(listings), "mug")
    for _tag, count in report.tags:
        assert count <= report.sampled
    for _phrase, count in report.phrases:
        assert count <= report.sampled


def test_counts_still_accumulate_across_distinct_listings():
    listings = [_listing("ceramic mug one", ["gift"]), _listing("ceramic mug two", ["gift"])]
    report = research(_FakeSearch(listings), "mug")
    assert dict(report.tags)["gift"] == 2
    assert dict(report.phrases)["ceramic mug"] == 2


# --- the audit must flag more tags than Etsy allows --------------------------


def test_the_audit_flags_more_than_thirteen_tags():
    # A listing with 14 tags must not score a clean 100 in the audit when the
    # listing validator rejects it.
    listing = {
        "listing_id": 1,
        "title": "Personalised Ceramic Christmas Ornament Keepsake Gift For Family",
        "description": "A lovely ornament. " * 20,
        "tags": [f"ornament tag {i}" for i in range(14)],
        "materials": ["ceramic"],
        "should_auto_renew": True,
        "state": "active",
    }
    audit = audit_listing(listing)
    codes = {i.code for i in audit.issues}
    assert "tags.too_many" in codes
    assert "tags.unused" not in codes
    assert audit.score < 100


def test_exactly_thirteen_tags_is_not_flagged():
    listing = {
        "listing_id": 1,
        "title": "Personalised Ceramic Christmas Ornament Keepsake Gift For Family",
        "description": "A lovely ornament. " * 20,
        "tags": [f"ornament tag {i}" for i in range(13)],
        "materials": ["ceramic"],
        "should_auto_renew": True,
        "state": "active",
    }
    codes = {i.code for i in audit_listing(listing).issues}
    assert "tags.too_many" not in codes and "tags.unused" not in codes


# --- suggestions are capped by the slots actually free -----------------------


def _report_with(n_tags):
    return MarketReport(
        keyword="mug",
        sampled=100,
        tags=[(f"market tag {i}", 90 - i) for i in range(n_tags)],
        phrases=[],
        price_min=None,
        price_median=None,
        price_max=None,
        currency="USD",
        median_favorers=None,
        top_listings=[],
    )


def test_twelve_existing_tags_leaves_room_for_exactly_one():
    # Offering 13 suggestions to a listing with 12 tags implied you could add them
    # all. Etsy's ceiling is 13 in total, so only one would ever land.
    suggestions = suggest_tags(_report_with(20), existing=[f"mine {i}" for i in range(12)])
    assert suggestions.free_slots == 1
    assert len(suggestions.add_now) == 1
    assert suggestions.needs_a_swap, "the rest are still worth showing, as swaps"


def test_a_full_tag_set_offers_nothing_to_add():
    suggestions = suggest_tags(_report_with(20), existing=[f"mine {i}" for i in range(13)])
    assert suggestions.free_slots == 0
    assert suggestions.add_now == []
    assert suggestions.needs_a_swap


def test_an_empty_listing_can_take_all_thirteen():
    suggestions = suggest_tags(_report_with(20), existing=[])
    assert suggestions.free_slots == 13
    assert len(suggestions.add_now) == 13


def test_over_length_market_tags_are_never_suggested():
    report = MarketReport(
        keyword="mug", sampled=10, tags=[("x" * 25, 9), ("short tag", 8)], phrases=[],
        price_min=None, price_median=None, price_max=None, currency="USD",
        median_favorers=None, top_listings=[],
    )
    assert suggest_tags(report, existing=[]).add_now == ["short tag"]


# --- a format or size Etsy refuses must fail before the draft exists ----------


def test_a_format_etsy_refuses_fails_validation_not_the_upload(tmp_path):
    # A .webp dropped into a product folder passed --dry-run, then Etsy rejected it
    # after the draft had been created, leaving a half-built listing and a "partial".
    (tmp_path / "photo.webp").write_bytes(b"RIFF0000WEBPVP8 ")
    prepared = prepare([dict(BASE, images="photo.webp")], base_dir=tmp_path)
    assert prepared[0].result.failed
    assert "JPG, PNG and GIF" in prepared[0].result.message


def test_an_image_over_etsys_size_cap_is_caught_locally(tmp_path):
    big = tmp_path / "huge.jpg"
    big.write_bytes(b"0" * (MAX_IMAGE_BYTES + 1))
    prepared = prepare([dict(BASE, images="huge.jpg")], base_dir=tmp_path)
    assert prepared[0].result.failed
    assert "20MB" in prepared[0].result.message


def test_an_accepted_format_passes_without_being_decoded(tmp_path):
    # prepare() answers "would the endpoint take this", not "are the pixels intact".
    # Decoding here would walk every byte of every image on every --dry-run.
    (tmp_path / "photo.jpg").write_bytes(b"x")
    assert not prepare([dict(BASE, images="photo.jpg")], base_dir=tmp_path)[0].result.failed


def test_one_unacceptable_image_stops_the_whole_file(tmp_path):
    (tmp_path / "ok.jpg").write_bytes(b"x")
    (tmp_path / "bad.tif").write_bytes(b"x")
    client = _RecordingClient()
    report = push(
        client,
        [dict(BASE, images="ok.jpg"), dict(BASE, images="bad.tif")],
        base_dir=tmp_path,
    )
    assert report.aborted
    assert client.created == [], "row 1 must not become a draft because row 2 is bad"


def test_the_client_never_invents_a_content_type(tmp_path):
    # Etsy takes JPG, PNG and GIF. Anything else must be refused, not sent under a
    # guessed type — a .webp entry would look supported and fail on arrival.
    for name in ("a.bmp", "a.tif", "a.webp"):
        path = tmp_path / name
        path.write_bytes(b"x")
        with pytest.raises(ValidationError, match="JPG, PNG and GIF"):
            _mime_for(path)
    (tmp_path / "a.jpg").write_bytes(b"x")
    assert _mime_for(tmp_path / "a.jpg") == "image/jpeg"


# --- the header every request depends on -----------------------------------------


def test_public_search_still_sends_the_api_key_and_no_bearer():
    # The market search carries no Authorization header, which is correct — but it
    # must still carry x-api-key or Etsy 403s every call.
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["api_key"] = request.headers.get("x-api-key")
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"count": 0, "results": []})

    client = EtsyClient(Config(keystring="KEY", shared_secret="SECRET"), require_auth=False)
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    list(client.search_active_listings(keywords="mug", max_items=1))

    assert seen["api_key"] == "KEY:SECRET"
    assert seen["auth"] is None
