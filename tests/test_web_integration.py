"""How the screens work together: the job lanes, caches that one area drops after
another area wrote to Etsy, the job kinds and links the pages rely on, and the
consent tab's landing page.

All data is invented: ExampleShop (shop 12345678), listings from 1000001.
"""

from __future__ import annotations

import re
import socket
import threading
import time
import urllib.parse
from pathlib import Path

import httpx
import pytest
from web_helpers import ETSY_SHOP_ID, FakeEtsy, use_fake_etsy, wait_for_job

from etsyotomasyon import auth
from etsyotomasyon import pinterest as pin
from etsyotomasyon.client import EtsyClient, RateLimiter
from etsyotomasyon.web.api import listings as listings_api
from etsyotomasyon.web.api import profit
from etsyotomasyon.web.jobs import LANES
from etsyotomasyon.web.server import STATIC_DIR

WEB_DIR = Path(STATIC_DIR).parent
JS_DIR = STATIC_DIR / "js"
PAGES_DIR = JS_DIR / "pages"
LISTINGS_PATH = f"/shops/{ETSY_SHOP_ID}/listings"


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    """No real network, no rate limit, no retry pauses, empty module caches."""

    def refuse():
        raise OSError("no network in tests")

    monkeypatch.setattr(profit, "_fetch_tcmb", refuse)
    monkeypatch.setattr(RateLimiter, "acquire", lambda self: None)
    monkeypatch.setattr(EtsyClient, "_backoff", staticmethod(lambda attempt: 0.0))
    listings_api.invalidate()
    yield
    listings_api.invalidate()


def _wait_running(job, timeout=5.0):
    deadline = time.monotonic() + timeout
    while job.status != "running" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert job.status == "running"


# ================================================================== job lanes


def test_a_read_job_runs_beside_a_long_write_job(web):
    release = threading.Event()
    upload = web.ctx.jobs.start("designs", "designs:job.title", lambda j: release.wait(10))
    try:
        _wait_running(upload)
        numbers = web.ctx.jobs.start("profit", "profit:job.title", lambda j: "sums", lane="read")
        assert numbers.wait(5), "a read job waited behind the write job"
        assert numbers.result == "sums" and upload.status == "running"
    finally:
        release.set()
    assert upload.wait(5)


def test_each_lane_is_strictly_serial(web):
    order: list[str] = []
    gates = {name: threading.Event() for name in ("w1", "r1")}

    def step(name):
        def work(job):
            order.append(f"{name}:start")
            if name in gates:
                gates[name].wait(10)
            order.append(f"{name}:end")
        return work

    w1 = web.ctx.jobs.start("test", "common:test", step("w1"))
    w2 = web.ctx.jobs.start("test", "common:test", step("w2"))
    r1 = web.ctx.jobs.start("test", "common:test", step("r1"), lane="read")
    r2 = web.ctx.jobs.start("test", "common:test", step("r2"), lane="read")
    _wait_running(w1)
    _wait_running(r1)
    assert w2.status == "queued" and r2.status == "queued"
    gates["r1"].set()
    assert r2.wait(5) and w2.status == "queued"  # the read lane moved on, the write lane did not
    gates["w1"].set()
    assert w2.wait(5)
    assert order.index("w1:end") < order.index("w2:start")
    assert order.index("r1:end") < order.index("r2:start")


def test_an_unknown_lane_is_refused(web):
    assert LANES == ("write", "read")
    with pytest.raises(ValueError):
        web.ctx.jobs.start("test", "common:test", lambda j: None, lane="fast")


def test_summaries_name_their_lane_and_the_list_filters_by_it(web):
    w = web.ctx.jobs.start("test", "common:test", lambda j: None)
    r = web.ctx.jobs.start("test", "common:test", lambda j: None, lane="read")
    assert wait_for_job(web, w.id)["lane"] == "write"
    assert wait_for_job(web, r.id)["lane"] == "read"
    assert [j.id for j in web.ctx.jobs.list(lane="read")] == [r.id]
    assert [j.id for j in web.ctx.jobs.list(lane="write")] == [w.id]


