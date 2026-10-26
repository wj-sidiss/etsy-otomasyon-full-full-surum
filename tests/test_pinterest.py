"""Pinterest: every test runs offline against a mock transport.

What is pinned here is what would cost a seller: a Pin sent twice, a Pin that points
at a draft, copy over Pinterest's limits, or a queue that bursts instead of spreading.
"""

import base64
import json
from datetime import date

import httpx
import pytest

from etsyotomasyon import pinterest as pin
from etsyotomasyon.errors import AuthError, ValidationError

LISTING = {
    "listing_id": 4001,
    "state": "active",
    "url": "https://www.etsy.com/listing/4001/sage-fern-wallpaper?ref=shop_home",
    "title": "Sage Fern Wallpaper | Botanical Leaf Mural | Minimal Bedroom Decor | Peel and Stick "
             "| Removable | Self Adhesive",
    "tags": ["fern wallpaper", "sage green decor", "botanical mural"],
}
IMAGES = [
    {"rank": 2, "url_fullxfull": "https://i.etsystatic.com/2.jpg"},
    {"rank": 1, "url_fullxfull": "https://i.etsystatic.com/1.jpg"},
    {"rank": 7, "url_fullxfull": "https://i.etsystatic.com/7.jpg"},
]


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("ETSYOTOMASYON_HOME", str(tmp_path / "home"))
    for var in ("PINTEREST_APP_ID", "PINTEREST_APP_SECRET", "PINTEREST_ACCESS_TOKEN", "PINTEREST_SANDBOX"):
        monkeypatch.delenv(var, raising=False)


def _config(**kw):
    return pin.PinterestConfig(app_id="app", app_secret="secret", **kw)


def _client(handler, token=None, **kw):
    token = token or pin.PinToken(access_token="acc", refresh_token="ref", expires_at=9e12)
    client = pin.PinterestClient(_config(**kw), token=token, http=httpx.Client(
        transport=httpx.MockTransport(handler)), per_second=1000)
    return client


# --- copy within Pinterest's limits ----------------------------------------------


def test_the_title_keeps_whole_segments_from_the_left_within_100():
    title = pin.pin_title(LISTING["title"])
    assert len(title) <= pin.MAX_TITLE_LEN
    assert title.startswith("Sage Fern Wallpaper | Botanical Leaf Mural")
    assert not title.endswith("|")


def test_a_single_overlong_segment_is_cut_on_a_word():
    title = pin.pin_title("word " * 40)
    assert len(title) <= pin.MAX_TITLE_LEN and not title.endswith(" ")


def test_the_description_uses_title_phrases_and_tags_within_800():
    body = pin.pin_description(LISTING["title"], ["x" * 30] * 60)
    assert len(body) <= pin.MAX_DESCRIPTION_LEN
    assert body.startswith("Sage Fern Wallpaper. Botanical Leaf Mural")


def test_an_explicit_description_wins():
    assert pin.pin_description(LISTING["title"], ["a"], "  Our  own words ") == "Our own words"


# --- listing to payload ------------------------------------------------------------


def test_one_pin_per_chosen_image_in_rank_order_linking_to_the_listing():
    pins = pin.pins_for_listing(LISTING, IMAGES, "b1", ranks={1, 2})
    assert [p["rank"] for p in pins] == [1, 2]
    payload = pins[0]["payload"]
    assert payload["board_id"] == "b1"
    assert payload["link"] == "https://www.etsy.com/listing/4001/sage-fern-wallpaper"
    assert payload["media_source"] == {"source_type": "image_url", "url": "https://i.etsystatic.com/1.jpg"}
    assert "ai_disclosures" not in payload


def test_ai_created_imagery_is_declared_when_asked():
    payload = pin.pins_for_listing(LISTING, IMAGES, "b1", ai_modified=True)[0]["payload"]
    assert payload["ai_disclosures"] == {"values": ["AI_MODIFIED"]}


def test_a_draft_is_never_pinned():
    # A Pin to an unpublished listing leads to a page buyers cannot open.
    with pytest.raises(ValidationError, match="Publish it first"):
        pin.pins_for_listing(dict(LISTING, state="draft"), IMAGES, "b1")


@pytest.mark.parametrize(("spec", "expected"), [("1-3", {1, 2, 3}), ("1,5", {1, 5}), ("2-3,7", {2, 3, 7})])
def test_image_ranks_parse(spec, expected):
    assert pin.parse_ranks(spec) == expected


