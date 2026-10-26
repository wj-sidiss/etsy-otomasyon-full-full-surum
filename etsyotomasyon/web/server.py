"""The local web server: static files, the JSON API, server-sent events.

Bound to 127.0.0.1 only. Anything that reaches it is still treated with suspicion,
because every web page the person has open can make their browser send requests
to localhost:

* Host must be localhost:PORT or 127.0.0.1:PORT (defeats DNS rebinding).
* /api/* needs the session cookie, which only the URL the app itself opened can
  set (`?k=<token>`, a new random token per launch). /api/ping is the exception.
* Writes (POST/PUT/PATCH/DELETE) also need `X-Etsyotomasyon: 1` — a header a foreign
  page cannot add without a CORS preflight, which is never answered — and a
  same-origin Origin header when one is sent.
* No CORS headers, ever; CSP, nosniff, no-referrer and DENY on every response.
"""

from __future__ import annotations

import hmac
import http.server
import logging
import socket
import socketserver
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

from . import errors as errors_mod
from . import events as events_mod
from .context import SWITCH_WAIT, AppContext
from .errors import to_api_error
from .files import MIME, content_type_for, resolve_inside
from .router import ApiError, Request, Response, Router

log = logging.getLogger("etsyotomasyon.web")

STATIC_DIR = Path(__file__).resolve().parent / "static"
COOKIE = "etsyotomasyon_session"
MAX_BODY = 100 * 1024 * 1024
# A refused request's body up to this size is read and dropped; above it, lingering close.
DRAIN_LIMIT = 1024 * 1024
LINGER_SECONDS = 3.0
HEARTBEAT = 15.0
CSP = (
    "default-src 'self'; img-src 'self' data: blob: https:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
    "form-action 'self'"
)
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": CSP,
}
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
# App routes whose last segment may be a file name (the mockup editor is
# /kurulum/mockuplar/<file name>): these always get the page, dot or not. No static
# file lives under them.
SPA_PREFIXES = ("kurulum/", "ilanlar/")


class WebServer(http.server.ThreadingHTTPServer):
    """One thread per connection; never looks up its own host name.

    Listens on 127.0.0.1 (or, with family=AF_INET6, on ::1 — the launcher adds that
    second listener on the same port, because "localhost" resolves to ::1 first and
    a browser would otherwise wait for that attempt to fail on every connection).
    """

    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second program bind a port that is in use, so a
    # busy port would look free. Elsewhere it only skips TIME_WAIT, which is harmless.
    allow_reuse_address = sys.platform != "win32"

    def __init__(
        self,
        ctx: AppContext,
        port: int = 0,
        *,
        static_dir: Path | None = None,
        router: Router | None = None,
        family: int = socket.AF_INET,
    ) -> None:
        from .api import register_all

        self.ctx = ctx
        self.static_dir = Path(static_dir or STATIC_DIR)
        if router is None:
            router = Router()
            register_all(router, ctx)
        self.router = router
        self.stopping = threading.Event()
        self.companion: WebServer | None = None  # the ::1 listener, if any
        self._serving = threading.Event()
        self._state_lock = threading.Lock()
        self.address_family = family
        host = "::1" if family == socket.AF_INET6 else "127.0.0.1"
        super().__init__((host, port), Handler)
        self.port = int(self.server_address[1])
        ctx.port = self.port

    def server_bind(self) -> None:
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            # Nobody else may bind this port while we hold it.
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        if self.address_family == socket.AF_INET6 and hasattr(socket, "IPV6_V6ONLY"):
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def allowed_hosts(self) -> set[str]:
        return {f"localhost:{self.port}", f"127.0.0.1:{self.port}", f"[::1]:{self.port}"}

    def allowed_origins(self) -> set[str]:
        return {f"http://{host}" for host in self.allowed_hosts()}

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        with self._state_lock:
            if self.stopping.is_set():
                return
            self._serving.set()
        super().serve_forever(poll_interval)

    def stop_serving(self) -> None:
        """Stop this listener's serve_forever; safe before it started and when repeated."""
        with self._state_lock:
            self.stopping.set()
            serving = self._serving.is_set()
            self._serving.clear()
        if serving:
            self.shutdown()

    def stop(self) -> None:
        """End SSE streams and stop serving, on both listeners (call from another thread)."""
        self.stopping.set()
        self.ctx.events.close()
        if self.companion is not None:
            self.companion.stop_serving()
        self.stop_serving()

    def server_close(self) -> None:
        if self.companion is not None:
            self.companion.server_close()
        super().server_close()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # The default prints a traceback to sys.stderr, which a windowed app lacks.
        log.debug("connection error from %s", client_address, exc_info=True)


