"""OAuth 2.0 Authorization Code + PKCE against Etsy.

Etsy treats API clients as public clients: there is no client secret in the token
exchange, the PKCE verifier proves possession instead.

CALLBACK RULES, AND WHY THERE ARE TWO FLOWS
-------------------------------------------
Etsy's app settings screen states the callback rules directly:

    Must start with http:// or https://
    Host must be a domain name (e.g. example.com)
    IP addresses are not allowed (e.g. 127.0.0.1)

So plain ``http`` is allowed, and ``localhost`` is allowed because it is a domain
name rather than an IP — but ``127.0.0.1`` is not. (Etsy's prose documentation says
the callback "must implement TLS and use an https:// prefix"; the app settings screen
and the values it actually accepts are the narrower, authoritative truth. Read that
sentence too literally and you will build the wrong flow.)

That gives two paths, chosen automatically:

  * **localhost callback** — etsyotomasyon runs a one-shot listener on the port in your
    redirect URI and catches the code itself. Nothing to copy.
  * **anything else** — Etsy redirects the browser to your registered URL, which does
    not need to exist or serve anything, and you paste the address back in once.

Either way we then exchange code + verifier for an access token (1 hour) and a
refresh token (90 days). ``--listen PORT`` forces the listener (useful behind a
tunnel), ``--paste`` forces the manual path.

Tokens land in ~/.etsyotomasyon/token.json and are refreshed transparently by the client.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import http.server
import ipaddress
import secrets
import socket
import socketserver
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from .config import (
    OAUTH_AUTHORIZE_URL,
    OAUTH_TOKEN_URLS,
    Config,
    read_json,
    token_path,
    write_json_private,
)
from .errors import AuthError, AuthUnreachable

# Refresh this many seconds before the token actually expires, so a long-running
# batch never dies mid-flight on a token that lapsed between check and request.
EXPIRY_SKEW = 120


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


@dataclass
class Token:
    access_token: str
    refresh_token: str
    expires_at: float
    scopes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def user_id(self) -> int | None:
        """Etsy access tokens are '<user_id>.<random>', so the owner is readable offline."""
        head = self.access_token.split(".", 1)[0]
        return int(head) if head.isdigit() else None

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at - EXPIRY_SKEW

    @property
    def seconds_left(self) -> int:
        return max(0, int(self.expires_at - time.time()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "scopes": list(self.scopes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Token:
        return cls(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=float(data["expires_at"]),
            scopes=tuple(data.get("scopes", ())),
        )

    @classmethod
    def from_response(cls, payload: dict[str, Any], scopes: tuple[str, ...]) -> Token:
        # Prefer what Etsy says it GRANTED over what we asked for. They can differ,
        # and when they do it is the whole explanation for a later 403 — so storing
        # the request would make `auth status` lie exactly when it is consulted.
        granted = tuple(str(payload["scope"]).split()) if payload.get("scope") else scopes
        try:
            return cls(
                access_token=payload["access_token"],
                refresh_token=payload["refresh_token"],
                expires_at=time.time() + float(payload.get("expires_in", 3600)),
                scopes=granted,
            )
        except KeyError as exc:
            raise AuthError(f"Token response missing {exc}. Got: {payload}") from exc

    def missing_scopes(self, wanted: tuple[str, ...]) -> tuple[str, ...]:
        held = set(self.scopes)
        return tuple(s for s in wanted if s not in held)


def load_token() -> Token | None:
    data = read_json(token_path())
    return Token.from_dict(data) if data else None


def save_token(token: Token) -> None:
    write_json_private(token_path(), token.to_dict())


def clear_token() -> bool:
    path = token_path()
    if path.exists():
        path.unlink()
        return True
    return False


def _post_token(form: dict[str, str]) -> dict[str, Any]:
    """POST to the token endpoint, falling back to the alternate host on transport errors."""
    last: Exception | None = None
    for url in OAUTH_TOKEN_URLS:
        try:
            resp = httpx.post(url, data=form, timeout=30.0)
            # Some apps get a 403 "should be in the format 'keystring:shared_secret'"
            # for a form-encoded token request that succeeds, unchanged, as JSON
            # (etsy/open-api#1678, 2026). Etsy accepts both bodies, so retry once as
            # JSON before reporting a rejection the seller cannot fix.
            if resp.status_code == 403 and "keystring" in resp.text.lower():
                resp = httpx.post(url, json=form, timeout=30.0)
        except httpx.HTTPError as exc:
            last = exc
            continue
        if resp.status_code == 200:
            return resp.json()
        # A 4xx is a real answer (bad code, bad client_id) — do not retry the other host.
        if 400 <= resp.status_code < 500:
            raise AuthError(f"Token endpoint rejected the request ({resp.status_code}): {resp.text}")
        last = AuthError(f"{url} returned {resp.status_code}: {resp.text}")
    raise AuthUnreachable(f"Could not reach any Etsy token endpoint. Last error: {last}")


def refresh(token: Token, config: Config) -> Token:
    payload = _post_token(
        {
            "grant_type": "refresh_token",
            "client_id": config.keystring,
            "refresh_token": token.refresh_token,
        }
    )
    fresh = Token.from_response(payload, token.scopes or config.scopes)
    save_token(fresh)
    return fresh


# --- the authorization request -------------------------------------------------


@dataclass
class AuthRequest:
    url: str
    verifier: str
    state: str


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def validate_redirect_uri(uri: str) -> None:
    """Apply Etsy's actual callback rules, quoted from its app settings screen:

        Must start with http:// or https://
        Host must be a domain name (e.g. example.com)
        IP addresses are not allowed (e.g. 127.0.0.1)

    So http is fine and ``localhost`` is fine — it is a domain name, not an IP —
    which means a loopback listener does work. ``127.0.0.1`` does not.
    """
    if not uri:
        raise AuthError(
            "ETSY_REDIRECT_URI is not set.\n"
            "Register a callback URL on your Etsy app and put the identical string here.\n"
            "http:// and https:// are both accepted; the host must be a domain name, and\n"
            "IP addresses are rejected — use 'localhost', never '127.0.0.1'.\n"
            "For a local flow etsyotomasyon can catch automatically:\n"
            "  ETSY_REDIRECT_URI=http://localhost:3003/oauth/redirect"
        )
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme not in ("http", "https"):
        raise AuthError(
            f"ETSY_REDIRECT_URI must start with http:// or https:// — got {uri!r}."
        )
    host = parsed.hostname or ""
    if not host:
        raise AuthError(f"ETSY_REDIRECT_URI is not a full URL: {uri!r}")
    if _is_ip_literal(host):
        raise AuthError(
            f"Etsy does not accept IP addresses in a callback URL — got {host!r}.\n"
            "Use a domain name. 'localhost' is accepted; '127.0.0.1' is not."
        )


def is_loopback(uri: str) -> bool:
    """True when etsyotomasyon can serve the callback itself on this machine.

    Only for plain http: the listener speaks no TLS, so a browser sent to
    https://localhost would fail its handshake and the code would never arrive.
    An https callback uses the paste flow instead — the page does not load, and
    the code is read from the address bar.
    """
    parsed = urllib.parse.urlparse(uri)
    return parsed.scheme == "http" and (parsed.hostname or "").lower() == "localhost"


def build_authorization_url(config: Config) -> AuthRequest:
    validate_redirect_uri(config.redirect_uri)
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "redirect_uri": config.redirect_uri,
            "scope": " ".join(config.scopes),
            "client_id": config.keystring,
            "state": state,
            # Etsy accepts no other challenge method.
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return AuthRequest(f"{OAUTH_AUTHORIZE_URL}?{query}", verifier, state)


def extract_code(text: str, *, expected_state: str | None = None) -> str:
    """Pull the authorization code out of whatever the user pasted.

    Accepts the full redirected URL, a bare query string, or the code on its own.
    When a state parameter is present it must match, which is the CSRF check.
    """
    raw = (text or "").strip().strip('"').strip("'")
    if not raw:
        raise AuthError("Nothing pasted.")

    params: dict[str, list[str]] = {}
    if "?" in raw or "&" in raw or "=" in raw:
        query = raw.split("?", 1)[1] if "?" in raw else raw
        params = urllib.parse.parse_qs(query)

    if "error" in params:
        detail = params.get("error_description", [""])[0]
        raise AuthError(f"Etsy returned an error: {params['error'][0]} {detail}".strip())

    if params.get("code"):
        code = params["code"][0]
        got_state = params.get("state", [None])[0]
        if expected_state and got_state and got_state != expected_state:
            raise AuthError(
                "The state value does not match the request we started. "
                "Do not use this code — run `etsyotomasyon auth login` again."
            )
        return code

    if params:
        raise AuthError("That URL has no `code=` parameter. Copy the full address after Etsy redirects you.")

    # A bare code. Etsy's codes have no spaces; a stray sentence is a paste mistake.
    if " " in raw:
        raise AuthError("That does not look like an authorization code. Paste the redirected URL instead.")
    return raw


def exchange_code(config: Config, code: str, verifier: str) -> Token:
    payload = _post_token(
        {
            "grant_type": "authorization_code",
            "client_id": config.keystring,
            "redirect_uri": config.redirect_uri,
            "code": code,
            "code_verifier": verifier,
        }
    )
    token = Token.from_response(payload, config.scopes)
    save_token(token)
    return token


# --- optional automatic capture, for people with an https tunnel ---------------


class LoopbackServer(http.server.HTTPServer):
    """An HTTPServer that binds without looking up its own host name.

    HTTPServer.server_bind calls socket.getfqdn, a reverse DNS lookup that on
    some Macs stalls ~30s before the port even opens — and Cancel with it.
    """

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def state_matches(got: str | None, expected: str | None) -> bool:
    """The listener's CSRF check: None expects nothing (a handler used on its own)."""
    if expected is None:
        return True
    return hmac.compare_digest((got or "").encode("utf-8"), expected.encode("utf-8"))