@pytest.mark.parametrize("spec", ["0-2", "3-1", "a", "1-b"])
def test_bad_image_ranks_are_refused(spec):
    with pytest.raises(ValidationError):
        pin.parse_ranks(spec)


def test_a_board_is_found_by_id_or_exact_name():
    boards = [{"id": "11", "name": "Bedroom Wallpaper"}, {"id": "22", "name": "Kitchen"}]
    assert pin.resolve_board(boards, "22")["name"] == "Kitchen"
    assert pin.resolve_board(boards, "bedroom wallpaper")["id"] == "11"
    with pytest.raises(ValidationError, match="Your boards"):
        pin.resolve_board(boards, "Nursery")


# --- the queue spreads Pins and never doubles them ---------------------------------


def _pins(listing_id, ranks):
    listing = dict(LISTING, listing_id=listing_id)
    images = [{"rank": r, "url_fullxfull": f"https://i/{listing_id}/{r}.jpg"} for r in ranks]
    return pin.pins_for_listing(listing, images, "b1")


def test_the_daily_limit_counts_the_whole_queue(tmp_path):
    # Queueing two listings in one sitting must still come out at per_day a day.
    queue = pin.Queue(tmp_path / "q.json")
    start = date(2026, 10, 1)
    queue.add(_pins(1, [1, 2, 3]), start=start, per_day=2)
    queue.add(_pins(2, [1, 2, 3]), start=start, per_day=2)
    per_day = {}
    for entry in queue.entries:
        per_day[entry["due"]] = per_day.get(entry["due"], 0) + 1
    assert per_day == {"2026-10-01": 2, "2026-10-02": 2, "2026-10-03": 2}


def test_the_same_image_on_the_same_board_is_queued_once(tmp_path):
    queue = pin.Queue(tmp_path / "q.json")
    first = queue.add(_pins(1, [1, 2]), start=date(2026, 10, 1), per_day=5)
    again = queue.add(_pins(1, [1, 2]), start=date(2026, 10, 1), per_day=5)
    assert len(first) == 2 and again == []


def test_the_queue_round_trips_through_disk(tmp_path):
    queue = pin.Queue(tmp_path / "q.json")
    queue.add(_pins(1, [1]), start=date(2026, 10, 1), per_day=1)
    queue.save()
    assert pin.Queue.load(tmp_path / "q.json").entries == queue.entries


# --- posting: a sent-but-unconfirmed Pin is never sent twice -----------------------


class _Recorder:
    def __init__(self, outcomes):
        self.outcomes, self.sent = list(outcomes), []

    def create_pin(self, payload):
        self.sent.append(payload)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_due_pins_are_posted_and_recorded(tmp_path):
    queue = pin.Queue(tmp_path / "q.json")
    queue.add(_pins(1, [1, 2]), start=date(2026, 10, 1), per_day=1)
    client = _Recorder([{"id": "p1"}])
    done = pin.post_due(client, queue, today=date(2026, 10, 1))
    assert [e["status"] for e in done] == ["posted"]
    assert done[0]["pin_id"] == "p1"
    assert pin.Queue.load(tmp_path / "q.json").counts() == {"posted": 1, "pending": 1}


def test_a_definite_refusal_fails_and_an_ambiguous_error_is_parked(tmp_path):
    queue = pin.Queue(tmp_path / "q.json")
    queue.add(_pins(1, [1, 2]), start=date(2026, 10, 1), per_day=5)
    client = _Recorder([
        pin.PinterestApiError(400, "bad image"),
        pin.PinterestApiError(0, "network error: read timeout"),
    ])
    done = pin.post_due(client, queue, today=date(2026, 10, 1))
    assert [e["status"] for e in done] == ["failed", "uncertain"]
    # Tomorrow's run must not send the uncertain one again.
    again = pin.post_due(_Recorder([]), queue, today=date(2026, 10, 2))
    assert again == []


def test_a_pin_is_marked_sending_on_disk_before_the_request(tmp_path):
    queue = pin.Queue(tmp_path / "q.json")
    queue.add(_pins(1, [1]), start=date(2026, 10, 1), per_day=1)

    class Crash:
        def create_pin(self, payload):
            on_disk = json.loads((tmp_path / "q.json").read_text(encoding="utf-8"))
            assert on_disk[0]["status"] == "sending"
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        pin.post_due(Crash(), queue, today=date(2026, 10, 1))
    # After a crash the entry is left 'sending', which is never picked up as due.
    assert pin.Queue.load(tmp_path / "q.json").due(date(2026, 10, 5)) == []


