"""The new-version notice: comparing versions, reading GitHub's answer, the rhythm (a
look at every start unless the answer is under an hour old, then daily) and its cache,
the off switches, silence on failure, one bell notification per version, and the
endpoints. GitHub is always a fake (httpx.MockTransport)."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

import httpx
import pytest
from web_helpers import read_events

from etsyotomasyon import __version__
from etsyotomasyon.config import base_home
from etsyotomasyon.desktop import settings
from etsyotomasyon.web import launcher, update
from etsyotomasyon.web.update import LATEST_URL, RELEASES_PAGE, UpdateChecker

CURRENT = "0.3.0"
STATE_KEYS = {"current", "auto", "blocked", "enabled", "checked_at", "error", "latest",
              "available", "dismissed", "pill"}
LATEST_KEYS = {"version", "tag", "url", "name", "notes", "published_at"}
DAY = 24 * 60 * 60


def release(tag: str = "v0.3.1", **extra) -> dict:
    """A release as GitHub's /releases/latest returns it (only the fields read here)."""
    return {
        "tag_name": tag,
        "html_url": f"https://github.com/wj-sidiss/etsy-otomasyon-full-full-surum-2/releases/tag/{tag}",
        "name": f"etsyotomasyon {tag}",
        "body": "## Added\r\n- A new version notice <script>alert(1)</script>",
        "published_at": "2026-09-20T10:00:00Z",
        "draft": False,
        "prerelease": False,
        "assets": [{"name": "etsyotomasyon-windows.exe"}],
        **extra,
    }


class FakeGitHub:
    """Answers every request with `answer` (a dict/list for 200 JSON, an httpx.Response,
    or an exception to raise); keeps the requests."""

    def __init__(self, answer=None) -> None:
        self.answer = release() if answer is None else answer
        self.requests: list[httpx.Request] = []
        self._lock = threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.requests.append(request)
        answer = self.answer
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=answer)


class Clock:
    def __init__(self, now: float = 1_790_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def checks_on(monkeypatch):
    """conftest turns every check off; the tests of the checker turn it back on."""
    monkeypatch.delenv("ETSYOTOMASYON_NO_UPDATE_CHECK", raising=False)


def use_checker(web, gh: FakeGitHub, **kwargs) -> UpdateChecker:
    """A fresh checker on the app, talking to `gh`, as if the app ran version CURRENT."""
    web.ctx.updates.close()
    checker = UpdateChecker(web.ctx, transport=httpx.MockTransport(gh), **kwargs)
    checker.current = CURRENT
    web.ctx.updates = checker
    return checker


def update_notifications(web) -> list[dict]:
    return [n for n in web.ctx.notifications() if n["key"] == "update.notify"]


# --- versions -------------------------------------------------------------------------


@pytest.mark.parametrize("text, expected", [
    ("v0.3.1", (0, 3, 1)), ("0.3.1", (0, 3, 1)), ("V1.20.300", (1, 20, 300)),
    (" v0.3.10 ", (0, 3, 10)),
    ("v0.3", None), ("nightly", None), ("v0.3.1-rc1", None), ("v0.3.1.2", None), ("", None),
    (None, None), (31, None),
])
def test_parse_version(text, expected):
    assert update.parse_version(text) == expected


def test_versions_compare_as_numbers_not_text():
    assert update.is_newer("v0.3.10", "0.3.9")
    assert update.is_newer("0.4.0", "0.3.99")
    assert update.is_newer("v1.0.0", "0.9.9")
    assert not update.is_newer("v0.3.9", "0.3.10")
    assert not update.is_newer("v0.3.0", "0.3.0")
    assert not update.is_newer("nightly", "0.3.0")
    assert not update.is_newer("v0.3.1", "unknown")
    # The running version may carry a suffix; its X.Y.Z is what counts.
    assert update.current_version("0.3.0.dev1") == (0, 3, 0)
    assert update.is_newer("0.3.1", "0.3.0.dev1")


# --- reading GitHub's answer ------------------------------------------------------------


def test_parse_release_keeps_only_what_the_page_shows():
    got = update.parse_release(release())
    assert got == {
        "version": "0.3.1",
        "tag": "v0.3.1",
        "url": "https://github.com/wj-sidiss/etsy-otomasyon-full-full-surum-2/releases/tag/v0.3.1",
        "name": "etsyotomasyon v0.3.1",
        # Plain text, as it came (the page shows it with textContent), with \n line ends.
        "notes": "## Added\n- A new version notice <script>alert(1)</script>",
        "published_at": "2026-09-20T10:00:00Z",
    }


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "http://github.com/x", "https://github.com.evil.example/x",
    "https://example.com/release", "https://github.com/a b", 42, None,
])
def test_a_download_link_off_github_becomes_the_releases_page(url):
    assert update.parse_release(release(html_url=url))["url"] == RELEASES_PAGE