class Handler(http.server.BaseHTTPRequestHandler):
    server: WebServer
    protocol_version = "HTTP/1.1"
    timeout = 30
    server_version = "etsyotomasyon"
    sys_version = ""
    _body_done = False
    _linger = False

    # --- plumbing -------------------------------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Silent: a windowed app has no stderr, and requests are not worth logging."""

    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def do_PUT(self) -> None:  # noqa: N802
        self._handle()

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle()

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle()

    def do_OPTIONS(self) -> None:  # noqa: N802
        # No CORS, so no preflight is ever answered with permission.
        self._body_done = False
        self._reject(405, "method_not_allowed", "OPTIONS is not supported", api=True)

    # --- the request ------------------------------------------------------------------

    def _handle(self) -> None:
        try:
            self._dispatch()
        except (ConnectionError, TimeoutError, OSError):
            self.close_connection = True
        except Exception:  # noqa: BLE001 — never let a bug kill the connection thread noisily
            log.exception("request failed")
            try:
                self._send(Response.error(to_api_error(RuntimeError("internal"))), api=True)
            except OSError:
                self.close_connection = True

    def _dispatch(self) -> None:
        self._body_done = False
        ctx = self.server.ctx
        split = urllib.parse.urlsplit(self.path)
        raw_path = split.path or "/"
        is_api = raw_path == "/api" or raw_path.startswith("/api/")
        method = self.command.upper()

        host = (self.headers.get("Host") or "").strip().lower()
        if host not in self.server.allowed_hosts():
            self._reject(403, "forbidden_host", "This address is not served here.", api=is_api)
            return

        query_pairs = urllib.parse.parse_qsl(split.query, keep_blank_values=True)
        if method == "GET" and any(k == "k" for k, _ in query_pairs):
            given = [v for k, v in query_pairs if k == "k"][-1]
            if ctx.token and hmac.compare_digest(given.encode(), ctx.token.encode()):
                rest = urllib.parse.urlencode([(k, v) for k, v in query_pairs if k != "k"])
                location = raw_path + (f"?{rest}" if rest else "")
                response = Response.redirect(location)
                response.headers["Set-Cookie"] = (
                    f"{COOKIE}={ctx.token}; HttpOnly; SameSite=Strict; Path=/"
                )
                self._discard_body()
                self._send(response, api=is_api)
                return

        if is_api and raw_path != "/api/ping" and not self._has_session():
            self._reject(401, "no_session", "Open etsyotomasyon from the app to use it.", api=True)
            return

        if method in MUTATING:
            if (self.headers.get("X-Etsyotomasyon") or "").strip() != "1":
                self._reject(403, "forbidden", "Missing the X-Etsyotomasyon header.", api=is_api)
                return
            origin = self.headers.get("Origin")
            if origin is not None and origin.strip().lower() not in self.server.allowed_origins():
                self._reject(403, "forbidden_origin", "Cross-origin requests are refused.", api=is_api)
                return

        body = self._read_body()
        if body is None:
            return

        if is_api:
            if raw_path == "/api/events" and method == "GET":
                self._events()
                return
            self._api(method, raw_path, split.query, body)
            return
        if method not in ("GET", "HEAD"):
            self._reject(405, "method_not_allowed", f"{method} is not allowed here", api=False)
            return
        self._static(raw_path)

    def _has_session(self) -> bool:
        """The session cookie is present and right.

        Parsed by hand: cookies are per host, not per port, so the browser also sends
        whatever other localhost tools have set, and http.cookies gives up on the
        whole header at the first value it does not like.
        """
        token = self.server.ctx.token
        if not token:
            return False
        for header in self.headers.get_all("Cookie") or []:
            for part in header.split(";"):
                name, sep, value = part.strip().partition("=")
                if sep and name.strip() == COOKIE and hmac.compare_digest(
                    value.strip().strip('"').encode(), token.encode()
                ):
                    return True
        return False

    def _content_length(self) -> int | None:
        """The declared body size; None when it is chunked or malformed."""
        if (self.headers.get("Transfer-Encoding") or "").strip():
            return None
        raw = (self.headers.get("Content-Length") or "0").strip() or "0"
        try:
            length = int(raw)
        except ValueError:
            return None
        return length if length >= 0 else None

    def _read_body(self) -> bytes | None:
        """Exactly Content-Length bytes; None when the request was refused."""
        if (self.headers.get("Transfer-Encoding") or "").strip():
            self._reject(411, "length_required", "Send a Content-Length.", api=True)
            return None
        length = self._content_length()
        if length is None:
            self._reject(400, "bad_request", "Bad Content-Length.", api=True)
            return None
        if length > MAX_BODY:
            self._reject(413, "too_large", "The upload is larger than 100 MB.", api=True,
                         limit=MAX_BODY)
            return None
        self._body_done = True
        chunks: list[bytes] = []
        left = length
        while left > 0:
            chunk = self.rfile.read(min(left, 1024 * 1024))
            if not chunk:
                self.close_connection = True
                return None
            chunks.append(chunk)
            left -= len(chunk)
        return b"".join(chunks)

    def _discard_body(self) -> None:
        """Consume a body we will not use, so the answer is not lost to a TCP reset.

        Closing a socket that still holds unread bytes makes the OS send a reset,
        and the client may then lose the response it was about to read. A small
        body is read and dropped (the connection stays usable); a large or
        unmeasurable one is left to a lingering close (see finish()).
        """
        if self._body_done:
            return
        self._body_done = True
        length = self._content_length()
        if length is not None and length <= DRAIN_LIMIT:
            left = length
            while left > 0:
                chunk = self.rfile.read(min(left, 64 * 1024))
                if not chunk:
                    break
                left -= len(chunk)
            return
        self.close_connection = True
        self._linger = True

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            if self._linger:
                self._lingering_close()

    def _lingering_close(self) -> None:
        """Stop sending, then read and drop what the client still sends, for a moment."""
        try:
            self.connection.shutdown(socket.SHUT_WR)
            self.connection.settimeout(0.5)
            deadline = time.monotonic() + LINGER_SECONDS
            received = 0
            while time.monotonic() < deadline and received < 2 * MAX_BODY:
                chunk = self.connection.recv(256 * 1024)
                if not chunk:
                    break
                received += len(chunk)
        except OSError:
            pass

    # --- API ------------------------------------------------------------------------------

    def _api(self, method: str, raw_path: str, raw_query: str, body: bytes) -> None:
        ctx = self.server.ctx
        # An answer saying "reconnect" / "offline" / "bad_keys" (sent, or shown inside
        # a page's answer) makes the context re-check the shop's status soon.
        with errors_mod.serving(ctx):
            response = self._api_response(ctx, method, raw_path, raw_query, body)
        self._send(response, api=True)

    def _api_response(
        self, ctx: AppContext, method: str, raw_path: str, raw_query: str, body: bytes
    ) -> Response:
        query = dict(urllib.parse.parse_qsl(raw_query, keep_blank_values=True))
        try:
            route, params = self.server.router.match(method, raw_path)
            request = Request(
                method=method,
                path=urllib.parse.unquote(raw_path),
                query=query,
                headers=self.headers,
                body=body,
                params=params,
                ctx=ctx,
                raw_query=raw_query,
            )
            if route.exclusive:
                # Refuse at once while a job is queued or running rather than wait
                # behind it; the lock itself gives up after SWITCH_WAIT seconds.
                if ctx.jobs.busy():
                    raise ApiError(409, "busy", "Wait for the running task to finish first.")
                with ctx.shop_lock.write(timeout=SWITCH_WAIT):
                    result = route.handler(request)
            else:
                with ctx.shop_lock.read():
                    result = route.handler(request)
            response = result if isinstance(result, Response) else Response.json(
                {} if result is None else result
            )
        except BaseException as exc:  # noqa: BLE001
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            response = Response.error(to_api_error(exc))
        return response

    def _events(self) -> None:
        """GET /api/events: hello, the current status, then whatever is published."""
        ctx = self.server.ctx
        sub = ctx.events.subscribe()
        try:
            self.connection.settimeout(None)  # a stream is idle between events by design
            self.send_response(200)
            self._common_headers(api=True)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self._write(events_mod.encode("hello", {"instance": ctx.instance, "version": ctx.version}))
            self._write(events_mod.encode("status", ctx.public_status()))
            while not self.server.stopping.is_set():
                try:
                    frame = sub.get(timeout=HEARTBEAT)
                except EOFError:
                    break
                self._write(frame if frame is not None else ": ping\n\n")
        except (OSError, ValueError):
            pass
        finally:
            sub.close()

    def _write(self, text: str) -> None:
        self.wfile.write(text.encode("utf-8"))
        self.wfile.flush()

    # --- static --------------------------------------------------------------------------

    def _static(self, raw_path: str) -> None:
        root = self.server.static_dir
        rel = urllib.parse.unquote(raw_path).lstrip("/")
        target = resolve_inside(root, rel, follow_links=False) if rel else None
        if target is not None and target.is_file():
            self._send_static_file(target)
            return
        last = rel.rsplit("/", 1)[-1]
        if "." in last and not rel.startswith(SPA_PREFIXES):
            self._send(Response.text("Not found", 404), api=False)
            return
        index = root / "index.html"
        if index.is_file():
            self._send_static_file(index)
        else:
            self._send(Response.text("etsyotomasyon: the web files are missing from this build.", 404),
                       api=False)

    def _send_static_file(self, path: Path) -> None:
        self._send(Response.file(path, content_type_for(path)), api=False)

    # --- responses ---------------------------------------------------------------------

    def _reject(self, status: int, code: str, message: str, *, api: bool, **params: Any) -> None:
        # A refused request's body may still be on the wire: consume it (or linger
        # on close) so it is neither read as the next request nor turned into a reset.
        self._discard_body()
        self.close_connection = True
        self._send(Response.error(ApiError(status, code, message, **params)), api=api)

    def _common_headers(self, *, api: bool) -> None:
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)

    def _send(self, response: Response, *, api: bool) -> None:
        body = response.body
        size = len(body)
        handle = None
        if response.file_path is not None:
            try:
                handle = open(response.file_path, "rb")  # noqa: SIM115 — closed below
                size = Path(response.file_path).stat().st_size
            except OSError:
                if handle is not None:
                    handle.close()
                handle = None
                response = Response.error(ApiError(404, "not_found", "Not found."))
                body, size = response.body, len(response.body)
        try:
            self.send_response(response.status)
            self._common_headers(api=api)
            headers = dict(response.headers)
            headers.setdefault("Cache-Control", "no-store" if api else "no-cache")
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(size))
            for name, value in headers.items():
                self.send_header(name, value)
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            if self.command == "HEAD" or response.status in (204, 304):
                return
            if handle is not None:
                while True:
                    chunk = handle.read(256 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            else:
                self.wfile.write(body)
        finally:
            if handle is not None:
                handle.close()


__all__ = ["WebServer", "Handler", "STATIC_DIR", "COOKIE", "CSP", "MIME"]
