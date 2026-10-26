"""Helpers for testing the web app. Import what you need in a test module:

    from web_helpers import FakeEtsy, use_fake_etsy, wait_for_job, read_events

The `web` fixture (registered in conftest.py) starts a real WebServer on a free
port, on a thread, inside the isolated ETSYOTOMASYON_HOME every test already gets:

    def test_something(web):
        resp = web.client.get("/api/status")           # session cookie + X-Etsyotomasyon sent
        assert resp.status_code == 200
        web.ctx                                         # the AppContext behind it
        web.url                                         # "http://127.0.0.1:<port>"

A fake Etsy for everything the context's EtsyClient sends:

    def test_connected(web):
        fake = use_fake_etsy(web)                       # keys + token saved, shop connected
        fake.add("GET", "/shops/12345678/listings", {"count": 0, "results": []})
        web.ctx.refresh_status(force=True)
        assert web.client.get("/api/status").json()["state"] == "connected"
        assert ("GET", "/shops/12345678") in fake.calls

    fake.offline = True                                 # every request: httpx.ConnectError

Waiting for a job started by an endpoint:

    job = web.client.post("/api/things/run").json()
    final = wait_for_job(web, job["id"])                # the full job once it has ended
    assert final["status"] == "done"

All values are invented: keystring KEY123, shop ExampleShop, Etsy shop id 12345678.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

import httpx
import pytest

from etsyotomasyon import auth
from etsyotomasyon.config import DEFAULT_SCOPES
from etsyotomasyon.desktop import settings
from etsyotomasyon.drop import workspace as workspace_mod
from etsyotomasyon.web.context import AppContext
from etsyotomasyon.web.server import WebServer

KEYSTRING = "KEY123"
SHARED_SECRET = "SECRET456"
SHOP_NAME = "ExampleShop"
ETSY_SHOP_ID = 12345678
USER_ID = 7654321
ACCESS_TOKEN = f"{USER_ID}.exampleaccesstoken"
REFRESH_TOKEN = f"{USER_ID}.examplerefreshtoken"
API_PREFIX = "/v3/application"

Responder = Any  # dict | list | httpx.Response | Callable[[httpx.Request], Any]


class WebHarness:
    """A running server plus a client that holds a session."""

    def __init__(
        self, *, check_status: bool = False, transport: Any = None, static_dir: Any = None
    ) -> None:
        self.desktop: Any = None
        self.token = "test-session-token"
        self.ctx = AppContext(token=self.token, transport=transport, check_status=check_status)
        self.server = WebServer(self.ctx, 0, static_dir=static_dir)
        self.port = self.server.port
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()
        # verify=False only skips building a TLS context (0.3 s each): this is plain http.
        self.client = httpx.Client(
            base_url=self.url, trust_env=False, verify=False, timeout=15.0,
            headers={"X-Etsyotomasyon": "1"},
        )
        resp = self.client.get(f"/?k={self.token}")
        assert resp.status_code == 302, resp.status_code
        assert "etsyotomasyon_session" in self.client.cookies

    def anonymous(self, **headers: str) -> httpx.Client:
        """A client with no session cookie and only the headers given (caller closes it)."""
        return httpx.Client(
            base_url=self.url, trust_env=False, verify=False, timeout=15.0, headers=headers
        )

    def close(self) -> None:
        self.client.close()
        self.server.stop()
        self.thread.join(5.0)
        self.server.server_close()
        self.ctx.close()


def start_web(**kwargs: Any) -> WebHarness:
    return WebHarness(**kwargs)


@pytest.fixture
def web(tmp_path, monkeypatch):
    """A running web app in the test's isolated home. See the module docstring.

    The default products folder ("Etsy Studio" on the Desktop) is redirected to
    `tmp_path / "Desktop"`, so no test ever reads or creates anything on the real
    Desktop. `web.desktop` is that folder.
    """
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    monkeypatch.setattr(workspace_mod, "desktop_dir", lambda: desktop)
    harness = start_web()
    harness.desktop = desktop
    try:
        yield harness
    finally:
        harness.close()


class FakeEtsy:
    """A small stand-in for Etsy's API, used through httpx.MockTransport.

    Paths are given without the /v3/application prefix. A responder is a dict or
    list (sent as 200 JSON), an httpx.Response, or a callable taking the
    httpx.Request and returning either. Unknown paths answer 404. `calls` records
    (method, path) of every request; `requests` keeps the httpx.Request objects.
    """

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Responder] = {}
        self.calls: list[tuple[str, str]] = []
        self.requests: list[httpx.Request] = []
        self.offline = False
        self._lock = threading.Lock()
        self.add("GET", "/openapi-ping", {"application_id": 1})
        self.add("GET", "/users/me", {"user_id": USER_ID, "shop_id": ETSY_SHOP_ID})
        self.add("GET", f"/users/{USER_ID}/shops", {"shop_id": ETSY_SHOP_ID, "shop_name": SHOP_NAME})
        self.add(
            "GET",
            f"/shops/{ETSY_SHOP_ID}",
            {"shop_id": ETSY_SHOP_ID, "shop_name": SHOP_NAME, "currency_code": "USD"},
        )

    def add(self, method: str, path: str, responder: Responder) -> FakeEtsy:
        self.routes[(method.upper(), path)] = responder
        return self

    def error(self, method: str, path: str, status: int, message: str = "error") -> FakeEtsy:
        """Make `path` answer with an Etsy-style error."""
        return self.add(method, path, httpx.Response(status, json={"error": message}))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith(API_PREFIX):
            path = path[len(API_PREFIX):]
        with self._lock:
            self.calls.append((request.method, path))
            self.requests.append(request)
        if self.offline:
            raise httpx.ConnectError("offline (FakeEtsy)", request=request)
        responder = self.routes.get((request.method, path))
        if responder is None:
            return httpx.Response(404, json={"error": f"FakeEtsy has no {request.method} {path}"})
        if callable(responder) and not isinstance(responder, httpx.Response):
            responder = responder(request)
        if isinstance(responder, httpx.Response):
            return responder
        return httpx.Response(200, json=responder)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def use_fake_etsy(
    web_or_ctx: WebHarness | AppContext,
    fake: FakeEtsy | None = None,
    *,
    keys: bool = True,
    connected: bool = True,
    token_expires_in: float = 3600.0,
) -> FakeEtsy:
    """Point the context's EtsyClient at a FakeEtsy, with keys and a sign-in saved.

    keys: write KEY123 / SECRET456 into the open shop's .env. connected: also save
    an OAuth token (not expiring for an hour by default).
    """
    ctx = web_or_ctx.ctx if isinstance(web_or_ctx, WebHarness) else web_or_ctx
    fake = fake or FakeEtsy()
    if keys:
        settings.save({
            "ETSY_KEYSTRING": KEYSTRING,
            "ETSY_SHARED_SECRET": SHARED_SECRET,
            "ETSY_REDIRECT_URI": settings.ETSY_REDIRECT_DEFAULT,
        })
    if connected:
        auth.save_token(auth.Token(
            access_token=ACCESS_TOKEN,
            refresh_token=REFRESH_TOKEN,
            expires_at=time.time() + token_expires_in,
            scopes=tuple(DEFAULT_SCOPES),
        ))
    ctx.transport = fake.transport()
    ctx.reset_client()
    return fake


def wait_for_job(web: WebHarness, job_id: str, timeout: float = 10.0) -> dict[str, Any]:
    """Poll GET /api/jobs/{id} until the job has ended; returns the full job."""
    deadline = time.monotonic() + timeout
    while True:
        job = web.client.get(f"/api/jobs/{job_id}").json()
        if job.get("status") not in ("queued", "running"):
            return job
        if time.monotonic() > deadline:
            raise AssertionError(f"job {job_id} still {job.get('status')} after {timeout}s")
        time.sleep(0.02)


def read_events(
    web: WebHarness,
    until: Callable[[list[tuple[str, Any]]], bool],
    *,
    timeout: float = 10.0,
    after_connect: Callable[[], Any] | None = None,
) -> list[tuple[str, Any]]:
    """Open /api/events and collect (topic, data) until `until(events)` is true.

    `after_connect` runs once the stream is open (after `hello`), e.g. to trigger
    the event under test.
    """
    events: list[tuple[str, Any]] = []
    started = time.monotonic()
    with httpx.Client(base_url=web.url, trust_env=False, verify=False, cookies=web.client.cookies,
                      timeout=httpx.Timeout(timeout, read=timeout)) as http:
        with http.stream("GET", "/api/events") as resp:
            assert resp.status_code == 200, resp.status_code
            topic, data = None, []
            triggered = False
            for line in resp.iter_lines():
                if line.startswith("event: "):
                    topic = line[len("event: "):]
                elif line.startswith("data: "):
                    data.append(line[len("data: "):])
                elif line == "" and topic is not None:
                    events.append((topic, json.loads("\n".join(data))))
                    topic, data = None, []
                    if not triggered and after_connect is not None and events[-1][0] == "hello":
                        triggered = True
                        after_connect()
                    if until(events):
                        return events
                if time.monotonic() - started > timeout:
                    break
    raise AssertionError(f"events so far: {events}")