@pytest.mark.parametrize("data", [
    release(tag_name="nightly"), release(tag_name="v0.3.1-beta"), release(draft=True),
    release(prerelease=True), [release()], "v0.3.1", None,
])
def test_no_usable_release(data):
    assert update.parse_release(data) is None


def test_odd_fields_are_tamed():
    got = update.parse_release(release(name=None, body=None, published_at="yesterday"))
    assert (got["name"], got["notes"], got["published_at"]) == ("", "", None)
    long = update.parse_release(release(body="x" * (update.MAX_NOTES + 500), name="n" * 1000))
    assert len(long["notes"]) <= update.MAX_NOTES + 2 and long["notes"].endswith("…")
    assert len(long["name"]) <= update.MAX_NAME + 2


# --- the request ----------------------------------------------------------------------------


def test_the_request_sends_nothing_but_a_user_agent(web, checks_on):
    gh = FakeGitHub()
    checker = use_checker(web, gh)
    state = checker.check(force=True)
    assert state["latest"]["version"] == "0.3.1" and state["error"] is None
    [req] = gh.requests
    assert req.method == "GET"
    assert str(req.url) == LATEST_URL and not req.url.query
    assert req.headers["User-Agent"] == f"etsyotomasyon/{__version__}"
    assert req.headers["Accept"] == "application/vnd.github+json"
    assert "cookie" not in req.headers and "authorization" not in req.headers
    assert req.content == b""


def test_a_renamed_repository_is_followed(web, checks_on):
    moved = "https://api.github.com/repositories/123456/releases/latest"

    def answer(req: httpx.Request) -> httpx.Response:
        if str(req.url) == LATEST_URL:
            return httpx.Response(301, headers={"Location": moved})
        return httpx.Response(200, json=release("v0.4.0"))

    gh = FakeGitHub()
    checker = use_checker(web, gh)
    checker.transport = httpx.MockTransport(answer)
    assert checker.check(force=True)["latest"]["version"] == "0.4.0"


# --- the rhythm (every start, then daily) and the cache ------------------------------------------


def test_while_running_the_next_look_is_a_day_later(web, checks_on):
    clock, gh = Clock(), FakeGitHub()
    checker = use_checker(web, gh, clock=clock)
    checker.check()
    assert len(gh.requests) == 1
    checker.check()  # not due: nothing sent
    clock.now += DAY - 60
    checker.check()
    assert len(gh.requests) == 1
    cache = json.loads((base_home() / update.CACHE_FILE).read_text(encoding="utf-8"))
    assert cache["checked_at"] == clock.now - (DAY - 60) and cache["latest"]["version"] == "0.3.1"
    clock.now += 61
    assert checker.due_in() <= 0
    checker.check()
    assert len(gh.requests) == 2


def write_cache(clock: Clock, *, answered_ago: float | None, tried_ago: float | None = None,
                error: str | None = None) -> None:
    """update.json as an earlier run of the app left it."""
    now = clock.now
    tried = answered_ago if tried_ago is None else tried_ago
    (base_home() / update.CACHE_FILE).write_text(json.dumps({
        "checked_at": None if answered_ago is None else now - answered_ago,
        "attempted_at": None if tried is None else now - tried,
        "error": error,
        "latest": release() if answered_ago is not None else None,
        "notified": "0.3.1" if answered_ago is not None else None,
        "dismissed": None,
    }), encoding="utf-8")


def run_loop(checker: UpdateChecker, gh: FakeGitHub, *, looks: int, settle: float = 0.3) -> None:
    """Start the background loop, wait for `looks` requests (or `settle` seconds for
    none), then a little longer for any look that should not come; stop it."""
    checker.start()
    deadline = time.monotonic() + 5
    while len(gh.requests) < looks and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(settle)
    thread = checker._thread
    checker.close()
    thread.join(2)


