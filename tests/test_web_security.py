"""What stops other web pages (and other people) from using the local server.

A page on any site can make the browser send requests to localhost, so the
server trusts nothing it did not hand out itself: the Host header, a per-launch
session cookie, a custom header on writes, and the Origin all have to fit.
"""

from __future__ import annotations

import socket

import httpx
import pytest
from web_helpers import start_web

from etsyotomasyon.drop import workspace as workspace_mod
from etsyotomasyon.web.server import CSP

INDEX = "<!doctype html><title>etsyotomasyon test page</title>"
SECRET = "top secret, outside the static folder"


@pytest.fixture
def site(tmp_path, monkeypatch):
    """A server over a small static folder of our own, independent of the real UI."""
    static = tmp_path / "static"
    (static / "js" / "pages").mkdir(parents=True)
    (static / "css").mkdir()
    (static / "i18n").mkdir()
    (static / "index.html").write_text(INDEX, encoding="utf-8")
    (static / "js" / "app.js").write_text("export const a = 1;\n", encoding="utf-8")
    (static / "js" / "pages" / "panel.js").write_text("export default {};\n", encoding="utf-8")
    (static / "js" / "worker.mjs").write_text("export {};\n", encoding="utf-8")
    (static / "css" / "base.css").write_text("body{}\n", encoding="utf-8")
    (static / "i18n" / "common.json").write_text('{"tr":{},"en":{}}', encoding="utf-8")
    (static / "logo.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    (static / "font.woff2").write_bytes(b"wOF2")
    (tmp_path / "secret.txt").write_text(SECRET, encoding="utf-8")
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    monkeypatch.setattr(workspace_mod, "desktop_dir", lambda: desktop)
    harness = start_web(static_dir=static)
    try:
        yield harness
    finally:
        harness.close()


def raw(site, request: bytes) -> bytes:
    """Send bytes exactly as given (httpx would normalise paths and headers)."""
    with socket.create_connection(("127.0.0.1", site.port), timeout=10) as sock:
        sock.sendall(request)
        chunks = []
        while True:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            chunks.append(chunk)
            if b"\r\n\r\n" in b"".join(chunks) and b"Connection: close" not in b"".join(chunks):
                # keep-alive: stop once the body announced by Content-Length is in
                head, _, body = b"".join(chunks).partition(b"\r\n\r\n")
                length = [ln for ln in head.split(b"\r\n") if ln.lower().startswith(b"content-length:")]
                if length and len(body) >= int(length[0].split(b":")[1]):
                    break
    return b"".join(chunks)


def status_of(response: bytes) -> int:
    return int(response.split(b" ", 2)[1])


# --- Host header --------------------------------------------------------------------


@pytest.mark.parametrize("host", ["evil.example", "evil.example:{port}", "localhost:1",
                                  "127.0.0.1", "localhost.evil.example:{port}"])
def test_a_foreign_host_is_refused(site, host):
    host = host.format(port=site.port)
    with site.anonymous() as http:
        for path in ("/", "/api/ping", "/js/app.js"):
            resp = http.get(path, headers={"Host": host})
            assert resp.status_code == 403, (host, path)
    assert site.client.get("/api/session", headers={"Host": host}).status_code == 403


def test_both_local_names_are_accepted(site):
    for host in (f"localhost:{site.port}", f"127.0.0.1:{site.port}", f"LOCALHOST:{site.port}",
                 f"[::1]:{site.port}"):
        assert site.client.get("/api/session", headers={"Host": host}).status_code == 200


def test_a_request_without_a_host_is_refused(site):
    response = raw(site, b"GET / HTTP/1.0\r\n\r\n")
    assert status_of(response) == 403


# --- session ------------------------------------------------------------------------------


def test_the_api_needs_the_session_cookie(site):
    with site.anonymous() as http:
        resp = http.get("/api/session")
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "no_session"
        assert http.get("/api/status").status_code == 401
        assert http.get("/api/events").status_code == 401
        ping = http.get("/api/ping")
        assert ping.status_code == 200 and ping.json()["app"] == "etsyotomasyon"
        assert http.get("/").status_code == 200  # static files hold no secrets


def test_a_wrong_cookie_is_no_session(site):
    with site.anonymous() as http:
        resp = http.get("/api/session", headers={"Cookie": "etsyotomasyon_session=guess"})
        assert resp.status_code == 401
        resp = http.get("/api/session", headers={"Cookie": "etsyotomasyon_session"})
        assert resp.status_code == 401


def test_other_localhost_cookies_do_not_hide_the_session(site):
    """Cookies are per host, not per port: other local tools' cookies come along."""
    header = (f'tool="{{\\"a\\":1,b}}"; broken cookie; x=1,2; '
              f"etsyotomasyon_session={site.token}; theme=dark")
    with site.anonymous() as http:
        assert http.get("/api/session", headers={"Cookie": header}).status_code == 200


def test_a_wrong_k_sets_nothing(site):
    with site.anonymous() as http:
        resp = http.get("/?k=wrong")
        assert resp.status_code == 200 and "set-cookie" not in resp.headers
        assert http.get("/api/session").status_code == 401


def test_the_right_k_sets_the_cookie_and_hides_itself(site):
    with site.anonymous() as http:
        resp = http.get(f"/kurulum/magaza?x=1&k={site.token}")
        assert resp.status_code == 302
        assert resp.headers["location"] == "/kurulum/magaza?x=1"
        cookie = resp.headers["set-cookie"]
        assert cookie.startswith(f"etsyotomasyon_session={site.token};")
        for part in ("HttpOnly", "SameSite=Strict", "Path=/"):
            assert part in cookie
        assert http.get("/api/session").status_code == 200


def test_ping_names_the_instance_without_the_token(site):
    data = site.client.get("/api/ping").json()
    assert data["instance"] == site.ctx.instance and site.token not in str(data)


# --- writes -------------------------------------------------------------------------------


def test_a_write_needs_the_custom_header(site):
    with site.anonymous() as http:
        http.cookies = site.client.cookies
        resp = http.post("/api/prefs", json={"language": "en"})
        assert resp.status_code == 403 and resp.json()["error"]["code"] == "forbidden"
        resp = http.post("/api/prefs", json={"language": "en"}, headers={"X-Etsyotomasyon": "0"})
        assert resp.status_code == 403
        for method in ("PUT", "PATCH", "DELETE"):
            assert http.request(method, "/api/prefs").status_code == 403
    assert site.ctx.app_prefs().get("language") is None


def test_a_write_from_another_origin_is_refused(site):
    for origin in ("https://evil.example", "null", f"http://localhost:{site.port + 1}",
                   f"https://localhost:{site.port}"):
        resp = site.client.post("/api/prefs", json={"language": "en"}, headers={"Origin": origin})
        assert resp.status_code == 403, origin
        assert resp.json()["error"]["code"] == "forbidden_origin"
    for origin in (f"http://localhost:{site.port}", f"http://127.0.0.1:{site.port}"):
        resp = site.client.post("/api/prefs", json={"language": "en"}, headers={"Origin": origin})
        assert resp.status_code == 200, origin


def test_no_cors_preflight_is_ever_granted(site):
    resp = site.client.options("/api/prefs", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert resp.status_code in (403, 405)
    assert not any(h.lower().startswith("access-control-") for h in resp.headers)


def test_a_body_over_100_mb_is_refused_before_it_is_read(site):
    cookie = f"etsyotomasyon_session={site.token}"
    request = (
        f"PUT /api/prefs HTTP/1.1\r\nHost: 127.0.0.1:{site.port}\r\nCookie: {cookie}\r\n"
        f"X-Etsyotomasyon: 1\r\nContent-Length: {200 * 1024 * 1024}\r\n\r\n"
    ).encode()
    response = raw(site, request)
    assert status_of(response) == 413


def test_a_chunked_body_is_refused(site):
    cookie = f"etsyotomasyon_session={site.token}"
    request = (
        f"POST /api/prefs HTTP/1.1\r\nHost: 127.0.0.1:{site.port}\r\nCookie: {cookie}\r\n"
        "X-Etsyotomasyon: 1\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n"
    ).encode()
    assert status_of(raw(site, request)) == 411


# --- headers --------------------------------------------------------------------------------


def test_every_response_carries_the_security_headers(site):
    with site.anonymous() as http:
        responses = [
            site.client.get("/api/session"),
            site.client.get("/api/nope"),
            http.get("/"),
            http.get("/js/app.js"),
            http.get("/missing.js"),
            http.get("/api/session"),
            http.get("/", headers={"Host": "evil.example"}),
        ]
    for resp in responses:
        assert resp.headers["content-security-policy"] == CSP, resp.url
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["referrer-policy"] == "no-referrer"
        assert resp.headers["x-frame-options"] == "DENY"
        assert not any(h.lower().startswith("access-control-") for h in resp.headers)
    assert CSP == (
        "default-src 'self'; img-src 'self' data: blob: https:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'self'"
    )


def test_api_answers_are_never_cached_and_static_files_are_revalidated(site):
    assert site.client.get("/api/session").headers["cache-control"] == "no-store"
    assert site.client.get("/js/app.js").headers["cache-control"] == "no-cache"
    assert site.client.get("/").headers["cache-control"] == "no-cache"


def test_the_server_does_not_announce_python(site):
    server = site.client.get("/").headers.get("server", "")
    assert "Python" not in server


# --- static files ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    "/%2e%2e/secret.txt",
    "/..%2fsecret.txt",
    "/js/..%2f..%2fsecret.txt",
    "/js/%2e%2e/%2e%2e/secret.txt",
    "/..%5csecret.txt",
    "/js/..%5c..%5csecret.txt",
    "/C:%5cWindows%5cwin.ini",
    "/%00.js",
])
def test_nothing_outside_the_static_folder_is_served(site, path):
    resp = site.client.get(path)
    assert SECRET not in resp.text
    assert resp.status_code in (200, 404)
    if resp.status_code == 200:
        assert resp.text == INDEX  # only ever the app's own page


