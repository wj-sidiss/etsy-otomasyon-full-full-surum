"""How etsyotomasyon meets the contract of Etsy's API.

Each test names the failure it prevents. None of them touch the network.
"""

import httpx
import pytest

from etsyotomasyon import client as client_mod
from etsyotomasyon.auth import Token
from etsyotomasyon.client import EtsyClient
from etsyotomasyon.config import Config
from etsyotomasyon.errors import ConfigError, EtsyApiError, ValidationError
from etsyotomasyon.listings import build_payload
from etsyotomasyon.seo import MarketReport, research

# --- x-api-key needs BOTH halves ------------------------------------------------
# Etsy: 403 {"error":"Invalid API key: should be in the format 'keystring:shared_secret'."}


def test_api_key_header_joins_keystring_and_secret():
    config = Config(keystring="KEY", shared_secret="SECRET")
    assert config.api_key_header == "KEY:SECRET"


def test_client_sends_the_joined_header(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["x-api-key"] = request.headers.get("x-api-key")
        return httpx.Response(200, json={"ok": True})

    client = EtsyClient(Config(keystring="KEY", shared_secret="SECRET"), require_auth=False)
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    client.ping()
    assert seen["x-api-key"] == "KEY:SECRET"


def test_oauth_client_id_stays_the_bare_keystring():
    # The trap in a naive fix: colon-joining the client_id too would break login.
    config = Config(keystring="KEY", shared_secret="SECRET")
    assert config.keystring == "KEY"
    assert ":" not in config.keystring


def test_load_reads_both_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("ETSYOTOMASYON_HOME", str(tmp_path))
    monkeypatch.setenv("ETSY_KEYSTRING", "KEY")
    monkeypatch.setenv("ETSY_SHARED_SECRET", "SECRET")
    config = Config.load()
    assert (config.keystring, config.shared_secret) == ("KEY", "SECRET")


def test_load_splits_a_pasted_colon_form(monkeypatch, tmp_path):
    # People will paste the header value straight into ETSY_KEYSTRING. Splitting it
    # keeps the header right AND keeps client_id from being corrupted.
    monkeypatch.setenv("ETSYOTOMASYON_HOME", str(tmp_path))
    monkeypatch.setenv("ETSY_KEYSTRING", "KEY:SECRET")
    monkeypatch.delenv("ETSY_SHARED_SECRET", raising=False)
    config = Config.load()
    assert config.keystring == "KEY"
    assert config.shared_secret == "SECRET"
    assert config.api_key_header == "KEY:SECRET"


def test_load_explains_a_missing_shared_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("ETSYOTOMASYON_HOME", str(tmp_path))
    monkeypatch.setenv("ETSY_KEYSTRING", "KEY")
    monkeypatch.delenv("ETSY_SHARED_SECRET", raising=False)
    with pytest.raises(ConfigError, match="ETSY_SHARED_SECRET"):
        Config.load()


# --- pull -> push round trip ----------------------------------------------------


@pytest.mark.parametrize("state", ["draft", "expired", "sold_out"])
def test_unsettable_states_round_trip_instead_of_failing(state):
    # `listings pull --state draft` writes state=draft. Pushing that file back used
    # to fail every single row, breaking the workflow the CLI advertises.
    payload = build_payload({"title": "New title", "state": state}, is_update=True)
    assert payload == {"title": "New title"}


@pytest.mark.parametrize("state", ["active", "inactive"])
def test_settable_states_are_still_sent(state):
    assert build_payload({"state": state}, is_update=True)["state"] == state


def test_a_nonsense_state_is_still_an_error():
    with pytest.raises(ValidationError, match="not an Etsy listing state"):
        build_payload({"title": "x", "state": "banana"}, is_update=True)


# --- retries must not duplicate writes -----------------------------------------


def _counting_client(monkeypatch, status):
    monkeypatch.setattr(client_mod.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(status, json={"error": "boom"})

    client = EtsyClient(Config(keystring="K", shared_secret="S"), require_auth=False)
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    return client, calls


def test_get_is_retried_on_5xx(monkeypatch):
    client, calls = _counting_client(monkeypatch, 500)
    with pytest.raises(EtsyApiError):
        client.get("/openapi-ping", authed=False)
    assert calls["n"] == client_mod.MAX_ATTEMPTS


def test_post_is_not_retried_on_5xx(monkeypatch):
    # Etsy has no idempotency key. A retried tracking POST emails the buyer twice.
    client, calls = _counting_client(monkeypatch, 500)
    with pytest.raises(EtsyApiError):
        client.post("/shops/1/receipts/2/tracking", json_body={"tracking_code": "X"}, authed=False)
    assert calls["n"] == 1


def test_post_is_still_retried_on_429(monkeypatch):
    # 429 means Etsy refused the request, not that it acted on it.
    client, calls = _counting_client(monkeypatch, 429)
    with pytest.raises(EtsyApiError):
        client.post("/shops/1/listings", form={"title": "x"}, authed=False)
    assert calls["n"] == client_mod.MAX_ATTEMPTS


def test_post_is_not_retried_after_a_read_timeout(monkeypatch):
    monkeypatch.setattr(client_mod.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("timed out", request=request)

    client = EtsyClient(Config(keystring="K", shared_secret="S"), require_auth=False)
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(EtsyApiError, match="may still have been accepted"):
        client.post("/shops/1/receipts/2/tracking", json_body={"t": "x"}, authed=False)
    assert calls["n"] == 1


def test_post_is_retried_when_the_connection_never_opened(monkeypatch):
    # A failed connect provably never reached Etsy, so repeating it is safe.
    monkeypatch.setattr(client_mod.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("no route", request=request)

    client = EtsyClient(Config(keystring="K", shared_secret="S"), require_auth=False)
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(EtsyApiError):
        client.post("/shops/1/listings", form={"title": "x"}, authed=False)
    assert calls["n"] == client_mod.MAX_ATTEMPTS


# --- price bands must not mix currencies ----------------------------------------


class _FakeSearchClient:
    def __init__(self, listings):
        self._listings = listings

    def search_active_listings(self, **_kw):
        return iter(self._listings)


def _listing(amount, code, title="ceramic coffee mug"):
    return {
        "listing_id": amount,
        "title": title,
        "tags": ["ceramic mug"],
        "price": {"amount": amount * 100, "divisor": 100, "currency_code": code},
        "num_favorers": 3,
    }


def test_price_band_uses_the_dominant_currency_only():
    # A EUR 15, a USD 15 and a TRY 15 are not the same number. Pooling them produced
    # a band labelled with whichever currency happened to arrive first.
    listings = [_listing(10, "USD"), _listing(20, "USD"), _listing(9999, "TRY")]
    report = research(_FakeSearchClient(listings), "mug")
    assert report.currency == "USD"
    assert report.price_min == 10 and report.price_max == 20
    assert report.price_sample == 2
    assert report.currency_count == 2
    assert report.price_band_is_partial


def test_single_currency_sample_is_not_flagged_as_partial():
    report = research(_FakeSearchClient([_listing(10, "USD"), _listing(20, "USD")]), "mug")
    assert report.currency_count == 1
    assert not report.price_band_is_partial
    assert report.price_sample == 2


def test_market_report_defaults_keep_older_callers_working():
    report = MarketReport(
        keyword="k", sampled=0, tags=[], phrases=[], price_min=None, price_median=None,
        price_max=None, currency="", median_favorers=None, top_listings=[],
    )
    assert report.price_sample == 0 and not report.price_band_is_partial


# --- stored scopes must be what Etsy granted ------------------------------------


def test_granted_scopes_win_over_requested_scopes():
    # auth status is where a user looks to debug a 403. Showing the request would
    # hide the exact cause: Etsy granted less than we asked for.
    token = Token.from_response(
        {"access_token": "1.a", "refresh_token": "r", "expires_in": 3600, "scope": "shops_r listings_r"},
        ("shops_r", "listings_r", "listings_w"),
    )
    assert token.scopes == ("shops_r", "listings_r")
    assert token.missing_scopes(("shops_r", "listings_r", "listings_w")) == ("listings_w",)


def test_requested_scopes_are_used_when_etsy_returns_none():
    token = Token.from_response(
        {"access_token": "1.a", "refresh_token": "r", "expires_in": 3600},
        ("shops_r", "listings_r"),
    )
    assert token.scopes == ("shops_r", "listings_r")
    assert token.missing_scopes(("shops_r",)) == ()


# --- digital downloads: uploadListingFile ------------------------------------------------
# OAS: POST /v3/application/shops/{shop_id}/listings/{listing_id}/files (listings_w),
# multipart/form-data {listing_file_id?, file (binary), name, rank (>=1, default 1)};
# 201 -> ShopListingFile. Sending the file under any other field name (an "image"
# copied from uploadListingImage, say) gets a 400 after the draft already exists.


def test_upload_listing_file_uses_the_oas_multipart_fields(tmp_path):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"listing_file_id": 1, "listing_id": 1000001,
                                         "rank": 1, "filename": "planner.pdf"})

    token = Token("1.a", "r", 4102444800, ("listings_w",))
    client = EtsyClient(Config(keystring="KEY123", shared_secret="S", shop_id=12345678),
                        token=token, transport=httpx.MockTransport(handler))
    path = tmp_path / "planner.pdf"
    path.write_bytes(b"%PDF-1.4")
    assert client.upload_listing_file(1000001, path)["listing_file_id"] == 1
    request = seen[0]
    assert (request.method, request.url.path) == (
        "POST", "/v3/application/shops/12345678/listings/1000001/files")
    body = request.content
    for field in (b'name="file"; filename="planner.pdf"', b'name="name"', b'name="rank"'):
        assert field in body
    assert b'name="image"' not in body and b'name="listing_file_id"' not in body


def test_a_download_draft_is_created_as_a_download_without_a_shipping_profile():
    # createDraftListing: shipping_profile_id is "Required when listing type is
    # physical"; type enum physical | download | both.
    payload = build_payload({
        "title": "Boho Planner Printable", "description": "PDF planner.", "price": "4.5",
        "quantity": "999", "who_made": "i_did", "when_made": "2020_2026",
        "taxonomy_id": "2078", "type": "download", "shipping_profile_id": "501",
    }, is_update=False)
    assert payload["type"] == "download" and "shipping_profile_id" not in payload