@pytest.mark.parametrize("answered_ago, looks", [
    (None, 1),                            # never asked: the start asks
    (update.START_FRESH - 60, 0),         # under an hour old: reused
    (update.START_FRESH + 60, 1),         # over an hour old: asked again at the start
    (DAY - 60, 1),                        # a day would still be fresh while running
    (10 * DAY, 1),
])
def test_every_start_asks_unless_the_answer_is_under_an_hour_old(web, checks_on,
                                                                  answered_ago, looks):
    clock, gh = Clock(), FakeGitHub()
    write_cache(clock, answered_ago=answered_ago)
    checker = use_checker(web, gh, clock=clock, first_delay=0.01, min_wait=0.05)
    assert checker.start_fresh == update.START_FRESH == 60 * 60
    run_loop(checker, gh, looks=looks)
    assert len(gh.requests) == looks  # and once asked, the next look is a day away
    state = checker.state()
    assert state["latest"]["version"] == "0.3.1" and state["available"]
    if looks:
        assert state["checked_at"] == clock.now
    else:
        assert state["checked_at"] == clock.now - answered_ago


def test_a_start_after_the_answer_aged_asks_while_the_old_one_still_shows(web, checks_on):
    """The start rule is only for the start: a running app keeps its day."""
    clock, gh = Clock(), FakeGitHub()
    write_cache(clock, answered_ago=2 * update.START_FRESH)
    checker = use_checker(web, gh, clock=clock)
    assert checker.due_in() > 0  # while running, a two-hour-old answer is not due
    assert checker.due_in(max_age=checker.start_fresh) <= 0
    checker.check()
    assert gh.requests == []
    checker.check(max_age=checker.start_fresh)
    assert len(gh.requests) == 1


def test_a_failed_look_is_not_repeated_by_a_quick_restart(web, checks_on):
    """The hour after a failed look holds across a restart too: at most one try an hour."""
    clock, gh = Clock(), FakeGitHub()
    write_cache(clock, answered_ago=3 * update.START_FRESH, tried_ago=60, error="offline")
    checker = use_checker(web, gh, clock=clock, first_delay=0.01, min_wait=0.05)
    assert checker.state()["error"] == "offline"
    run_loop(checker, gh, looks=0)
    assert gh.requests == []
    clock.now += update.RETRY_AFTER
    checker = use_checker(web, gh, clock=clock, first_delay=0.01, min_wait=0.05)
    run_loop(checker, gh, looks=1)
    assert len(gh.requests) == 1 and checker.state()["error"] is None


def test_switched_on_later_the_first_look_follows_the_start_rule(web, checks_on):
    clock, gh = Clock(), FakeGitHub()
    write_cache(clock, answered_ago=2 * update.START_FRESH)
    prefs = settings.load_app_prefs()
    prefs["update_check"] = False
    settings.save_app_prefs(prefs)
    checker = use_checker(web, gh, clock=clock, first_delay=0.01, min_wait=0.05)
    checker.start()
    time.sleep(0.2)
    assert gh.requests == []  # off at the start: no look
    checker.set_auto(True)  # wakes the loop: a two-hour-old answer is asked again
    deadline = time.monotonic() + 5
    while not gh.requests and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.2)
    assert len(gh.requests) == 1
    checker.close()


# step() is one pass of the background loop; what it returns is what the loop waits.


def test_a_start_reuses_a_fresh_answer_and_looks_a_day_after_it(web, checks_on):
    clock, gh = Clock(), FakeGitHub()
    write_cache(clock, answered_ago=30 * 60)
    checker = use_checker(web, gh, clock=clock)
    starting, wait = checker.step(True)
    assert gh.requests == [] and starting is False
    assert wait == pytest.approx(DAY - 30 * 60)
    clock.now += wait
    starting, wait = checker.step(starting)
    assert len(gh.requests) == 1 and wait == pytest.approx(DAY)


def test_a_start_look_put_off_by_a_failed_try_comes_an_hour_after_it(web, checks_on):
    """Not a day after the old answer: the start still wants its look."""
    clock, gh = Clock(), FakeGitHub()
    write_cache(clock, answered_ago=3 * update.START_FRESH, tried_ago=60, error="offline")
    checker = use_checker(web, gh, clock=clock)
    starting, wait = checker.step(True)
    assert gh.requests == [] and starting is True
    assert wait == pytest.approx(update.RETRY_AFTER - 60)
    clock.now += wait
    starting, wait = checker.step(starting)
    assert len(gh.requests) == 1 and starting is False and wait == pytest.approx(DAY)