def test_the_shop_cannot_change_while_a_read_job_runs(web):
    release = threading.Event()
    job = web.ctx.jobs.start("profit", "profit:job.title", lambda j: release.wait(10), lane="read")
    try:
        _wait_running(job)
        assert web.ctx.jobs.busy() and not web.ctx.jobs.busy(lane="write")
        for path, body in (("/api/shops/add", {}), ("/api/shops/switch", {"id": ""})):
            resp = web.client.post(path, json=body)
            assert resp.status_code == 409 and resp.json()["error"]["code"] == "busy", path
    finally:
        release.set()
    assert job.wait(5)
    assert web.client.post("/api/shops/add", json={}).status_code == 200


def test_a_queued_read_job_can_be_cancelled(web):
    release = threading.Event()
    first = web.ctx.jobs.start("test", "common:test", lambda j: release.wait(10), lane="read")
    second = web.ctx.jobs.start("test", "common:test", lambda j: "never", lane="read")
    try:
        _wait_running(first)
        resp = web.client.post(f"/api/jobs/{second.id}/cancel", json={})
        assert resp.status_code == 200 and resp.json()["status"] == "cancelled"
    finally:
        release.set()
    assert first.wait(5) and second.result is None


def test_profit_numbers_do_not_wait_for_an_upload(web):
    fake = FakeEtsy()
    empty = {"count": 0, "results": []}
    fake.add("GET", f"/shops/{ETSY_SHOP_ID}/receipts", empty)
    fake.add("GET", f"/shops/{ETSY_SHOP_ID}/payment-account/ledger-entries", empty)
    fake.add("GET", "/listings/batch", empty)
    use_fake_etsy(web, fake)
    release = threading.Event()
    upload = web.ctx.jobs.start("designs", "designs:job.title", lambda j: release.wait(20))
    try:
        _wait_running(upload)
        first = web.client.get("/api/profit").json()
        assert first["state"] == "loading"
        assert first["job"]["lane"] == "read" and first["job"]["kind"] == "profit"
        final = wait_for_job(web, first["job"]["id"], timeout=15)
        assert final["status"] == "done", final
        assert upload.status == "running"
        assert web.client.get("/api/profit").json()["state"] == "ready"
    finally:
        release.set()
    assert upload.wait(5)


# ================================================================== caches across areas


def _listing(listing_id, state="active", tags=("mug", "gift")):
    return {
        "listing_id": listing_id,
        "shop_id": ETSY_SHOP_ID,
        "title": f"Handmade Stoneware Coffee Mug {listing_id}",
        "description": "A sturdy stoneware mug, glazed by hand in small batches.",
        "state": state,
        "tags": list(tags),
        "materials": ["stoneware"],
        "taxonomy_id": 1633,
        "price": {"amount": 1850, "divisor": 100, "currency_code": "USD"},
        "updated_timestamp": 1_700_000_000,
        "url": f"https://www.etsy.com/listing/{listing_id}/example",
        "should_auto_renew": True,
        "images": [{"listing_id": listing_id, "listing_image_id": listing_id * 10, "rank": 1,
                    "url_75x75": "https://img.example.test/75.jpg",
                    "url_170x135": "https://img.example.test/170.jpg",
                    "url_570xN": "https://img.example.test/570.jpg",
                    "url_fullxfull": "https://img.example.test/full.jpg"}],
    }


