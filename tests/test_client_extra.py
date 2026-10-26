"""The EtsyClient wrappers the web app added, and its thread safety.

Every path, parameter and scope here was checked against Etsy's OpenAPI spec
(https://www.etsy.com/openapi/generated/oas/3.0.0.json). None of these tests
touch the network: requests go to an httpx.MockTransport.
"""

from __future__ import annotations

import threading
import time
import urllib.parse

import httpx
import pytest

from etsyotomasyon import auth
from etsyotomasyon import client as client_mod
from etsyotomasyon.client import EtsyClient, taxonomy_path, walk_taxonomy
from etsyotomasyon.config import Config
from etsyotomasyon.errors import EtsyApiError, ValidationError

SHOP = 12345678
PREFIX = "/v3/application"


def make_client(handler, *, token: auth.Token | None = None) -> EtsyClient:
    config = Config(keystring="KEY123", shared_secret="SECRET456", shop_id=SHOP, rate_per_sec=10)
    token = token or auth.Token("1.access", "1.refresh", time.time() + 3600, ("transactions_r",))
    return EtsyClient(config, token=token, transport=httpx.MockTransport(handler))


def recorder(responses: dict[str, object]):
    """A handler answering by path (without the /v3/application prefix)."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path[len(PREFIX):]
        body = responses.get(path)
        if body is None:
            return httpx.Response(404, json={"error": f"no {path}"})
        if callable(body):
            body = body(request)
        return httpx.Response(200, json=body)

    return handler, seen


def query(request: httpx.Request) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(request.url.query.decode()))


def test_listings_by_shop_sends_state_sort_and_includes():
    handler, seen = recorder({f"/shops/{SHOP}/listings": {"count": 1, "results": [{"listing_id": 1000001}]}})
    client = make_client(handler)
    rows = list(client.listings_by_shop(
        "draft", includes=["Images"], sort_on="updated", sort_order="desc", max_items=5
    ))
    assert rows == [{"listing_id": 1000001}]
    q = query(seen[0])
    assert q["state"] == "draft" and q["sort_on"] == "updated" and q["sort_order"] == "desc"
    assert q["includes"] == "Images"
    assert seen[0].headers["authorization"] == "Bearer 1.access"


def test_listings_by_shop_leaves_unset_sorting_to_etsy():
    handler, seen = recorder({f"/shops/{SHOP}/listings": {"count": 0, "results": []}})
    list(make_client(handler).listings_by_shop())
    q = query(seen[0])
    assert "sort_on" not in q and "sort_order" not in q and q["state"] == "active"


def test_counts_ask_for_one_row_and_read_count():
    handler, seen = recorder({
        f"/shops/{SHOP}/listings": {"count": 42, "results": [{}]},
        f"/shops/{SHOP}/receipts": {"count": 7, "results": [{}]},
    })
    client = make_client(handler)
    assert client.count_listings("active") == 42
    assert client.count_receipts(was_shipped=False, min_created=1700000000) == 7
    listings_q, receipts_q = query(seen[0]), query(seen[1])
    assert listings_q["limit"] == "1" and listings_q["state"] == "active"
    assert receipts_q["limit"] == "1" and receipts_q["was_shipped"] == "false"
    assert receipts_q["min_created"] == "1700000000"


def test_a_count_missing_from_the_answer_is_zero():
    handler, _ = recorder({f"/shops/{SHOP}/listings": {"results": []}})
    assert make_client(handler).count_listings("draft") == 0


def test_listing_images_come_back_in_rank_order():
    handler, seen = recorder({"/listings/1000001/images": {"count": 2, "results": [
        {"listing_image_id": 2, "rank": 2}, {"listing_image_id": 1, "rank": 1},
    ]}})
    images = make_client(handler).listing_images(1000001)
    assert [i["listing_image_id"] for i in images] == [1, 2]
    assert seen[0].method == "GET"


def test_listings_batch_chunks_at_100_ids_and_drops_duplicates():
    def answer(request):
        ids = query(request)["listing_ids"].split(",")
        return {"count": len(ids), "results": [{"listing_id": int(i)} for i in ids]}

    handler, seen = recorder({"/listings/batch": answer})
    ids = list(range(1000001, 1000251)) + [1000001]
    rows = make_client(handler).listings_batch(ids, includes=["Images"])
    assert len(rows) == 250
    assert [len(query(r)["listing_ids"].split(",")) for r in seen] == [100, 100, 50]
    assert all(query(r)["includes"] == "Images" for r in seen)


def test_receipt_and_its_payments():
    handler, seen = recorder({
        f"/shops/{SHOP}/receipts/2000001": {"receipt_id": 2000001},
        f"/shops/{SHOP}/receipts/2000001/payments": {"count": 1, "results": [{"payment_id": 3000001}]},
    })
    client = make_client(handler)
    assert client.receipt(2000001) == {"receipt_id": 2000001}
    assert client.receipt_payments(2000001) == [{"payment_id": 3000001}]


def test_payments_send_ids_comma_separated():
    handler, seen = recorder({f"/shops/{SHOP}/payments": {"count": 1, "results": [{"payment_id": 3000001}]}})
    assert make_client(handler).payments([3000001, 3000002]) == [{"payment_id": 3000001}]
    assert query(seen[0])["payment_ids"] == "3000001,3000002"


def test_payments_of_nothing_asks_nothing():
    handler, seen = recorder({})
    assert make_client(handler).payments([]) == []
    assert seen == []


def test_shop_transactions_paginate():
    pages = {"0": [{"transaction_id": n} for n in range(100)], "100": [{"transaction_id": 100}]}

    def answer(request):
        offset = query(request)["offset"]
        return {"count": 101, "results": pages[offset]}

    handler, seen = recorder({f"/shops/{SHOP}/transactions": answer})
    rows = list(make_client(handler).shop_transactions())
    assert len(rows) == 101 and len(seen) == 2


def test_ledger_entries_need_both_bounds():
    handler, seen = recorder({
        f"/shops/{SHOP}/payment-account/ledger-entries": {"count": 1, "results": [{"entry_id": 1}]}
    })
    rows = list(make_client(handler).ledger_entries(1700000000, 1702592000))
    assert rows == [{"entry_id": 1}]
    q = query(seen[0])
    assert q["min_created"] == "1700000000" and q["max_created"] == "1702592000"


def test_readiness_state_definitions():
    handler, seen = recorder({
        f"/shops/{SHOP}/readiness-state-definitions": {"count": 1, "results": [
            {"readiness_state_id": 5, "readiness_state": "made_to_order",
             "min_processing_days": 1, "max_processing_days": 3},
        ]}
    })
    assert make_client(handler).readiness_state_definitions()[0]["readiness_state_id"] == 5


def test_taxonomy_path_names_a_node_by_its_trail():
    nodes = [{"id": 1, "name": "Home & Living", "children": [
        {"id": 2, "name": "Kitchen", "children": [{"id": 3, "name": "Mugs", "children": []}]},
    ]}]
    assert taxonomy_path(nodes, 3) == "Home & Living > Kitchen > Mugs"
    assert taxonomy_path(nodes, "2") == "Home & Living > Kitchen"
    assert taxonomy_path(nodes, 99) == ""
    assert taxonomy_path(nodes, "not a number") == ""
    assert [node_id for node_id, _ in walk_taxonomy(nodes)] == [1, 2, 3]


# --- thread safety --------------------------------------------------------------


def test_an_expired_token_is_refreshed_once_for_many_threads(monkeypatch):
    refreshes = []

    def refresh(token, config):
        refreshes.append(token.access_token)
        time.sleep(0.05)  # widen the window two threads could both refresh in
        return auth.Token("1.fresh", "1.refresh2", time.time() + 3600, token.scopes)

    monkeypatch.setattr(client_mod.auth, "refresh", refresh)
    handler, seen = recorder({f"/shops/{SHOP}": {"shop_id": SHOP}})
    expired = auth.Token("1.stale", "1.refresh", time.time() - 10, ())
    client = make_client(handler, token=expired)
    threads = [threading.Thread(target=client.shop) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert refreshes == ["1.stale"]
    assert {r.headers["authorization"] for r in seen} == {"Bearer 1.fresh"}


def test_a_401_is_refreshed_once_even_when_several_threads_see_it(monkeypatch):
    refreshes = []

    def refresh(token, config):
        refreshes.append(token.access_token)
        time.sleep(0.05)
        return auth.Token("1.fresh", "1.refresh2", time.time() + 3600, token.scopes)

    monkeypatch.setattr(client_mod.auth, "refresh", refresh)

    def handler(request):
        if request.headers["authorization"] == "Bearer 1.revoked":
            return httpx.Response(401, json={"error": "invalid_token"})
        return httpx.Response(200, json={"shop_id": SHOP})

    revoked = auth.Token("1.revoked", "1.refresh", time.time() + 3600, ())
    client = make_client(handler, token=revoked)
    barrier = threading.Barrier(4)

    def call():
        barrier.wait()
        client.shop()

    threads = [threading.Thread(target=call) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert refreshes == ["1.revoked"]


def test_the_attempt_cap_applies_to_the_calling_thread_only(monkeypatch):
    monkeypatch.setattr(client_mod.time, "sleep", lambda _s: None)
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("down", request=request)

    client = make_client(handler)
    with client.attempts(2), pytest.raises(EtsyApiError) as error:
        client.ping()
    assert error.value.status == 0 and len(calls) == 2
    calls.clear()
    with pytest.raises(EtsyApiError):
        client.ping()
    assert len(calls) == client_mod.MAX_ATTEMPTS


def test_identity_is_looked_up_once_across_threads():
    config = Config(keystring="KEY123", shared_secret="SECRET456", rate_per_sec=10)
    lookups = []

    def handler(request):
        path = request.url.path[len(PREFIX):]
        lookups.append(path)
        if path == "/users/me":
            time.sleep(0.05)
            return httpx.Response(200, json={"user_id": 1})
        if path == "/users/1/shops":
            return httpx.Response(200, json={"shop_id": SHOP})
        return httpx.Response(200, json={"shop_id": SHOP})

    token = auth.Token("1.access", "1.refresh", time.time() + 3600, ())
    client = EtsyClient(config, token=token, transport=httpx.MockTransport(handler))
    threads = [threading.Thread(target=client.shop_id) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert lookups.count("/users/me") == 1 and lookups.count("/users/1/shops") == 1


# --- review fixes: entities, pagination, the 401 re-send ---------------------------------------

ESCAPED = {
    "listing_id": 1000001,
    "title": "Mom&#39;s &quot;Best&quot; Coffee Mug &amp; Gift",
    "description": "A mug for Mom&#39;s coffee &amp; tea.\nDishwasher safe &lt;3",
    "tags": ["mother&#39;s day", "mom &amp; dad", "plain tag"],
    "materials": ["ceramic &amp; glaze"],
    "style": ["Boho &amp; Chic"],
    "price": {"amount": 1800, "divisor": 100, "currency_code": "USD"},
}
PLAIN = {
    "title": "Mom's \"Best\" Coffee Mug & Gift",
    "description": "A mug for Mom's coffee & tea.\nDishwasher safe <3",
    "tags": ["mother's day", "mom & dad", "plain tag"],
    "materials": ["ceramic & glaze"],
    "style": ["Boho & Chic"],
}


def _plain(listing):
    return {key: listing[key] for key in PLAIN}


def test_listing_text_is_decoded_once_where_it_is_read():
    import copy

    handler, seen = recorder({
        f"/shops/{SHOP}/listings": lambda r: {"count": 1, "results": [copy.deepcopy(ESCAPED)]},
        "/listings/batch": lambda r: {"count": 1, "results": [copy.deepcopy(ESCAPED)]},
        "/listings/1000001": lambda r: copy.deepcopy(ESCAPED),
        "/listings/active": lambda r: {"count": 1, "results": [copy.deepcopy(ESCAPED)]},
    })
    client = make_client(handler)
    assert _plain(next(client.listings_by_shop("active"))) == PLAIN
    assert _plain(client.listings_batch([1000001])[0]) == PLAIN
    assert _plain(client.listing(1000001)) == PLAIN
    assert _plain(next(client.search_active_listings(keywords="mug"))) == PLAIN
    # Decoded once: text that already reads "&amp;" after one decode stays that way.
    twice = client_mod.unescape_listing({"title": "R&amp;amp;B"})
    assert twice["title"] == "R&amp;B"
    assert client_mod.unescape_listing(None) is None


def test_writes_send_plain_text_and_their_answers_come_back_plain():
    import copy

    def answer(request):
        return copy.deepcopy(ESCAPED)

    handler, seen = recorder({
        f"/shops/{SHOP}/listings/1000001": answer,
        f"/shops/{SHOP}/listings": answer,
    })
    client = make_client(handler)
    updated = client.update_listing(1000001, {"title": PLAIN["title"], "tags": PLAIN["tags"]})
    created = client.create_draft_listing({"title": PLAIN["title"], "tags": PLAIN["tags"]})
    for request in seen:
        form = dict(urllib.parse.parse_qsl(request.content.decode()))
        assert form["title"] == PLAIN["title"]
        assert form["tags"] == "mother's day,mom & dad,plain tag"
        assert "&#39;" not in request.content.decode() and "%26amp%3B" not in request.content.decode()
    assert updated["title"] == created["title"] == PLAIN["title"]


def test_receipt_text_is_decoded_where_it_is_read():
    import copy

    receipt = {
        "receipt_id": 3000001, "name": "Example O&#39;Buyer", "city": "St. John&#39;s",
        "transactions": [{"title": "Mom&#39;s Mug &amp; Gift", "variations": [
            {"formatted_name": "Size", "formatted_value": "11&quot; x 14&quot;"}]}],
    }
    handler, _seen = recorder({
        f"/shops/{SHOP}/receipts": lambda r: {"count": 1, "results": [copy.deepcopy(receipt)]},
        f"/shops/{SHOP}/receipts/3000001": lambda r: copy.deepcopy(receipt),
        f"/shops/{SHOP}/transactions": lambda r: {"count": 1,
                                                  "results": copy.deepcopy(receipt["transactions"])},
        f"/shops/{SHOP}/shipping-profiles": {"count": 1, "results": [
            {"shipping_profile_id": 1, "title": "Mugs &amp; Cups"}]},
    })
    client = make_client(handler)
    for got in (next(client.receipts()), client.receipt(3000001),
                client.receipts_page(limit=10)["results"][0]):
        assert got["name"] == "Example O'Buyer" and got["city"] == "St. John's"
        tx = got["transactions"][0]
        assert tx["title"] == "Mom's Mug & Gift"
        assert tx["variations"][0]["formatted_value"] == "11\" x 14\""
    assert next(client.shop_transactions())["title"] == "Mom's Mug & Gift"
    assert client.receipts_page(limit=10)["count"] == 1
    assert client.shipping_profiles()[0]["title"] == "Mugs & Cups"


def _pages(total):
    """A handler serving `total` records by limit/offset, like Etsy, counting requests."""
    served = []

    def handler(request):
        q = query(request)
        offset, limit = int(q["offset"]), int(q["limit"])
        served.append(offset)
        rows = [{"n": i} for i in range(offset, min(offset + limit, total))]
        return httpx.Response(200, json={"count": total, "results": rows})

    return handler, served


def test_a_shops_own_collections_are_read_past_12000(monkeypatch):
    monkeypatch.setattr(client_mod.RateLimiter, "acquire", lambda self: None)
    handler, served = _pages(15_000)
    client = make_client(handler)
    assert len(list(client.receipts(max_items=20_000))) == 15_000
    served.clear()
    assert len(list(client.ledger_entries(946684800, 946684800 + 86400, max_items=20_000))) == 15_000
    assert max(served) == 14_900
    assert len(list(client.receipts(max_items=12_345))) == 12_345


def test_only_the_marketplace_search_stops_at_its_window(monkeypatch):
    monkeypatch.setattr(client_mod.RateLimiter, "acquire", lambda self: None)
    handler, served = _pages(15_000)
    client = make_client(handler)
    found = list(client.search_active_listings(keywords="mug", max_items=20_000))
    assert len(found) == client_mod.MAX_SEARCH_OFFSET and max(served) < client_mod.MAX_SEARCH_OFFSET


def test_a_401_is_sent_again_with_the_new_token_even_under_one_attempt(monkeypatch):
    def refresh(token, config):
        return auth.Token("1.new", "1.refresh2", time.time() + 3600, token.scopes)

    monkeypatch.setattr(client_mod.auth, "refresh", refresh)
    sent = []

    def handler(request):
        sent.append(request.headers["authorization"])
        if request.headers["authorization"] == "Bearer 1.old":
            return httpx.Response(401, json={"error": "invalid_token"})
        return httpx.Response(200, json={"count": 0, "results": []})

    old = auth.Token("1.old", "1.refresh", time.time() + 3600, ())
    client = make_client(handler, token=old)
    with client.attempts(1):
        assert client.shipping_profiles() == []
    assert sent == ["Bearer 1.old", "Bearer 1.new"]


def test_a_second_401_is_reconnect_not_offline(monkeypatch):
    monkeypatch.setattr(client_mod.auth, "refresh",
                        lambda token, config: auth.Token("1.new", "1.r", time.time() + 3600, ()))
    sent = []

    def handler(request):
        sent.append(request.headers["authorization"])
        return httpx.Response(401, json={"error": "invalid_token"})

    client = make_client(handler, token=auth.Token("1.old", "1.r", time.time() + 3600, ()))
    with client.attempts(1), pytest.raises(EtsyApiError) as error:
        client.shipping_profiles()
    assert error.value.status == 401 and len(sent) == 2


# --- uploadListingFile ------------------------------------------------------------------


def _parts(request: httpx.Request) -> dict[str, tuple[str | None, bytes, str | None]]:
    """The multipart form of a request: {field: (filename, body, content type)}."""
    import email.parser
    import email.policy

    head = f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode()
    message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        head + request.content)
    out = {}
    for part in message.iter_parts():
        out[part.get_param("name", header="content-disposition")] = (
            part.get_filename(), part.get_payload(decode=True), part.get("content-type"))
    return out


def test_upload_listing_file_posts_the_file_its_name_and_rank(tmp_path):
    handler, seen = recorder({f"/shops/{SHOP}/listings/1000001/files": {
        "listing_file_id": 9001, "listing_id": 1000001, "rank": 2, "filename": "planner.pdf",
        "filesize": "14 B", "size_bytes": 14, "filetype": "application/pdf"}})
    client = make_client(handler)
    path = tmp_path / "planner.pdf"
    path.write_bytes(b"%PDF-1.4 hello")
    answer = client.upload_listing_file(1000001, path, rank=2)
    assert answer["listing_file_id"] == 9001
    (request,) = seen
    assert request.method == "POST"
    assert request.url.path == f"{PREFIX}/shops/{SHOP}/listings/1000001/files"
    assert request.headers["authorization"] == "Bearer 1.access"
    assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
    parts = _parts(request)
    assert set(parts) == {"file", "name", "rank"}
    assert parts["file"] == ("planner.pdf", b"%PDF-1.4 hello", "application/pdf")
    assert parts["name"][1] == b"planner.pdf" and parts["rank"][1] == b"2"


def test_upload_listing_file_sends_any_sellable_type_as_octet_stream(tmp_path):
    handler, seen = recorder({f"/shops/{SHOP}/listings/1000001/files": {"listing_file_id": 1}})
    client = make_client(handler)
    path = tmp_path / "Cut File.dxf"
    path.write_bytes(b"0\nSECTION")
    client.upload_listing_file(1000001, path, name="cut-file.dxf")
    parts = _parts(seen[0])
    assert parts["file"][0] == "cut-file.dxf" and parts["name"][1] == b"cut-file.dxf"
    assert parts["file"][2] == "image/vnd.dxf" and parts["rank"][1] == b"1"
    other = tmp_path / "brushes.abr"
    other.write_bytes(b"8BIM")
    client.upload_listing_file(1000001, other)
    assert _parts(seen[1])["file"][2] == "application/octet-stream"


@pytest.mark.parametrize("name, data, code", [
    ("installer.exe", b"MZ", "file_type"),
    ("run.bat", b"echo", "file_type"),
    ("README", b"no extension", "file_type"),
    ("empty.pdf", b"", "file_empty"),
])
def test_a_file_etsy_would_refuse_is_never_read_or_sent(tmp_path, name, data, code):
    handler, seen = recorder({})
    client = make_client(handler)
    path = tmp_path / name
    path.write_bytes(data)
    assert client_mod.file_issue(path)[0] == code
    with pytest.raises(ValidationError):
        client.upload_listing_file(1000001, path)
    assert seen == []


def test_the_digital_file_limits(tmp_path, monkeypatch):
    # Not in the OAS (it gives uploadListingFile no limits); Etsy's seller help: five
    # files per listing, 20 MB each.
    assert client_mod.MAX_LISTING_FILES == 5
    assert client_mod.MAX_FILE_BYTES == 20 * 1024 * 1024
    monkeypatch.setattr(client_mod, "MAX_FILE_BYTES", 10)
    big = tmp_path / "big.zip"
    big.write_bytes(b"PK" + b"0" * 20)
    code, message, params = client_mod.file_issue(big)
    assert code == "file_too_large" and params["name"] == "big.zip" and params["max_mb"] == 0
    assert client_mod.file_issue(tmp_path / "gone.pdf")[0] == "file_missing"
    ok = tmp_path / "ok.pdf"
    ok.write_bytes(b"%PDF")
    assert client_mod.file_issue(ok) is None and client_mod.file_problem(ok) is None


def test_a_file_upload_is_a_write_and_is_not_repeated_after_a_5xx(tmp_path, monkeypatch):
    monkeypatch.setattr(client_mod.time, "sleep", lambda _s: None)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500, json={"error": "boom"})

    client = make_client(handler)
    path = tmp_path / "planner.pdf"
    path.write_bytes(b"%PDF")
    with pytest.raises(EtsyApiError):
        client.upload_listing_file(1000001, path)
    assert len(calls) == 1