def test_a_failing_start_look_is_tried_again_hourly_until_it_answers(web, checks_on):
    clock = Clock()
    gh = FakeGitHub(httpx.ConnectError("no network"))
    write_cache(clock, answered_ago=3 * update.START_FRESH)
    checker = use_checker(web, gh, clock=clock)
    starting, wait = checker.step(True)
    assert len(gh.requests) == 1 and starting is True
    assert wait == pytest.approx(update.RETRY_AFTER)
    assert checker.state()["error"] == "offline"
    assert checker.state()["latest"]["version"] == "0.3.1"  # the old answer still shows
    clock.now += wait
    gh.answer = release("v0.3.2")
    starting, wait = checker.step(starting)
    assert len(gh.requests) == 2 and starting is False and wait == pytest.approx(DAY)
    assert checker.state()["latest"]["version"] == "0.3.2" and checker.state()["error"] is None


def test_with_the_checks_off_a_step_looks_at_nothing(web, checks_on):
    clock, gh = Clock(), FakeGitHub()
    checker = use_checker(web, gh, clock=clock)
    checker.set_auto(False)
    assert checker.step(True) == (True, update.INTERVAL)
    assert gh.requests == []
    checker.set_auto(True)
    starting, wait = checker.step(True)
    assert len(gh.requests) == 1 and starting is False and wait == pytest.approx(DAY)


def test_a_failed_look_is_tried_again_an_hour_later(web, checks_on):
    clock = Clock()
    gh = FakeGitHub(httpx.ConnectError("no network"))
    checker = use_checker(web, gh, clock=clock)
    checker.check()
    assert checker.due_in() == pytest.approx(update.RETRY_AFTER)
    clock.now += update.RETRY_AFTER + 1
    gh.answer = release()
    checker.check()
    assert len(gh.requests) == 2 and checker.state()["error"] is None


def test_a_clock_set_back_does_not_stop_the_checks(web, checks_on):
    clock, gh = Clock(), FakeGitHub()
    checker = use_checker(web, gh, clock=clock)
    checker.check()
    clock.now -= 10 * DAY  # the answer now seems to come from the future
    assert checker.due_in() <= 0


def test_a_tampered_cache_is_checked_like_an_answer(web, checks_on):
    path = base_home() / update.CACHE_FILE
    path.write_text(json.dumps({
        "checked_at": "soon", "error": "<b>", "notified": "x", "dismissed": "v0.3.1",
        "latest": {"version": "9.9.9", "tag": "v9.9.9", "url": "javascript:alert(1)",
                   "notes": ["not text"], "published_at": None},
    }), encoding="utf-8")
    state = use_checker(web, FakeGitHub()).state()
    assert state["latest"]["url"] == RELEASES_PAGE and state["latest"]["notes"] == ""
    assert state["checked_at"] is None and state["error"] is None
    path.write_text("{not json", encoding="utf-8")
    assert use_checker(web, FakeGitHub()).state()["latest"] is None


def test_the_background_loop_looks_once_then_waits(web, checks_on):
    gh = FakeGitHub()
    checker = use_checker(web, gh, first_delay=0.01, min_wait=0.05)
    checker.start()
    checker.start()  # idempotent
    deadline = time.monotonic() + 5
    while not gh.requests and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.2)
    assert len(gh.requests) == 1  # the next one is due in a day
    thread = checker._thread
    checker.close()
    thread.join(2)
    assert not thread.is_alive()


def test_the_launcher_starts_the_checker():
    import httpx as http_mod

    seen = {}

    def ready(server):
        seen["thread"] = server.ctx.updates._thread
        with http_mod.Client(base_url=f"http://127.0.0.1:{server.port}", trust_env=False,
                             verify=False) as http:
            http.get(f"/?k={server.ctx.token}")
            http.post("/api/quit", json={}, headers={"X-Etsyotomasyon": "1"})

    assert launcher.launch(open_browser=False, ports=[0], ready=ready) == 0
    assert seen["thread"] is not None and seen["thread"].name == "etsyotomasyon-update"


# --- the off switches ------------------------------------------------------------------------------


def test_tests_never_look_by_default(web):
    gh = FakeGitHub()
    checker = use_checker(web, gh, first_delay=0.0)
    state = checker.check(force=True)
    assert state["blocked"] and not state["enabled"] and state["auto"]
    resp = web.client.post("/api/update/check")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "update_check_off"
    checker.start()
    time.sleep(0.2)
    assert gh.requests == []