class Shop:
    """A few invented listings behind the fake Etsy that both areas read and write."""

    def __init__(self, web, listings):
        self.fake = use_fake_etsy(web)
        self.by_id = {x["listing_id"]: x for x in listings}
        self.fake.add("GET", LISTINGS_PATH, self.shop_listings)
        self.fake.add("GET", "/seller-taxonomy/nodes", {"count": 1, "results": [
            {"id": 2, "name": "Home & Living", "children": [{"id": 1633, "name": "Mugs", "children": []}]},
        ]})
        self.fake.add("GET", "/listings/batch", self.batch)
        for listing_id in self.by_id:
            self.fake.add("PATCH", f"{LISTINGS_PATH}/{listing_id}", self.patch(listing_id))
            self.fake.add("GET", f"/listings/{listing_id}/images",
                          {"count": 1, "results": self.by_id[listing_id]["images"]})

    def shop_listings(self, request: httpx.Request):
        q = dict(request.url.params)
        rows = [x for x in self.by_id.values() if x["state"] == q.get("state", "active")]
        offset, limit = int(q.get("offset", 0)), int(q.get("limit", 25))
        return {"count": len(rows), "results": rows[offset:offset + limit]}

    def batch(self, request: httpx.Request):
        ids = {int(i) for i in request.url.params.get("listing_ids", "").split(",") if i}
        rows = [x for i, x in self.by_id.items() if i in ids]
        return {"count": len(rows), "results": rows}

    def patch(self, listing_id):
        def handler(request: httpx.Request):
            form = dict(urllib.parse.parse_qsl(request.content.decode()))
            item = self.by_id[listing_id]
            for key, value in form.items():
                item[key] = value.split(",") if key in ("tags", "materials") else value
            return {k: v for k, v in item.items() if k != "images"}
        return handler

    def reads(self) -> int:
        return sum(1 for call in self.fake.calls if call == ("GET", LISTINGS_PATH))


def test_an_seo_fix_drops_the_listings_table_cache(web):
    shop = Shop(web, [_listing(1000001), _listing(1000002)])
    assert web.client.get("/api/listings", params={"tab": "active"}).status_code == 200
    before = shop.reads()
    web.client.get("/api/listings", params={"tab": "active"})
    assert shop.reads() == before  # cached

    resp = web.client.post("/api/seo/fix/1000001", json={
        "tags": ["mug", "gift", "coffee mug", "stoneware mug"], "confirm": True})
    assert resp.status_code == 200, resp.text

    row = next(r for r in web.client.get("/api/listings", params={"tab": "active"}).json()["items"]
               if r["id"] == 1000001)
    assert shop.reads() > before, "the listings table kept its stale copy"
    assert row["tags"] == 4  # the table shows how many tags a listing has


def test_the_seo_fix_keeps_its_own_updated_audit(web):
    shop = Shop(web, [_listing(1000001), _listing(1000002)])
    assert web.client.get("/api/seo/audit").json()["cached"] is False
    resp = web.client.post("/api/seo/fix/1000001", json={"tags": ["mug", "gift", "coffee mug"],
                                                         "confirm": True})
    assert resp.status_code == 200, resp.text
    reads = shop.reads()
    again = web.client.get("/api/seo/audit").json()
    assert again["cached"] is True and shop.reads() == reads


def test_a_listing_edit_drops_the_seo_audit(web):
    Shop(web, [_listing(1000001), _listing(1000002)])
    assert web.client.get("/api/seo/audit").json()["cached"] is False
    assert web.client.get("/api/seo/audit").json()["cached"] is True
    resp = web.client.patch("/api/listings/1000001", json={
        "tags": ["mug", "gift", "coffee cup"], "confirm": True})
    assert resp.status_code == 200, resp.text
    audit = web.client.get("/api/seo/audit").json()
    assert audit["cached"] is False
    item = next(i for i in audit["items"] if i["listing_id"] == 1000001)
    assert item["tags"] == ["mug", "gift", "coffee cup"]


def test_publishing_a_draft_drops_the_seo_audit(web):
    Shop(web, [_listing(1000001, state="draft"), _listing(1000002)])
    assert web.client.get("/api/seo/audit", params={"state": "draft"}).json()["cached"] is False
    resp = web.client.post("/api/listings/1000001/publish", json={"confirm": True})
    assert resp.status_code == 200, resp.text
    assert web.client.get("/api/seo/audit", params={"state": "draft"}).json()["cached"] is False


def test_a_change_reaches_every_other_cache_and_survives_a_broken_one(web):
    seen: list[str] = []
    web.ctx.on_change("listings", lambda c: seen.append("a"), name="a")
    web.ctx.on_change("listings", lambda c: 1 / 0, name="broken")
    web.ctx.on_change("listings", lambda c: seen.append("b"), name="b")
    web.ctx.on_change("orders", lambda c: seen.append("orders"), name="c")
    web.ctx.changed("listings", source="a")
    assert seen == ["b"]
    web.ctx.changed("orders")
    assert seen == ["b", "orders"]
    with pytest.raises(ValueError):
        web.ctx.changed("everything")
    with pytest.raises(ValueError):
        web.ctx.on_change("everything", lambda c: None)