def test_a_raw_dot_dot_path_is_not_served(site):
    response = raw(site, f"GET /../secret.txt HTTP/1.0\r\nHost: 127.0.0.1:{site.port}\r\n\r\n".encode())
    assert SECRET.encode() not in response


def test_app_routes_fall_back_to_the_page(site):
    for path in ("/", "/panel", "/kurulum/magaza", "/ilanlar/1000001", "/css",
                 "/kurulum/mockuplar/tshirt-white.png", "/kurulum/mockuplar/mug%2Ecream.jpg"):
        resp = site.client.get(path)
        assert resp.status_code == 200 and resp.text == INDEX, path
        assert resp.headers["content-type"] == "text/html; charset=utf-8"


def test_a_missing_file_is_404_not_the_page(site):
    for path in ("/missing.js", "/js/missing.js", "/favicon.ico"):
        assert site.client.get(path).status_code == 404, path
    resp = site.client.get("/api/nope")
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("path, content_type", [
    ("/index.html", "text/html; charset=utf-8"),
    ("/js/app.js", "text/javascript; charset=utf-8"),
    ("/js/pages/panel.js", "text/javascript; charset=utf-8"),
    ("/js/worker.mjs", "text/javascript; charset=utf-8"),
    ("/css/base.css", "text/css; charset=utf-8"),
    ("/i18n/common.json", "application/json; charset=utf-8"),
    ("/logo.svg", "image/svg+xml"),
    ("/font.woff2", "font/woff2"),
])
def test_files_are_served_with_their_type(site, path, content_type):
    resp = site.client.get(path)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == content_type


def test_the_mime_map_does_not_come_from_the_registry(monkeypatch, site):
    import mimetypes

    monkeypatch.setattr(mimetypes, "guess_type", lambda *a, **k: ("text/plain", None))
    assert site.client.get("/js/app.js").headers["content-type"].startswith("text/javascript")


def test_head_sends_headers_only_and_writes_to_static_are_refused(site):
    head = site.client.head("/js/app.js")
    assert head.status_code == 200 and head.content == b""
    assert int(head.headers["content-length"]) > 0
    assert site.client.post("/index.html").status_code == 405


def test_the_real_static_folder_serves_javascript_as_javascript():
    """Whatever the UI ships, a .js file must never go out as text/plain."""
    from etsyotomasyon.web.files import content_type_for

    assert content_type_for("x.js") == "text/javascript; charset=utf-8"
    assert content_type_for("x.JS") == "text/javascript; charset=utf-8"
    assert content_type_for("x.unknown") == "application/octet-stream"


def test_keep_alive_serves_several_requests_on_one_connection(site):
    with httpx.Client(base_url=site.url, trust_env=False, verify=False) as http:
        for _ in range(5):
            assert http.get("/js/app.js").status_code == 200