def test_a_dry_run_sends_nothing(tmp_path):
    queue = pin.Queue(tmp_path / "q.json")
    queue.add(_pins(1, [1]), start=date(2026, 10, 1), per_day=1)
    done = pin.post_due(None, queue, today=date(2026, 10, 1), dry_run=True)
    assert len(done) == 1 and done[0]["status"] == "pending"


# --- the HTTP client ---------------------------------------------------------------


def test_create_pin_sends_json_with_a_bearer_token():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["Authorization"]
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "pin-9"})

    client = _client(handler)
    assert client.create_pin({"board_id": "b1", "title": "t"}) == {"id": "pin-9"}
    assert seen["auth"] == "Bearer acc"
    assert seen["url"] == "https://api.pinterest.com/v5/pins"
    assert seen["body"]["board_id"] == "b1"


def test_the_sandbox_switch_changes_the_host():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(201, json={"id": "x"})

    _client(handler, sandbox=True).create_pin({})
    assert seen["url"].startswith("https://api-sandbox.pinterest.com/v5/")


def test_a_post_that_hits_a_server_error_is_not_repeated():
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(500, json={"message": "oops"})

    with pytest.raises(pin.PinterestApiError) as caught:
        _client(handler).create_pin({})
    assert calls == ["POST"] and caught.value.status == 500


def test_a_429_is_retried_after_the_given_delay(monkeypatch):
    monkeypatch.setattr(pin.time, "sleep", lambda s: None)
    responses = [httpx.Response(429, headers={"Retry-After": "1"}, json={}),
                 httpx.Response(201, json={"id": "ok"})]
    assert _client(lambda r: responses.pop(0)).create_pin({}) == {"id": "ok"}


def test_boards_follow_the_bookmark():
    pages = {
        None: {"items": [{"id": "1", "name": "A"}], "bookmark": "next"},
        "next": {"items": [{"id": "2", "name": "B"}], "bookmark": None},
    }

    def handler(request):
        return httpx.Response(200, json=pages[request.url.params.get("bookmark")])

    assert [b["id"] for b in _client(handler).boards()] == ["1", "2"]


def test_an_unexpected_401_refreshes_once_then_retries(monkeypatch):
    refreshed = []

    def fake_refresh(config, token, http=None):
        refreshed.append(token.refresh_token)
        return pin.PinToken(access_token="new", refresh_token="ref2", expires_at=9e12)

    monkeypatch.setattr(pin, "refresh", fake_refresh)
    auths = []

    def handler(request):
        auths.append(request.headers["Authorization"])
        return httpx.Response(401, json={}) if len(auths) == 1 else httpx.Response(201, json={"id": "p"})

    assert _client(handler).create_pin({}) == {"id": "p"}
    assert auths == ["Bearer acc", "Bearer new"] and refreshed == ["ref"]


# --- tokens -----------------------------------------------------------------------