@pytest.mark.parametrize("value, blocked", [
    ("1", True), ("true", True), ("yes", True), ("", False), ("0", False), ("false", False),
])
def test_the_environment_switch(web, monkeypatch, value, blocked):
    monkeypatch.setenv("ETSYOTOMASYON_NO_UPDATE_CHECK", value)
    gh = FakeGitHub()
    checker = use_checker(web, gh)
    checker.check(force=True)
    assert checker.blocked is blocked
    assert len(gh.requests) == (0 if blocked else 1)


def test_the_settings_toggle_stops_the_automatic_looks(web, checks_on):
    gh = FakeGitHub()
    checker = use_checker(web, gh, first_delay=0.0, min_wait=0.05)
    assert checker.state()["auto"] is True  # on by default
    resp = web.client.post("/api/update/auto", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json()["auto"] is False and resp.json()["enabled"] is False
    assert settings.load_app_prefs()["update_check"] is False
    checker.start()
    time.sleep(0.3)
    assert gh.requests == []
    # A look asked for by hand still works, but the pill stays away.
    state = web.client.post("/api/update/check").json()
    assert len(gh.requests) == 1 and state["available"] and not state["pill"]
    assert web.client.post("/api/update/auto", json={"enabled": "no"}).status_code == 422
    # Turned back on: the pill is back.
    assert web.client.post("/api/update/auto", json={"enabled": True}).json()["pill"] is True


# --- failures are quiet ------------------------------------------------------------------------------


@pytest.mark.parametrize("answer, code", [
    (httpx.ConnectError("no network"), "offline"),
    (httpx.ReadTimeout("slow"), "offline"),
    (httpx.Response(403, json={"message": "API rate limit exceeded"}), "rate_limited"),
    (httpx.Response(429, text="slow down"), "rate_limited"),
    (httpx.Response(500, text="oops"), "bad_response"),
    (httpx.Response(200, text="<html>not json</html>"), "bad_response"),
    (httpx.Response(200, json=["not", "an", "object"]), "bad_response"),
])
def test_a_failure_is_only_a_log_line(web, checks_on, caplog, answer, code):
    checker = use_checker(web, FakeGitHub(answer))
    with caplog.at_level(logging.WARNING, logger="etsyotomasyon.web"):
        state = checker.check(force=True)
    assert state["error"] == code and state["latest"] is None and not state["pill"]
    assert "update check failed" in caplog.text
    assert update_notifications(web) == []


def test_a_failure_keeps_the_last_good_answer(web, checks_on):
    gh = FakeGitHub()
    checker = use_checker(web, gh)
    first = checker.check(force=True)
    gh.answer = httpx.ConnectError("no network")
    state = checker.check(force=True)
    assert state["error"] == "offline"
    assert state["latest"] == first["latest"] and state["checked_at"] == first["checked_at"]
    assert state["pill"]  # the version found before is still worth showing


def test_no_release_yet_or_an_odd_tag_is_not_an_error(web, checks_on):
    checker = use_checker(web, FakeGitHub(httpx.Response(404, json={"message": "Not Found"})))
    state = checker.check(force=True)
    assert state["error"] is None and state["latest"] is None and state["checked_at"]
    checker = use_checker(web, FakeGitHub(release("nightly")))
    state = checker.check(force=True)
    assert state["error"] is None and state["latest"] is None and not state["available"]


# --- one notification per version ---------------------------------------------------------------------


def test_a_new_version_is_announced_once(web, checks_on):
    gh = FakeGitHub()
    checker = use_checker(web, gh)
    checker.check(force=True)
    checker.check(force=True)
    use_checker(web, gh).check(force=True)  # after a restart, too
    [note] = update_notifications(web)
    assert note["ns"] == "common" and note["params"] == {"version": "0.3.1"}
    assert note["link"] == "/ayarlar" and note["tone"] == "info"
    gh.answer = release("v0.3.2")
    checker = use_checker(web, gh)
    checker.check(force=True)
    assert [n["params"]["version"] for n in update_notifications(web)] == ["0.3.2", "0.3.1"]
    # A release pulled back to an older one is not announced again.
    gh.answer = release("v0.3.1")
    checker.check(force=True)
    assert len(update_notifications(web)) == 2


@pytest.mark.parametrize("tag", ["v0.3.0", "v0.2.9"])
def test_the_running_version_or_an_older_one_is_not_news(web, checks_on, tag):
    state = use_checker(web, FakeGitHub(release(tag))).check(force=True)
    assert state["latest"]["version"] == tag[1:]
    assert not state["available"] and not state["pill"]
    assert update_notifications(web) == []


# --- the endpoints ---------------------------------------------------------------------------------------


def test_the_state_shape(web, checks_on):
    use_checker(web, FakeGitHub())
    before = web.client.get("/api/update").json()
    assert set(before) == STATE_KEYS
    assert before["current"] == CURRENT and before["latest"] is None
    assert before["checked_at"] is None and before["pill"] is False
    after = web.client.post("/api/update/check").json()
    assert set(after) == STATE_KEYS and set(after["latest"]) == LATEST_KEYS
    assert after == web.client.get("/api/update").json()
    assert after["available"] and after["pill"] and not after["dismissed"]
    assert after["enabled"] and after["auto"] and not after["blocked"]


def test_every_change_is_pushed_to_open_tabs(web, checks_on):
    use_checker(web, FakeGitHub())
    events = read_events(
        web,
        lambda evs: any(topic == "update" for topic, _ in evs),
        after_connect=lambda: web.client.post("/api/update/check"),
    )
    pushed = next(data for topic, data in events if topic == "update")
    assert pushed["pill"] and pushed["latest"]["version"] == "0.3.1"


def test_dismiss_hides_the_pill_for_that_version_only(web, checks_on):
    gh = FakeGitHub()
    checker = use_checker(web, gh)
    checker.check(force=True)
    resp = web.client.post("/api/update/dismiss", json={"version": "0.3.1"})
    assert resp.status_code == 200
    assert resp.json()["dismissed"] and not resp.json()["pill"] and resp.json()["available"]
    # Remembered across a restart.
    checker = use_checker(web, gh)
    assert checker.state()["dismissed"]
    # A newer release shows it again.
    gh.answer = release("v0.3.2")
    state = checker.check(force=True)
    assert state["pill"] and not state["dismissed"]


@pytest.mark.parametrize("body", [{}, {"version": 3}, {"version": "latest"}, {"version": "0.3"}])
def test_dismiss_needs_a_version(web, body):
    resp = web.client.post("/api/update/dismiss", json=body)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "invalid"


def test_the_update_endpoints_need_the_session_and_the_header(web):
    with web.anonymous(**{"X-Etsyotomasyon": "1"}) as http:
        assert http.get("/api/update").status_code == 401
        resp = http.post("/api/update/dismiss", json={"version": "0.3.1"})
        assert resp.status_code == 401 and resp.json()["error"]["code"] == "no_session"
    with httpx.Client(base_url=web.url, trust_env=False, cookies=web.client.cookies) as http:
        for path, body in [("/api/update/dismiss", {"version": "0.3.1"}),
                           ("/api/update/check", {}), ("/api/update/auto", {"enabled": False})]:
            resp = http.post(path, json=body)
            assert resp.status_code == 403 and resp.json()["error"]["code"] == "forbidden", path
        resp = http.post("/api/update/dismiss", json={"version": "0.3.1"},
                         headers={"X-Etsyotomasyon": "1", "Origin": "http://evil.example"})
        assert resp.status_code == 403 and resp.json()["error"]["code"] == "forbidden_origin"
    assert web.ctx.updates.state()["dismissed"] is False
    assert settings.load_app_prefs().get("update_check") is None


def test_the_cache_lives_in_the_base_home_for_every_shop(web, checks_on):
    use_checker(web, FakeGitHub()).check(force=True)
    assert (Path(base_home()) / "update.json").is_file()
    web.client.post("/api/shops/add")
    # Another shop sees the same answer and is not told again.
    state = use_checker(web, FakeGitHub()).check(force=True)
    assert state["pill"] and update_notifications(web) == []

def test_the_pill_gives_way_before_the_page_title_is_cut():
    """At 1280 px "Profit & loss" was cut beside the pill even without its label: the
    shell drops the label first, then "What's new?" (Settings has it too)."""
    from etsyotomasyon.web.server import STATIC_DIR

    app = (Path(STATIC_DIR) / "js" / "app.js").read_text(encoding="utf-8")
    css = (Path(STATIC_DIR) / "css" / "base.css").read_text(encoding="utf-8")
    body = app[app.index("function fitUpdatePill()"):app.index("export function showReleaseNotes")]
    assert body.index('add("is-compact")') < body.index('add("is-tight")')
    assert ".update-pill.is-tight .update-pill-notes" in css
