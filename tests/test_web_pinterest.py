"""Pinterest page API: app settings, the consent flow, boards, queueing and posting.

Pinterest itself is a MockTransport; nothing here reaches the network.
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.parse
from datetime import date, timedelta

import httpx
import pytest
from web_helpers import ETSY_SHOP_ID, use_fake_etsy, wait_for_job

from etsyotomasyon import pinterest
from etsyotomasyon.desktop import settings
from etsyotomasyon.web.api import pinterest as api

LISTING_URL = "https://www.etsy.com/listing/1000001/retro-mountain-sunset-shirt"


def _images(listing_id, ranks):
    return [{"listing_image_id": listing_id * 10 + r, "rank": r,
             "url_170x135": f"https://i.etsystatic.com/x/il_170x135.{listing_id}{r}.jpg",
             "url_fullxfull": f"https://i.etsystatic.com/x/il_fullxfull.{listing_id}{r}.jpg"}
            for r in ranks]


LISTINGS = {
    1000001: {"listing_id": 1000001, "state": "active", "url": LISTING_URL + "?ref=shop",
              "title": "Retro Mountain Sunset Shirt | Vintage Hiking T-Shirt",
              "tags": ["hiking shirt"], "images": _images(1000001, [1, 2, 3])},
    1000002: {"listing_id": 1000002, "state": "active",
              "url": "https://www.etsy.com/listing/1000002/but-first-coffee-mug",
              "title": "But First Coffee Mug", "tags": [], "images": _images(1000002, [1, 2])},
    1000003: {"listing_id": 1000003, "state": "draft", "url": "",
              "title": "Wildflower Botanical Print", "tags": [], "images": _images(1000003, [1])},
}


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def keys(web):
    """A Pinterest app saved for the open shop, with a free callback port."""
    port = _free_port()
    resp = web.client.post("/api/pinterest/keys", json={
        "app_id": "1500001", "app_secret": "pinsecret123", "redirect_uri": f"http://localhost:{port}/",
    })
    assert resp.status_code == 200, resp.text
    return port


class FakePinterest:
    def __init__(self):
        self.pins: list[dict] = []
        self.create_status = 201
        self.boards_status = 200
        self.boards = [
            {"id": "900001", "name": "Shirts", "privacy": "PUBLIC"},
            {"id": "900002", "name": "Mugs & Cups", "privacy": "PUBLIC",
             "media": {"image_cover_url": "https://i.pinimg.com/cover.jpg"}},
        ]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer pinaccess"
        if request.method == "GET" and request.url.path == "/v5/boards":
            if self.boards_status != 200:
                return httpx.Response(self.boards_status, json={"message": "refused"})
            return httpx.Response(200, json={"items": self.boards, "bookmark": None})
        if request.method == "POST" and request.url.path == "/v5/pins":
            body = json.loads(request.content)
            self.pins.append(body)
            if self.create_status >= 300:
                return httpx.Response(self.create_status, json={"message": "refused"})
            return httpx.Response(201, json={"id": f"pin{len(self.pins)}"})
        return httpx.Response(404, json={"message": "no such path"})


@pytest.fixture
def pin_api(monkeypatch, keys):
    """Pinterest connected (a saved token) and answered by a FakePinterest."""
    fake = FakePinterest()
    pinterest.save_token(pinterest.PinToken(access_token="pinaccess", refresh_token="pinrefresh",
                                            expires_at=time.time() + 3600))
    monkeypatch.setattr(api, "_http", lambda: httpx.Client(transport=httpx.MockTransport(fake)))
    monkeypatch.setattr(api, "PIN_RATE", 1000.0)
    return fake


def _etsy(web):
    fake = use_fake_etsy(web)

    def batch(request):
        ids = [int(i) for i in request.url.params["listing_ids"].split(",")]
        assert request.url.params.get("includes") == "Images"
        return {"count": len(ids), "results": [LISTINGS[i] for i in ids if i in LISTINGS]}

    def by_shop(request):
        assert request.url.params.get("state") == "active"
        rows = [v for v in LISTINGS.values() if v["state"] == "active"]
        return {"count": len(rows), "results": rows}

    fake.add("GET", "/listings/batch", batch)
    fake.add("GET", f"/shops/{ETSY_SHOP_ID}/listings", by_shop)
    return fake


def _queue_some(web, ids=(1000001,), per_day=2, **extra):
    body = {"listing_ids": list(ids), "board_id": "900001", "board_name": "Shirts",
            "per_day": per_day, "images": "", "ai_modified": True, "dry_run": False, **extra}
    resp = web.client.post("/api/pinterest/queue", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- settings -------------------------------------------------------------------------


def test_status_before_anything_is_set_up(web):
    data = web.client.get("/api/pinterest/status").json()
    assert data["configured"] is False and data["connected"] is False
    assert data["app"]["redirect_uri"] == "http://localhost:8085/"
    assert data["app"]["has_secret"] is False
    assert data["queue"] == {"total": 0, "pending": 0, "due": 0, "posted": 0, "failed": 0,
                             "uncertain": 0}
    assert data["defaults"] == {"per_day": 2, "images": "", "ai_modified": True}
    assert data["schedule"]["command"].endswith("pinterest post")
    assert data["connecting"] is None


def test_saving_the_app_never_sends_the_secret_back(web, keys):
    resp = web.client.post("/api/pinterest/keys", json={"app_id": "1500001", "app_secret": ""})
    assert resp.status_code == 200
    assert "pinsecret123" not in resp.text
    data = resp.json()
    assert data["configured"] is True
    assert data["app"]["app_id"] == "1500001"
    assert data["app"]["has_secret"] is True and data["app"]["secret_length"] == 12
    # An empty secret kept the saved one; the callback went back to the default.
    assert settings.current("PINTEREST_APP_SECRET") == "pinsecret123"
    assert data["app"]["redirect_uri"] == "http://localhost:8085/"
    env = settings.env_path().read_text(encoding="utf-8")
    assert "PINTEREST_APP_ID=1500001" in env


def test_a_new_app_forgets_the_old_sign_in(web, keys):
    pinterest.save_token(pinterest.PinToken(access_token="pinaccess"))
    resp = web.client.post("/api/pinterest/keys", json={"app_id": "1500002"})
    assert resp.json()["token_cleared"] is True
    assert pinterest.load_token() is None


@pytest.mark.parametrize("body, field", [
    ({"app_id": ""}, "app_id"),
    ({"app_id": "15 00"}, "app_id"),
    ({"app_id": "1500001"}, "app_secret"),
    ({"app_id": "1500001", "app_secret": "s", "redirect_uri": "not a url"}, "redirect_uri"),
    ({"app_id": "1500001", "app_secret": "s", "sandbox": "yes"}, "sandbox"),
])
def test_bad_app_settings_are_refused(web, body, field):
    resp = web.client.post("/api/pinterest/keys", json=body)
    assert resp.status_code == 422
    assert resp.json()["error"]["params"]["field"] == field


def test_the_callback_cannot_be_etsyotomasyons_own_port(web):
    resp = web.client.post("/api/pinterest/keys", json={
        "app_id": "1500001", "app_secret": "s", "redirect_uri": f"http://localhost:{web.port}/",
    })
    assert resp.status_code == 422 and resp.json()["error"]["params"]["field"] == "redirect_uri"


# --- the consent flow ---------------------------------------------------------------------


def test_connect_needs_the_app_first(web):
    resp = web.client.post("/api/pinterest/connect")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "pinterest_keys"


def test_connect_needs_a_callback_on_this_computer(web):
    web.client.post("/api/pinterest/keys", json={
        "app_id": "1500001", "app_secret": "s", "redirect_uri": "https://example.com/pin",
    })
    resp = web.client.post("/api/pinterest/connect")
    assert resp.json()["error"]["code"] == "pinterest_callback_not_local"


def _start_connect(web):
    resp = web.client.post("/api/pinterest/connect")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    query = urllib.parse.parse_qs(urllib.parse.urlparse(data["url"]).query)
    return data, query


def _bind_like_a_listener(port):
    """Fail if something still listens on `port`.

    On Linux and macOS an answered connection leaves the port in TIME_WAIT, so bind the
    way a listener does (SO_REUSEADDR still refuses a port that is listening). On Windows
    that option would allow binding over a live listener, so it is left off there.
    """
    with socket.socket() as probe:
        if os.name != "nt":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))


def test_the_whole_consent_flow(web, keys, monkeypatch):
    seen = {}

    def exchange(config, code, http=None):
        seen["code"] = code
        token = pinterest.PinToken(access_token="pinaccess", refresh_token="r",
                                   expires_at=time.time() + 3600, scope="boards:read,pins:write")
        pinterest.save_token(token)
        return token

    monkeypatch.setattr(pinterest, "exchange_code", exchange)
    data, query = _start_connect(web)
    assert data["url"].startswith("https://www.pinterest.com/oauth/?")
    assert query["client_id"] == ["1500001"]
    assert query["redirect_uri"] == [f"http://localhost:{keys}/"]
    status = web.client.get("/api/pinterest/status").json()
    assert status["connecting"]["url"] == data["url"]

    # The browser comes back from Pinterest to the listener, which is already open.
    with httpx.Client(trust_env=False) as browser:
        back = browser.get(f"http://127.0.0.1:{keys}/",
                           params={"code": "pincode", "state": query["state"][0]})
    assert back.status_code == 302
    assert back.headers["location"] == f"http://localhost:{web.port}/oauth-done?service=pinterest"
    job = wait_for_job(web, data["job"]["id"])
    assert job["status"] == "done", job
    assert seen["code"] == "pincode"
    assert pinterest._Callback.return_url is None
    status = web.client.get("/api/pinterest/status").json()
    assert status["connected"] is True and status["token"]["source"] == "file"
    notes = web.client.get("/api/notifications").json()["items"]
    assert notes[0]["ns"] == "pinterest" and notes[0]["key"] == "notify.connected"
    # The listener has let go of the port.
    _bind_like_a_listener(keys)


def test_refusing_on_pinterest_ends_the_job(web, keys):
    data, query = _start_connect(web)
    with httpx.Client(trust_env=False) as browser:
        browser.get(f"http://127.0.0.1:{keys}/", params={"error": "access_denied",
                                                        "state": query["state"][0]})
    job = wait_for_job(web, data["job"]["id"])
    assert job["status"] == "error" and job["error"]["code"] == "pinterest_denied"


def test_an_answer_to_another_request_is_refused_and_the_wait_goes_on(web, keys):
    data, _query = _start_connect(web)
    with httpx.Client(trust_env=False) as browser:
        forged = browser.get(f"http://127.0.0.1:{keys}/", params={"code": "c", "state": "forged"})
    assert forged.status_code == 400
    job = web.client.get(f"/api/jobs/{data['job']['id']}").json()
    assert job["status"] in ("queued", "running")  # still waiting for the real answer
    web.client.post(f"/api/jobs/{data['job']['id']}/cancel")
    assert wait_for_job(web, data["job"]["id"])["status"] == "cancelled"


def test_cancel_closes_the_listener(web, keys):
    data, _query = _start_connect(web)
    assert web.client.post("/api/pinterest/connect").json()["error"]["code"] == "busy"
    web.client.post(f"/api/jobs/{data['job']['id']}/cancel")
    job = wait_for_job(web, data["job"]["id"])
    assert job["status"] == "cancelled"
    _bind_like_a_listener(keys)
    # A new attempt can start again.
    again = web.client.post("/api/pinterest/connect").json()
    web.client.post(f"/api/jobs/{again['job']['id']}/cancel")
    wait_for_job(web, again["job"]["id"])


def test_a_busy_callback_port_is_reported_at_once(web, keys):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", keys))
        taken.listen(1)
        resp = web.client.post("/api/pinterest/connect")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "pinterest_port_busy"
    assert resp.json()["error"]["params"]["port"] == keys


def test_disconnect_forgets_the_token(web, pin_api):
    assert web.client.get("/api/pinterest/status").json()["connected"] is True
    data = web.client.post("/api/pinterest/disconnect").json()
    assert data["connected"] is False
    assert pinterest.load_token() is None


# --- boards -------------------------------------------------------------------------------


def test_boards_need_a_connected_account(web, keys):
    resp = web.client.get("/api/pinterest/boards")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "pinterest_not_connected"


def test_boards_and_the_default_board(web, pin_api):
    web.ctx.update_shop_prefs(pin_board="mugs & cups")  # what the old window stored: a name
    data = web.client.get("/api/pinterest/boards").json()
    assert [b["name"] for b in data["items"]] == ["Shirts", "Mugs & Cups"]
    assert data["items"][1]["cover"] == "https://i.pinimg.com/cover.jpg"
    assert data["default"] == "900002"
    assert web.ctx.shop_prefs()["pin_board"] == "900002"

    web.client.post("/api/pinterest/board", json={"board_id": "900001", "name": "Shirts"})
    status = web.client.get("/api/pinterest/status").json()
    assert status["board"] == {"id": "900001", "name": "Shirts"}


@pytest.mark.parametrize("status, code", [
    (401, "pinterest_auth"), (403, "pinterest_forbidden"), (429, "pinterest_rate_limited"),
    (400, "pinterest_error"),
])
def test_pinterest_errors_get_their_own_codes(web, pin_api, monkeypatch, status, code):
    monkeypatch.setattr(pinterest.time, "sleep", lambda s: None)  # the 429 retries
    pinterest.save_token(pinterest.PinToken(access_token="pinaccess"))  # nothing to refresh with
    pin_api.boards_status = status
    resp = web.client.get("/api/pinterest/boards", params={"refresh": 1})
    assert resp.json()["error"]["code"] == code
    assert resp.status_code == status if status != 400 else resp.status_code == 502


# --- the listing picker and the queue -------------------------------------------------------


def test_the_picker_lists_active_listings_with_a_thumbnail(web, keys):
    _etsy(web)
    items = web.client.get("/api/pinterest/listings").json()["items"]
    assert [i["listing_id"] for i in items] == [1000001, 1000002]
    assert items[0]["thumb"].endswith("il_170x135.10000011.jpg")
    assert items[0]["url"] == LISTING_URL
    assert items[0]["images"] == 3 and items[0]["queued"] == 0


def test_a_preview_saves_nothing(web, keys):
    _etsy(web)
    data = web.client.post("/api/pinterest/queue", json={
        "listing_ids": [1000001, 1000002, 1000003, 1000009], "board_id": "900001",
        "per_day": 2, "images": "1-2", "ai_modified": True, "dry_run": True,
    }).json()
    assert data["dry_run"] is True
    today = date.today()
    assert [e["due"] for e in data["added"]] == [
        today.isoformat(), today.isoformat(),
        (today + timedelta(days=1)).isoformat(), (today + timedelta(days=1)).isoformat(),
    ]
    assert [(e["listing_id"], e["rank"]) for e in data["added"]] == [
        (1000001, 1), (1000001, 2), (1000002, 1), (1000002, 2),
    ]
    assert data["added"][0]["thumb"].endswith("il_170x135.10000011.jpg")
    assert data["added"][0]["ai_modified"] is True
    assert {(p["listing_id"], p["code"]) for p in data["problems"]} == {
        (1000003, "not_active"), (1000009, "not_found"),
    }
    assert not pinterest.queue_path().exists()
    assert "pin_board" not in web.ctx.shop_prefs()


def test_queueing_saves_the_schedule_and_the_choices(web, keys):
    _etsy(web)
    first = _queue_some(web, ids=(1000001,), per_day=2)
    assert len(first["added"]) == 3 and first["summary"]["pending"] == 3
    saved = pinterest.Queue.load().entries
    assert [e["payload"]["link"] for e in saved] == [LISTING_URL] * 3
    assert saved[0]["payload"]["ai_disclosures"] == {"values": ["AI_MODIFIED"]}
    assert saved[0]["thumb"].endswith("il_170x135.10000011.jpg")
    prefs = web.ctx.shop_prefs()
    assert prefs["pin_board"] == "900001" and prefs["pin_board_name"] == "Shirts"
    assert prefs["pin_per_day"] == 2 and prefs["pin_ai"] is True
    # The same images on the same board are not queued twice.
    again = _queue_some(web, ids=(1000001,))
    assert again["added"] == [] and again["skipped"] == 3
    items = web.client.get("/api/pinterest/listings").json()["items"]
    assert items[0]["queued"] == 3

    queue = web.client.get("/api/pinterest/queue").json()
    assert [i["status"] for i in queue["items"]] == ["pending"] * 3
    assert queue["items"][0]["board_name"] == "Shirts"
    assert queue["summary"]["due"] == 2


@pytest.mark.parametrize("change, code", [
    ({"images": "first"}, "pinterest_bad_ranks"),
    ({"images": "3-1"}, "pinterest_bad_ranks"),
    ({"per_day": 0}, "invalid"),
    ({"per_day": "2"}, "invalid"),
    ({"board_id": ""}, "invalid"),
    ({"listing_ids": []}, "invalid"),
    ({"listing_ids": ["abc"]}, "invalid"),
])
def test_bad_queue_requests_are_refused(web, keys, change, code):
    body = {"listing_ids": [1000001], "board_id": "900001", "per_day": 2, "dry_run": True, **change}
    resp = web.client.post("/api/pinterest/queue", json=body)
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == code


def test_queueing_needs_the_etsy_shop(web, keys):
    resp = web.client.post("/api/pinterest/queue", json={
        "listing_ids": [1000001], "board_id": "900001", "dry_run": True,
    })
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "setup_needed"


# --- posting ---------------------------------------------------------------------------------


def test_nothing_due_is_said_plainly(web, pin_api):
    resp = web.client.post("/api/pinterest/post")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "pinterest_nothing_due"


def test_posting_the_due_pins(web, pin_api):
    _etsy(web)
    _queue_some(web, ids=(1000001,), per_day=2)
    job = web.client.post("/api/pinterest/post", json={"confirm": True}).json()["job"]
    assert job["title_key"] == "pinterest:job.post" and job["params"] == {"n": 2}
    final = wait_for_job(web, job["id"])
    assert final["status"] == "done", final
    assert final["result"] == {"posted": 2, "failed": 0, "uncertain": 0, "total": 2}
    assert [p["board_id"] for p in pin_api.pins] == ["900001", "900001"]
    statuses = [e["status"] for e in pinterest.Queue.load().entries]
    assert statuses == ["posted", "posted", "pending"]
    notes = web.client.get("/api/notifications").json()["items"]
    assert notes[0]["key"] == "notify.posted" and notes[0]["params"] == {"n": 2}


def test_an_ambiguous_failure_is_parked_and_can_be_retried(web, pin_api):
    _etsy(web)
    _queue_some(web, ids=(1000001,), per_day=1)
    pin_api.create_status = 500
    final = wait_for_job(web, web.client.post("/api/pinterest/post", json={"confirm": True}).json()["job"]["id"])
    assert final["result"]["uncertain"] == 1
    assert len(pin_api.pins) == 1  # a write is never repeated on a 5xx
    queue = web.client.get("/api/pinterest/queue").json()
    parked = [i for i in queue["items"] if i["status"] == "uncertain"]
    assert len(parked) == 1 and parked[0]["message"] == "refused"
    assert queue["items"][0]["status"] == "uncertain"  # problems are listed first
    notes = web.client.get("/api/notifications").json()["items"]
    assert notes[0]["key"] == "notify.posted_problems"

    retried = web.client.post("/api/pinterest/retry", json={"ids": [parked[0]["id"]]}).json()
    assert retried["retried"] == 1
    entry = next(e for e in pinterest.Queue.load().entries if e["key"] == parked[0]["id"])
    assert entry["status"] == "pending" and entry["due"] == date.today().isoformat()
    again = web.client.post("/api/pinterest/retry", json={"ids": [parked[0]["id"]]})
    assert again.status_code == 409 and again.json()["error"]["code"] == "pinterest_nothing_to_retry"


def test_a_definite_refusal_fails_the_pin(web, pin_api):
    _etsy(web)
    _queue_some(web, ids=(1000002,), per_day=1)
    pin_api.create_status = 400
    final = wait_for_job(web, web.client.post("/api/pinterest/post", json={"confirm": True}).json()["job"]["id"])
    assert final["result"] == {"posted": 0, "failed": 1, "uncertain": 0, "total": 1}


def test_posting_needs_a_connected_account(web, keys):
    _etsy(web)
    _queue_some(web, ids=(1000002,))
    resp = web.client.post("/api/pinterest/post")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "pinterest_not_connected"


def test_removing_keeps_posted_pins(web, pin_api):
    _etsy(web)
    _queue_some(web, ids=(1000002,), per_day=1)
    wait_for_job(web, web.client.post("/api/pinterest/post", json={"confirm": True}).json()["job"]["id"])
    keys_ = [e["key"] for e in pinterest.Queue.load().entries]
    data = web.client.post("/api/pinterest/remove", json={"ids": keys_}).json()
    assert data["removed"] == 1
    assert [e["status"] for e in pinterest.Queue.load().entries] == ["posted"]
    resp = web.client.post("/api/pinterest/remove", json={"ids": keys_})
    assert resp.status_code == 409
    assert web.client.post("/api/pinterest/remove", json={"ids": []}).status_code == 422


# --- review fixes -------------------------------------------------------------------------------


@pytest.mark.parametrize("body", [None, {}, {"confirm": False}, {"confirm": "true"}])
def test_posting_needs_confirm_true(web, pin_api, body):
    _etsy(web)
    _queue_some(web, ids=(1000001,), per_day=2)
    resp = web.client.post("/api/pinterest/post", json=body)
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "confirm_required"
    assert pin_api.pins == []
    assert web.client.get("/api/jobs", params={"kind": "pinterest"}).json() == []
    assert {e["status"] for e in pinterest.Queue.load().entries} == {"pending"}


def test_pins_carry_etsy_titles_as_plain_text(web, keys, monkeypatch):
    # Etsy sends listing titles HTML-escaped; a Pin must never show "&#39;" or "&amp;".
    escaped = dict(LISTINGS[1000002], title="Mom&#39;s &quot;Best&quot; Coffee Mug &amp; Gift")
    monkeypatch.setitem(LISTINGS, 1000002, escaped)
    _etsy(web)
    _queue_some(web, ids=(1000002,), per_day=2)
    entries = pinterest.Queue.load().entries
    assert entries and all(e["payload"]["title"].startswith("Mom's \"Best\" Coffee Mug & Gift")
                           for e in entries)
    text = json.dumps([e["payload"] for e in entries])
    assert "&#39;" not in text and "&quot;" not in text and "&amp;" not in text
    items = web.client.get("/api/pinterest/listings", params={"refresh": 1}).json()["items"]
    assert items[1]["title"] == "Mom's \"Best\" Coffee Mug & Gift"


def test_the_consent_popup_gets_no_handle_on_the_app():
    # Reverse tabnabbing: the tab Pinterest's page opens in must not keep window.opener.
    from pathlib import Path

    source = (Path(api.__file__).parents[1] / "static" / "js" / "pages" / "pinterest.js").read_text(
        encoding="utf-8")
    opened = source.index('window.open("", "_blank")')
    assert "w.opener = null" in source[opened:opened + 600]
    assert 'api.post("/api/pinterest/post", { confirm: true })' in source


# --- round 2 ------------------------------------------------------------------------------------


def _big_shop(web, active):
    """A shop with `active` live listings, answered a page at a time like Etsy does."""
    fake = use_fake_etsy(web)
    rows = [{"listing_id": 2000000 + i, "state": "active", "title": f"Example Print {i}",
             "url": f"https://www.etsy.com/listing/{2000000 + i}/example-print",
             "tags": [], "images": _images(2000000 + i, [1])} for i in range(active)]

    def by_shop(request):
        assert request.url.params.get("state") == "active"
        offset = int(request.url.params.get("offset", "0"))
        limit = int(request.url.params.get("limit", "25"))
        return {"count": len(rows), "results": rows[offset:offset + limit]}

    fake.add("GET", f"/shops/{ETSY_SHOP_ID}/listings", by_shop)
    return fake


def test_the_picker_says_how_many_active_listings_the_shop_really_has(web, keys):
    # Review pinterest-count-capped: a shop with 2,575 live listings is not "500".
    fake = _big_shop(web, 2575)
    data = web.client.get("/api/pinterest/listings").json()
    assert len(data["items"]) == data["shown"] == api.LISTINGS_MAX
    assert data["total"] == 2575 and data["truncated"] is True
    count_calls = [r for r in fake.requests if r.url.params.get("limit") == "1"]
    assert len(count_calls) == 1  # one limit=1 request for the count
    # Cached with the list: a second look asks Etsy nothing.
    before = len(fake.calls)
    again = web.client.get("/api/pinterest/listings").json()
    assert again["total"] == 2575 and len(fake.calls) == before


def test_a_shop_the_picker_shows_whole_is_not_marked_cut(web, keys):
    fake = _etsy(web)
    data = web.client.get("/api/pinterest/listings").json()
    assert data["total"] == data["shown"] == 2 and data["truncated"] is False
    assert not [r for r in fake.requests if r.url.params.get("limit") == "1"]
    # Exactly LISTINGS_MAX: the count proves nothing was left out.
    _big_shop(web, api.LISTINGS_MAX)
    data = web.client.get("/api/pinterest/listings", params={"refresh": 1}).json()
    assert data["total"] == data["shown"] == api.LISTINGS_MAX and data["truncated"] is False


def test_the_picker_still_answers_when_the_count_fails(web, keys):
    fake = _big_shop(web, 600)
    listings_route = fake.routes[("GET", f"/shops/{ETSY_SHOP_ID}/listings")]

    def no_count(request):
        if request.url.params.get("limit") == "1":
            return httpx.Response(500, json={"error": "boom"})
        return listings_route(request)

    fake.add("GET", f"/shops/{ETSY_SHOP_ID}/listings", no_count)
    data = web.client.get("/api/pinterest/listings").json()
    assert data["shown"] == api.LISTINGS_MAX and data["total"] == api.LISTINGS_MAX


def test_pins_queued_before_the_fix_show_and_post_as_plain_text(web, pin_api):
    # A queue file from 0.2.0: Etsy's escaped title, no plain_text mark.
    today = date.today().isoformat()
    old = {"key": "1000002:1:900001", "listing_id": 1000002, "rank": 1, "due": today,
           "status": "pending", "pin_id": None, "message": "",
           "payload": {"board_id": "900001", "title": "Mom&#39;s Coffee Mug &amp; Gift",
                       "description": "Mom&#39;s Coffee Mug &amp; Gift", "link": LISTING_URL,
                       "alt_text": "Mom&#39;s Coffee Mug &amp; Gift",
                       "media_source": {"source_type": "image_url",
                                        "url": "https://i.etsystatic.com/x/il_fullxfull.1.jpg"}}}
    pinterest.queue_path().parent.mkdir(parents=True, exist_ok=True)
    pinterest.queue_path().write_text(json.dumps([old]), encoding="utf-8")
    items = web.client.get("/api/pinterest/queue").json()["items"]
    assert items[0]["title"] == "Mom's Coffee Mug & Gift"
    job = web.client.post("/api/pinterest/post", json={"confirm": True}).json()["job"]
    final = wait_for_job(web, job["id"])
    assert final["status"] == "done", final
    sent = pin_api.pins[0]
    assert sent["title"] == sent["description"] == sent["alt_text"] == "Mom's Coffee Mug & Gift"
    saved = pinterest.Queue.load().entries[0]
    assert saved["status"] == "posted" and saved["plain_text"] is True