def test_the_code_exchange_uses_basic_auth_and_asks_for_continuous_refresh():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["Authorization"]
        seen["form"] = dict(httpx.QueryParams(request.content.decode()))
        return httpx.Response(200, json={
            "access_token": "a", "refresh_token": "r", "expires_in": 2592000,
            "refresh_token_expires_in": 31536000, "scope": "pins:write",
        })

    token = pin.exchange_code(_config(), "the-code", http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert seen["auth"] == "Basic " + base64.b64encode(b"app:secret").decode()
    assert seen["form"]["grant_type"] == "authorization_code"
    assert seen["form"]["continuous_refresh"] == "true"
    assert token.refresh_token == "r"
    assert pin.load_token().access_token == "a"


def test_a_refresh_that_omits_the_refresh_token_keeps_the_old_one():
    def handler(request):
        return httpx.Response(200, json={"access_token": "a2", "expires_in": 60})

    old = pin.PinToken(access_token="a1", refresh_token="keep", expires_at=0)
    fresh = pin.refresh(_config(), old, http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert fresh.access_token == "a2" and fresh.refresh_token == "keep"


def test_a_pasted_redirect_must_carry_our_state():
    assert pin.code_from_redirect("http://localhost:8085/?code=abc&state=s1", "s1") == "abc"
    with pytest.raises(AuthError, match="state"):
        pin.code_from_redirect("http://localhost:8085/?code=abc&state=evil", "s1")


def test_without_an_app_configured_the_error_says_what_to_do():
    with pytest.raises(Exception, match="PINTEREST_APP_ID"):
        pin.PinterestConfig().require_app()


# --- the CLI --------------------------------------------------------------------


def test_the_cli_reports_an_unconnected_account_plainly(tmp_path):
    from typer.testing import CliRunner

    from etsyotomasyon.cli import app

    result = CliRunner().invoke(app, ["pinterest", "status"])
    assert result.exit_code == 0
    assert "not connected" in result.output
    assert "queue" in result.output


def test_retry_puts_only_unsettled_pins_back(tmp_path):
    from typer.testing import CliRunner

    from etsyotomasyon.cli import app

    queue = pin.Queue.load()
    queue.add(_pins(4001, [1, 2]), start=date(2026, 10, 1), per_day=5)
    queue.entries[0]["status"] = "uncertain"
    queue.entries[1]["status"] = "posted"
    queue.save()

    result = CliRunner().invoke(app, ["pinterest", "retry", "4001"])
    assert result.exit_code == 0, result.output
    after = pin.Queue.load().entries
    assert [e["status"] for e in after] == ["pending", "posted"]
    assert after[0]["due"] == date.today().isoformat()


# --- the redirect listener ------------------------------------------------------


def _serve_callback(return_url):
    import socket
    import threading

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    pin._Callback.result = {}
    pin._Callback.return_url = return_url
    server = pin.LoopbackServer(("127.0.0.1", port), pin._Callback)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return server, f"http://127.0.0.1:{port}"


def test_the_listener_sends_the_browser_back_to_the_app_when_asked():
    server, base = _serve_callback("http://localhost:3000/oauth-done")
    try:
        with httpx.Client(trust_env=False) as http:
            resp = http.get(f"{base}/?code=c1&state=s1")
            # The favicon request that follows must not wipe out the answer.
            assert http.get(f"{base}/favicon.ico").status_code == 404
    finally:
        server.shutdown()
        server.server_close()
        pin._Callback.return_url = None
    assert resp.status_code == 302
    assert resp.headers["location"] == "http://localhost:3000/oauth-done"
    assert pin._Callback.result == {"code": "c1", "state": "s1"}


def test_without_a_return_address_the_listener_shows_its_own_page():
    server, base = _serve_callback(None)
    try:
        with httpx.Client(trust_env=False) as http:
            ok = http.get(f"{base}/?code=c1&state=s1")
            refused = http.get(f"{base}/?error=access_denied&state=s1")
    finally:
        server.shutdown()
        server.server_close()
    assert ok.status_code == 200 and "connected" in ok.text
    assert refused.status_code == 200 and "access_denied" in refused.text
    assert pin._Callback.result == {"error": "access_denied", "state": "s1"}


def test_the_listener_ignores_an_answer_to_another_request():
    """Review oauth-listener-forged-first-request: a forged request is refused, the real
    answer still arrives, and nothing is sent on to the app for the forged one."""
    server, base = _serve_callback("http://localhost:3000/oauth-done")
    pin._Callback.expected_state = "s1"
    try:
        with httpx.Client(trust_env=False) as http:
            forged = http.get(f"{base}/?code=FORGED&state=nope")
            no_state = http.get(f"{base}/?code=FORGED")
            refusal = http.get(f"{base}/?error=access_denied&state=nope")
            assert pin._Callback.result == {}
            real = http.get(f"{base}/?code=c1&state=s1")
    finally:
        server.shutdown()
        server.server_close()
        pin._Callback.return_url = None
        pin._Callback.expected_state = None
    for resp in (forged, no_state, refusal):
        assert resp.status_code == 400 and "location" not in resp.headers
        assert "FORGED" not in resp.text and "access_denied" not in resp.text
    assert real.status_code == 302 and real.headers["location"] == "http://localhost:3000/oauth-done"
    assert pin._Callback.result == {"code": "c1", "state": "s1"}


def test_listening_for_the_code_waits_past_a_forged_answer():
    import socket
    import threading
    import time

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = pin.PinterestConfig(app_id="a", app_secret="s", redirect_uri=f"http://localhost:{port}/")
    answers = []

    def browser():
        with httpx.Client(trust_env=False) as http:
            for _ in range(100):
                try:
                    answers.append(http.get(f"http://127.0.0.1:{port}/?code=FORGED&state=nope"))
                    break
                except httpx.ConnectError:
                    time.sleep(0.05)
            time.sleep(0.6)
            answers.append(http.get(f"http://127.0.0.1:{port}/?code=real&state=mine"))

    threading.Thread(target=browser, daemon=True).start()
    assert pin._listen_for_code(config, "mine", timeout=15) == "real"
    assert answers[0].status_code == 400 and answers[1].status_code == 200
    assert pin._Callback.expected_state is None


# --- Pins queued before Etsy's text was decoded ------------------------------------------

OLD_ENTRY = {
    "key": "1000001:1:b1",
    "listing_id": 1000001,
    "rank": 1,
    "due": "2026-10-01",
    "status": "pending",
    "pin_id": None,
    "message": "",
    "payload": {
        "board_id": "b1",
        "title": "Mom&#39;s &quot;Best&quot; Mug &amp; Gift",
        "description": "Mom&#39;s &quot;Best&quot; Mug &amp; Gift. mother&#39;s day",
        "link": "https://www.etsy.com/listing/1000001/moms-best-mug",
        "alt_text": "Mom&#39;s &quot;Best&quot; Mug &amp; Gift",
        "media_source": {"source_type": "image_url", "url": "https://i/1000001/1.jpg?a=1&amp;b=2"},
    },
}
PLAIN_TITLE = "Mom's \"Best\" Mug & Gift"


def test_an_old_queue_entry_is_posted_as_plain_text(tmp_path):
    # Queued by 0.2.0 with Etsy's escaped title: the Pin must not say "&#39;".
    path = tmp_path / "q.json"
    path.write_text(json.dumps([OLD_ENTRY]), encoding="utf-8")
    queue = pin.Queue.load(path)
    client = _Recorder([{"id": "p1"}])
    done = pin.post_due(client, queue, today=date(2026, 10, 1))
    assert [e["status"] for e in done] == ["posted"]
    sent = client.sent[0]
    assert sent["title"] == sent["alt_text"] == PLAIN_TITLE
    assert sent["description"] == f"{PLAIN_TITLE}. mother's day"
    # Only the text is decoded: the link and the image address go out untouched.
    assert sent["link"] == OLD_ENTRY["payload"]["link"]
    assert sent["media_source"] == OLD_ENTRY["payload"]["media_source"]
    saved = json.loads(path.read_text(encoding="utf-8"))[0]
    assert saved["payload"]["title"] == PLAIN_TITLE and saved["plain_text"] is True


def test_an_old_entry_is_decoded_at_post_time_even_when_built_by_hand(tmp_path):
    queue = pin.Queue(tmp_path / "q.json", [json.loads(json.dumps(OLD_ENTRY))])
    client = _Recorder([{"id": "p1"}])
    pin.post_due(client, queue, today=date(2026, 10, 1))
    assert client.sent[0]["title"] == PLAIN_TITLE


def test_reading_the_queue_shows_old_titles_plain_and_saves_them_once(tmp_path):
    path = tmp_path / "q.json"
    path.write_text(json.dumps([OLD_ENTRY]), encoding="utf-8")
    queue = pin.Queue.load(path)
    assert queue.entries[0]["payload"]["title"] == PLAIN_TITLE
    assert "&#39;" in path.read_text(encoding="utf-8")  # reading alone writes nothing
    queue.save()
    again = pin.Queue.load(path).entries[0]
    assert again["payload"]["title"] == PLAIN_TITLE and again["plain_text"] is True


def test_a_new_pin_is_not_decoded_a_second_time(tmp_path):
    # The listing title is already plain (the client decoded it). A seller's literal
    # "&amp;" in it must reach Pinterest as typed.
    listing = dict(LISTING, title="R&amp;B Vinyl Wall Art")
    pins = pin.pins_for_listing(listing, IMAGES, "b1", ranks={1})
    queue = pin.Queue(tmp_path / "q.json")
    added = queue.add(pins, start=date(2026, 10, 1), per_day=1)
    assert added[0]["plain_text"] is True
    queue.save()
    client = _Recorder([{"id": "p1"}])
    pin.post_due(client, pin.Queue.load(tmp_path / "q.json"), today=date(2026, 10, 1))
    assert client.sent[0]["title"] == "R&amp;B Vinyl Wall Art"