def test_the_dashboard_forgets_listing_counts_after_a_change(web):
    Shop(web, [_listing(1000001), _listing(1000002, state="draft")])
    first = web.client.get("/api/dashboard/stats").json()["stats"]
    assert first["active"]["value"] == 1
    again = web.client.get("/api/dashboard/stats").json()["stats"]
    assert again["active"]["cached_at"] == first["active"]["cached_at"]
    web.client.post("/api/listings/1000002/publish", json={"confirm": True})
    after = web.client.get("/api/dashboard/stats").json()["stats"]
    assert after["active"]["cached_at"] != first["active"]["cached_at"]
    assert after["active"]["value"] == 2


# ================================================================== contracts in the source


def _module_constants(text: str) -> dict[str, str]:
    return dict(re.findall(r'^([A-Z_]+)\s*=\s*"([a-z-]+)"', text, re.MULTILINE))


def _server_job_kinds() -> set[str]:
    kinds: set[str] = set()
    for path in (WEB_DIR / "api").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        consts = _module_constants(text)
        for arg in re.findall(r"jobs\.start\(\s*([A-Z_]+|\"[a-z-]+\")", text):
            kinds.add(arg.strip('"') if arg.startswith('"') else consts[arg])
    return kinds


def test_the_job_kinds_pages_wait_for_are_kinds_the_server_starts():
    kinds = _server_job_kinds()
    assert {"designs", "publish", "listings-import", "orders", "profit", "connect",
            "pinterest"} <= kinds
    used: dict[str, set[str]] = {}
    patterns = (
        r"\b(?:job|ev)\.kind\s*[!=]==\s*\"([a-z-]+)\"",
        r"\"/api/jobs\",\s*\{\s*kind:\s*\"([a-z-]+)\"",
    )
    for path in [*PAGES_DIR.glob("*.js"), JS_DIR / "app.js"]:
        text = path.read_text(encoding="utf-8")
        found = {k for pattern in patterns for k in re.findall(pattern, text)}
        stat = re.search(r"STAT_JOBS = new Set\(\[([^\]]*)\]\)", text)
        if stat:
            found |= set(re.findall(r"\"([a-z-]+)\"", stat.group(1)))
        if found:
            used[path.name] = found
    assert "panel.js" in used and "listings.js" in used  # the scan works
    wrong = {name: sorted(k - kinds) for name, k in used.items() if k - kinds}
    assert not wrong, f"pages wait for job kinds nobody starts: {wrong}"
    assert set(listings_api.LISTING_JOB_KINDS) <= kinds


def _route_pages() -> dict[str, str]:
    app = (JS_DIR / "app.js").read_text(encoding="utf-8")
    table = app.split("export const ROUTES", 1)[1].split("];", 1)[0]
    return dict(re.findall(r'path:\s*"([^"]+)",\s*page:\s*"([a-z-]+)"', table))


def _params_read_by(page: str) -> set[str]:
    text = (PAGES_DIR / f"{page}.js").read_text(encoding="utf-8")
    names = set(re.findall(r"\b(?:q0|ctx\.query)\.([a-zA-Z_]+)", text))
    for body in re.findall(r"setQuery\(\{([^}]*)\}", text):
        names |= set(re.findall(r"\b([a-z_]+):", body))
    return names


def test_links_between_pages_use_the_query_the_target_reads():
    routes = _route_pages()
    link = re.compile(r"[\"'`](/[a-z/-]+)\?([a-z_]+=[^\"'`$]*)")
    sources = [*PAGES_DIR.glob("*.js"), JS_DIR / "app.js", *(WEB_DIR / "api").glob("*.py")]
    checked = 0
    problems = []
    for path in sources:
        for target, query in link.findall(path.read_text(encoding="utf-8")):
            page = routes.get(target)
            if page is None:
                continue
            checked += 1
            wanted = {part.split("=", 1)[0] for part in query.split("&")}
            unknown = wanted - _params_read_by(page)
            if unknown:
                problems.append(f"{path.name}: {target}?{query} ({page}.js reads no {sorted(unknown)})")
    assert checked >= 4  # panel, designs, profit, notifications
    assert not problems, problems