# Shown for a request that does not carry the state of the consent request being
# waited for: a link or <img> on any web page can reach localhost, and such a request
# must neither end the wait nor look like a success. Nothing of it is echoed.
FOREIGN_TITLE = "Not from etsyotomasyon · etsyotomasyon'ten değil"
FOREIGN_DETAIL = (
    "This address was not opened by the connection etsyotomasyon is waiting for, so it was "
    "ignored. Go back to etsyotomasyon.<br>Bu adres etsyotomasyon'in beklediği bağlantıdan gelmedi, "
    "yok sayıldı. etsyotomasyon'e dönün."
)


def listener_page(title: str, detail: str) -> bytes:
    """The listener's small bilingual page (detail is HTML: escape anything received)."""
    return f"""<!doctype html><meta charset="utf-8"><title>{title}</title>
<div style="font:16px/1.6 system-ui,sans-serif;max-width:34rem;margin:14vh auto;padding:0 1.5rem">
<h1 style="font-size:1.4rem;margin:0 0 .5rem">{title}</h1>
<p style="color:#555;margin:0">{detail}</p></div>""".encode()


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Serves the single request an https tunnel forwards to this machine."""

    result: dict[str, str] = {}
    expected_path = "/"
    # The state of the consent request being waited for (_capture_via_listener sets
    # it). Only a request carrying it is recorded, and only then is the browser sent
    # on: anything else is refused and the wait goes on, so a forged request can
    # neither end the flow nor fake a success. None records any request.
    expected_state: str | None = None
    # Where to send the browser once Etsy has handed over a code, instead of showing
    # the receipt page below: the web app sets its own http://localhost:<port>/oauth-done
    # so the consent tab lands back in etsyotomasyon. None (the CLI) keeps the page. The
    # code itself never travels further than this listener.
    return_url: str | None = None
    # Browsers open speculative connections to localhost and send nothing. The
    # server is single-threaded, so without a per-connection timeout one such
    # socket blocks serve_forever, and shutdown() — Cancel — waits on it.
    timeout = 2

    def _send_page(self, status: int, title: str, detail: str) -> None:
        encoded = listener_page(title, detail)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802 — name fixed by BaseHTTPRequestHandler
        parsed = urllib.parse.urlparse(self.path)
        if self.expected_path not in ("", "/") and parsed.path != self.expected_path:
            self.send_response(404)
            self.end_headers()
            return

        params = urllib.parse.parse_qs(parsed.query)
        if not state_matches(params.get("state", [None])[0], type(self).expected_state):
            self._send_page(400, FOREIGN_TITLE, FOREIGN_DETAIL)
            return
        if "code" in params or "error" in params:  # an answer; a bare visit is not one
            for key in ("code", "state", "error", "error_description"):
                if key in params:
                    type(self).result[key] = params[key][0]

        ok = "code" in params and "error" not in params
        # Only a success goes back to the app: an error keeps this listener's own page.
        if ok and self.return_url:
            self.send_response(302)
            self.send_header("Location", self.return_url)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            return
        # Shown to whoever just clicked "Allow" — most likely in the desktop app,
        # and as likely Turkish as English — so it says both, and nothing about a
        # terminal. Etsy's error text is echoed, so it is escaped.
        if ok:
            # Receipt, not success: the state check and the token exchange happen
            # after this page is sent, and either can still fail.
            title = "Etsy answered · Etsy yanıt verdi"
            detail = (
                "Go back to etsyotomasyon to see that the connection finished.<br>"
                "Bağlantının tamamlandığını görmek için etsyotomasyon'e dön."
            )
        else:
            title = "Not connected · Bağlanamadı"
            detail = html.escape(
                params.get("error_description", params.get("error", ["Unknown error"]))[0]
            )
        self._send_page(200 if ok else 400, title, detail)

    def log_message(self, *_args: Any) -> None:
        """Silence the default stderr access log — it would clutter CLI output."""


def _capture_via_listener(
    request: AuthRequest,
    config: Config,
    port: int,
    timeout: float,
    cancel: threading.Event | None = None,
) -> str:
    parsed = urllib.parse.urlparse(config.redirect_uri)
    _CallbackHandler.result = {}
    _CallbackHandler.expected_path = parsed.path or "/"
    try:
        server = LoopbackServer(("127.0.0.1", port), _CallbackHandler)
    except OSError as exc:
        raise AuthError(f"Cannot listen on 127.0.0.1:{port} ({exc}). Free the port or pick another.") from exc

    # Only the answer to this request counts; a forged one is refused and ignored.
    _CallbackHandler.expected_state = request.state
    server.socket.settimeout(1.0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.4}, daemon=True)
    thread.start()
    print(f"Listening on port {port} for the redirect to {config.redirect_uri} ...", flush=True)

    deadline = time.time() + timeout
    try:
        while time.time() < deadline and not _CallbackHandler.result:
            if cancel is not None and cancel.is_set():
                break
            time.sleep(0.25)
    finally:
        server.shutdown()
        server.server_close()
        _CallbackHandler.expected_state = None

    result = dict(_CallbackHandler.result)
    if not result and cancel is not None and cancel.is_set():
        raise AuthError("Cancelled before Etsy sent the browser back. Nothing was changed.")
    if not result:
        raise AuthError(f"Timed out after {int(timeout)}s waiting for the redirect.")
    if "error" in result:
        raise AuthError(
            f"Etsy returned an error: {result['error']} {result.get('error_description', '')}".strip()
        )
    if result.get("state") != request.state:
        raise AuthError("State mismatch — the redirect did not come from the request we started.")
    return result["code"]


# --- the flow ------------------------------------------------------------------


def login(
    config: Config,
    *,
    open_browser: bool = True,
    listen_port: int | None = None,
    paste: bool = False,
    timeout: float = 300.0,
    prompt: Callable[[str], str] = input,
    cancel: threading.Event | None = None,
) -> Token:
    """Run the consent flow and persist the resulting token.

    `cancel` lets a caller that cannot press Ctrl+C — the desktop window — stop
    waiting for the browser; the listener is closed and nothing is stored.
    """
    request = build_authorization_url(config)

    # A localhost callback can be served right here, so do that unless told otherwise.
    if listen_port is None and not paste and is_loopback(config.redirect_uri):
        parsed = urllib.parse.urlparse(config.redirect_uri)
        listen_port = parsed.port or (443 if parsed.scheme == "https" else 80)

    # flush=True matters: Python buffers stdout when it is not a terminal, so a piped
    # or redirected `auth login` would print the URL only after the flow finished —
    # by which point the URL you needed to open is useless.
    print("Open this URL to authorise etsyotomasyon with your Etsy account:\n", flush=True)
    print(f"  {request.url}\n", flush=True)
    if open_browser:
        try:
            webbrowser.open(request.url)
        except (webbrowser.Error, OSError):
            pass  # Headless box: the printed URL above is the fallback.

    if listen_port:
        code = _capture_via_listener(request, config, listen_port, timeout, cancel)
    else:
        print(
            f"Etsy will send your browser to {config.redirect_uri}\n"
            "That page does not need to load — the code is in the address bar.\n"
        )
        pasted = prompt("Paste the full address you were redirected to (or just the code): ")
        code = extract_code(pasted, expected_state=request.state)

    return exchange_code(config, code, request.verifier)


def port_is_free(port: int) -> bool:
    """Pre-flight check for --listen, so we fail fast with a clear message."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) != 0