def test_the_panel_links_to_the_listings_tabs():
    text = (PAGES_DIR / "panel.js").read_text(encoding="utf-8")
    # The drafts card (the video's "Bu ay oluşturulan taslak") opens the drafts tab.
    assert '"/ilanlar?tab=draft"' in text
    assert "?state=" not in text


# ================================================================== the consent tab


def test_the_consent_tab_page_is_drawn_before_any_session_call():
    app = (JS_DIR / "app.js").read_text(encoding="utf-8")
    boot = app[app.index("async function boot()"):]
    assert boot.index("OAUTH_DONE_PATH") < boot.index('api.get("/api/session")')
    done = app[app.index("async function showOAuthDone()"):app.index("function listenToOtherTabs()")]
    assert "api." not in done  # no API call: that tab has no session cookie
    assert 'BroadcastChannel(CHANNEL)' in done and "window.close()" in done
    for page in ("connect.js", "pinterest.js"):  # neither page is mounted on that address
        assert not re.search(r"[\"'`]/oauth-done", (PAGES_DIR / page).read_text(encoding="utf-8"))


def test_the_oauth_done_address_needs_no_session(web):
    with web.anonymous() as http:  # the cross-site redirect chain carries no Strict cookie
        page = http.get("/oauth-done", params={"service": "pinterest"})
        common = http.get("/i18n/common.json")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert common.status_code == 200
    words = common.json()
    assert words["tr"]["fullpage.oauth_done"].startswith("Bağlandı")
    assert words["en"]["fullpage.oauth_done"].startswith("Connected")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _serve(handler):
    port = _free_port()
    server_cls = auth.LoopbackServer if handler is auth._CallbackHandler else pin.LoopbackServer
    server = server_cls(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                     daemon=True).start()
    return server, f"http://127.0.0.1:{port}"


@pytest.mark.parametrize("handler", [auth._CallbackHandler, pin._Callback], ids=["etsy", "pinterest"])
def test_only_a_success_is_sent_back_to_the_app(handler):
    handler.return_url = "http://localhost:3000/oauth-done"
    handler.result = {}
    if handler is auth._CallbackHandler:
        handler.expected_path = "/"
    server, base = _serve(handler)
    try:
        with httpx.Client(trust_env=False) as http:
            refused = http.get(f"{base}/?error=access_denied&error_description=denied&state=s1")
            handler.result = {}
            both = http.get(f"{base}/?code=c1&error=access_denied&state=s1")
            handler.result = {}
            ok = http.get(f"{base}/?code=c1&state=s1")
    finally:
        server.shutdown()
        server.server_close()
        handler.return_url = None
        handler.result = {}
    assert refused.status_code in (200, 400) and "location" not in refused.headers
    assert both.status_code in (200, 400) and "location" not in both.headers
    assert ok.status_code == 302 and ok.headers["location"] == "http://localhost:3000/oauth-done"


# --- one event stream per browser ---------------------------------------------------------------


def test_the_tabs_of_a_browser_share_one_event_stream(web):
    """A browser gives a host about 6 connections for all its tabs; a stream per tab used
    them up with six tabs open. events.js elects one leader tab (Web Locks) that holds
    the stream and relays it over a BroadcastChannel; the server's event ids let a tab
    drop the doubles while two leaders overlap."""
    src = (JS_DIR / "events.js").read_text(encoding="utf-8")
    for needle in ("navigator.locks", "BroadcastChannel", "steal: true", "lastEventId",
                   'new EventSource("/api/events")'):
        assert needle in src, needle
    assert src.count("new EventSource(") == 1
    # Only the events client opens a stream; pages subscribe through ctx.events.
    for path in JS_DIR.rglob("*.js"):
        if path.name != "events.js":
            assert "EventSource" not in path.read_text(encoding="utf-8"), path.name
    sub = web.ctx.events.subscribe()
    try:
        web.ctx.notify("common", "n")
        assert re.match(r"event: notification\nid: \d+\ndata: ", sub.get(timeout=2))
    finally:
        sub.close()
