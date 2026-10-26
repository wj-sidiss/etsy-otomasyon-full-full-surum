"""Mağaza Bağlantısı: the Etsy app keys, connecting the shop, disconnecting it.

    GET  /api/connect/info        what is saved (never the secret) and what Etsy granted
    POST /api/connect/keys        save keystring + shared secret (+ callback), ask Etsy
    POST /api/connect/callback-confirmed
                                  "I added the callback to my Etsy app" (asked once per shop)
    GET  /api/connect/preflight   keys, callback and callback port, before Etsy is opened
    POST /api/connect/start       the same checks, then the callback listener and the job
    POST /api/connect/disconnect  forget this computer's sign-in (token.json)
    GET  /api/connect/counts      the connected card's shop row: active listings and
                                  orders to ship (two count requests, cached a minute)

The consent happens on Etsy's own page, in a tab the browser opened for it. Etsy sends
that tab to the one-shot callback listener (by default
http://localhost:3003/oauth/redirect), which hands the code to the job and sends the
tab on to /oauth-done in the app. The code never leaves the listener.
"""

from __future__ import annotations

import logging
import re
import socket
import threading
import time
import urllib.parse
from typing import TYPE_CHECKING, Any

from ... import auth, shops
from ...config import DEFAULT_SCOPES, Config, split_credential
from ...desktop import settings
from ...errors import AuthError, AuthUnreachable, ConfigError, EtsyApiError, EtsyotomasyonError
from ..errors import describe
from ..jobs import JobCancelled
from ..router import ApiError, Request

if TYPE_CHECKING:  # pragma: no cover
    from ..context import AppContext
    from ..jobs import Job
    from ..router import Router

log = logging.getLogger("etsyotomasyon.web")

# Every OAuth scope Etsy's Open API v3 defines, as listed under
# components.securitySchemes.oauth2 in https://www.etsy.com/openapi/generated/oas/3.0.0.json.
# An extra scope outside this list is refused here instead of on Etsy's consent page.
ETSY_SCOPES = (
    "address_r", "address_w", "email_r", "listings_d", "listings_r", "listings_w",
    "profile_r", "profile_w", "shops_r", "shops_w", "transactions_r", "transactions_w",
)
CONNECT_KIND = "connect"
CONNECT_TIMEOUT = 300.0  # seconds the listener waits for the person to answer Etsy
LISTEN_WAIT = 5.0  # how long /start waits for the listener to accept connections
BIND_FAILS_WITHIN = 2.0  # a listener that gives up this fast never got its port
DONE_PATH = "/oauth-done"
MAX_KEY_LEN = 200
# Shop pref: the callback address the person said is on their Etsy app (or that has
# worked). A new callback address asks again.
CALLBACK_PREF = "etsy_callback_confirmed"
# Status states in which Etsy has accepted the saved keys at the last check.
KEYS_ACCEPTED = ("disconnected", "connected", "reconnect")

COUNT_TTL = 60.0  # the shop row's counts, as long as the Panel keeps its own
COUNT_ATTEMPTS = 2  # a page read must stay quick: two tries per request
COUNT_NAMES = ("listings", "to_ship")

# _CallbackHandler.return_url is class state: one connect flow sets it at a time.
_listener_lock = threading.Lock()


class CountCache:
    """Per-shop cache of the shop row's counts: {(shop_id, name): (read_at, value)}."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[tuple[str, str], tuple[float, int]] = {}

    def get(self, shop_id: str, name: str, ttl: float = COUNT_TTL) -> int | None:
        with self._lock:
            hit = self._items.get((shop_id, name))
        if hit is None or time.time() - hit[0] > ttl:
            return None
        return hit[1]

    def put(self, shop_id: str, name: str, value: int) -> None:
        with self._lock:
            self._items[(shop_id, name)] = (time.time(), value)

    def forget(self, shop_id: str, names: tuple[str, ...]) -> None:
        with self._lock:
            for name in names:
                self._items.pop((shop_id, name), None)


def register(r: Router, ctx: AppContext) -> None:
    counts = CountCache()
    ctx.on_change("listings", lambda c: counts.forget(c.shop_id, ("listings",)), name="connect")
    ctx.on_change("orders", lambda c: counts.forget(c.shop_id, ("to_ship",)), name="connect")
    r.get("/api/connect/info", info)
    r.post("/api/connect/keys", save_keys)
    r.post("/api/connect/callback-confirmed", callback_confirmed)
    r.get("/api/connect/preflight", preflight)
    r.post("/api/connect/start", start)
    r.post("/api/connect/disconnect", disconnect)
    r.get("/api/connect/counts", lambda req: shop_counts(req, counts))


def _ctx(req: Request) -> AppContext:
    assert req.ctx is not None
    return req.ctx


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


def _saved_keys() -> tuple[str, str]:
    return split_credential(
        settings.current("ETSY_KEYSTRING"), settings.current("ETSY_SHARED_SECRET")
    )


def _requested_scopes() -> list[str]:
    raw = settings.current("ETSY_SCOPES")
    return raw.split() if raw else list(DEFAULT_SCOPES)


def _active_connect(ctx: AppContext) -> Job | None:
    jobs = ctx.jobs.list(kind=CONNECT_KIND, active=True)
    return jobs[0] if jobs else None


def _redirect() -> tuple[str, bool]:
    """The callback address in use (the default when none is saved), and whether saved."""
    saved = _clean_text(settings.current("ETSY_REDIRECT_URI"))
    return saved or settings.ETSY_REDIRECT_DEFAULT, bool(saved)


def _callback_port(uri: str) -> int | None:
    """The port the listener opens for `uri`; None when the address has no usable one."""
    try:
        port = urllib.parse.urlparse(uri).port
    except ValueError:
        return None
    if port is None:
        return 80 if uri.lower().startswith("http://") else None
    return port if 0 < port < 65536 else None


def _load_token() -> auth.Token | None:
    try:
        return auth.load_token()
    except ConfigError:
        return None


def _callback_confirmed(ctx: AppContext, redirect: str, token: auth.Token | None) -> bool:
    """Has this shop's callback been added on Etsy? Asked once per shop and address.

    A sign-in made before the question existed proves the address worked then.
    """
    confirmed_for = ctx.shop_prefs().get(CALLBACK_PREF)
    if isinstance(confirmed_for, str) and confirmed_for:
        return confirmed_for == redirect
    if confirmed_for is False:  # unticked on purpose
        return False
    return token is not None


def _remember_callback(ctx: AppContext, redirect: str) -> None:
    try:
        ctx.update_shop_prefs(**{CALLBACK_PREF: redirect})
    except (OSError, EtsyotomasyonError):
        log.warning("could not save the callback confirmation")


# --- GET /api/connect/info -------------------------------------------------------------


def info(req: Request) -> dict[str, Any]:
    """Everything the page shows about the connection, read from disk (no network)."""
    ctx = _ctx(req)
    keystring, secret = _saved_keys()
    redirect, saved = _redirect()
    problem = ""
    try:
        auth.validate_redirect_uri(redirect)
    except AuthError as exc:
        problem = _first_line(exc)
    token = _load_token()
    requested = _requested_scopes()
    name = shops.current().name or None
    job = _active_connect(ctx)
    return {
        "keys": bool(keystring and secret),
        "keystring_prefix": keystring[:6],
        "secret_length": len(secret),
        "redirect_uri": redirect,
        "redirect_saved": saved,
        "redirect_default": settings.ETSY_REDIRECT_DEFAULT,
        "redirect_ok": not problem,
        "redirect_problem": problem,
        "redirect_local": auth.is_loopback(redirect),
        "callback_port": _callback_port(redirect),
        "callback_confirmed": _callback_confirmed(ctx, redirect, token),
        "scopes_requested": requested,
        "scopes_default": list(DEFAULT_SCOPES),
        "scopes_granted": list(token.scopes) if token else [],
        "missing_scopes": list(token.missing_scopes(tuple(requested))) if token else [],
        "known_scopes": list(ETSY_SCOPES),
        "connected": token is not None,
        "shop_name": ctx.anonymise(name) if name else None,
        "job": job.to_dict() if job else None,
    }


# --- GET /api/connect/counts -------------------------------------------------------------


def _count(client: Any, name: str) -> int:
    if name == "listings":
        return int(client.count_listings("active"))
    # getShopReceipts filters (OAS): paid, not shipped, not cancelled = the orders to ship.
    return int(client.count_receipts(was_paid=True, was_shipped=False, was_canceled=False))


def shop_counts(req: Request, cache: CountCache) -> dict[str, Any]:
    """Active listings and orders to ship, for the connected card's shop row.

    One limit=1 request each, cached a minute per shop and dropped when this app changes
    the shop's listings or orders. Each count may fail on its own ({"value": null,
    "error": {...}}); while the status check finds Etsy unreachable, only what is cached
    is answered, so the page does not wait for two time-outs.
    """
    ctx = _ctx(req)
    client = ctx.client()  # 409 setup_needed before the shop is connected
    shop_id = ctx.shop_id
    offline = ctx.status.get("state") == "offline"
    out: dict[str, Any] = {}
    for name in COUNT_NAMES:
        value = cache.get(shop_id, name, float("inf") if offline else COUNT_TTL)
        if value is None and offline:
            out[name] = {"value": None, "error": {"code": "offline", "message": "Etsy is not reachable.",
                                                  "params": {}}}
            continue
        if value is None:
            try:
                with client.attempts(COUNT_ATTEMPTS):
                    value = _count(client, name)
            except Exception as exc:  # noqa: BLE001 — one count failing must not hide the other
                out[name] = {"value": None, "error": describe(exc)}
                continue
            cache.put(shop_id, name, value)
        out[name] = {"value": value, "error": None}
    return out


# --- POST /api/connect/callback-confirmed ------------------------------------------------


def callback_confirmed(req: Request) -> dict[str, Any]:
    """The person says the callback address is on their Etsy app ({"confirmed": bool}).

    Remembered per shop for the address in use, so the question comes back only when
    the address changes.
    """
    ctx = _ctx(req)
    confirmed = req.json_object().get("confirmed", True)
    if not isinstance(confirmed, bool):
        raise ApiError(422, "invalid", "confirmed must be true or false", field="confirmed")
    redirect, _saved = _redirect()
    ctx.update_shop_prefs(**{CALLBACK_PREF: redirect if confirmed else False})
    return {"callback_confirmed": _callback_confirmed(ctx, redirect, _load_token()),
            "redirect_uri": redirect}


# --- POST /api/connect/keys -------------------------------------------------------------


def _text(body: dict[str, Any], name: str) -> str:
    value = body.get(name)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ApiError(422, "invalid", f"{name} must be text", field=name)
    return value


# What a copy from Etsy's app page, an e-mail or a chat brings along with a key:
# invisible characters, quotes, a label ("Keystring", "Shared secret"), the value on
# the line after its label, or both halves in the x-api-key form "keystring:secret".
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"), None)
_EDGE = "\"'`\u201c\u201d\u2018\u2019\u201e\u00ab\u00bb\u2039\u203a<>[](){},;. \t"
_LABEL = re.compile(
    r"^(?:etsy\s+)?(?P<label>key\s*string|x-api-key|api\s*key|client[\s_-]*id"
    r"|shared\s*secret|secret)(?![a-z0-9])\s*[:=]?\s*(?P<rest>.*)$",
    re.IGNORECASE,
)


def _clean_text(value: str) -> str:
    """One line with the invisible characters, spaces and quotes around it removed."""
    value = (value or "").translate(_INVISIBLE).replace("\u00a0", " ").replace("\u202f", " ")
    return value.strip().strip(_EDGE).strip()


def _key_pairs(role: str, value: str) -> list[tuple[str, str]]:
    if ":" in value:  # "keystring:shared_secret" in one piece
        head, _, tail = value.partition(":")
        return [("key", _clean_text(head)), ("secret", _clean_text(tail))]
    return [(role, value)]


def _key_pieces(text: str, role: str) -> list[tuple[str, str]]:
    """The ("key"|"secret", value) pieces in one field; `role` is the field's own."""
    found: list[tuple[str, str]] = []
    plain: list[str] = []
    waiting = ""  # a label alone on its line: its value is on the next line
    for raw in (text or "").translate(_INVISIBLE).splitlines():
        line = _clean_text(raw)
        if not line:
            continue
        match = _LABEL.match(line)
        if match:
            label_role = "secret" if "secret" in match["label"].lower() else "key"
            rest = _clean_text(match["rest"])
            if rest:
                found += _key_pairs(label_role, rest)
            else:
                waiting = label_role
        elif waiting:
            found += _key_pairs(waiting, line)
            waiting = ""
        else:
            plain.append(line)
    if len(plain) == 2 and not found and ":" not in "".join(plain):
        found += [("key", plain[0]), ("secret", plain[1])]  # the two values, one per line
    elif plain:
        # Anything longer stays one value, and the space in it makes it refused below.
        found += _key_pairs(role, " ".join(plain))
    return [(r, v) for r, v in found if v]


def clean_keys(keystring: str, shared_secret: str) -> tuple[str, str]:
    """The keystring and shared secret in whatever the person pasted into the two fields.

    Each field may hold its own value, both (labelled, one per line, or colon-joined),
    or the other one (a label says which). A field's own value wins over one found in
    the other field.
    """
    in_key = _key_pieces(keystring, "key")
    in_secret = _key_pieces(shared_secret, "secret")

    def first(role: str, *groups: list[tuple[str, str]]) -> str:
        for group in groups:
            for got_role, value in group:
                if got_role == role:
                    return value
        return ""

    return first("key", in_key, in_secret), first("secret", in_secret, in_key)


def _check_key(name: str, value: str) -> None:
    if value and set(value) <= set("•●∙·*"):
        # Etsy's page shows the secret as dots until the eye icon is pressed.
        raise ApiError(422, "masked_key", f"The {name} was copied while hidden.", field=name)
    if len(value) > MAX_KEY_LEN or any(ch.isspace() for ch in value):
        raise ApiError(422, "invalid_key", f"That does not look like an Etsy {name}.", field=name)


def _verify(ctx: AppContext) -> tuple[str, str]:
    """Ask Etsy whether it accepts the saved keys: ("ok"|"rejected"|"offline"|"unknown", why)."""
    client = ctx.client(require_auth=False)
    try:
        with client.attempts(2):
            client.ping()
    except EtsyApiError as exc:
        if exc.status == 0:
            return "offline", exc.message
        if exc.status in (401, 403):
            return "rejected", exc.message
        return "unknown", _first_line(exc)
    return "ok", ""


def save_keys(req: Request) -> dict[str, Any]:
    """Save the app keys (and callback), then check them with a key-only call.

    An empty keystring or secret keeps the saved one, so the callback can be changed
    without typing the keys again; a new keystring needs its own secret. A keystring
    from another app makes the old sign-in useless, so it is removed.
    """
    ctx = _ctx(req)
    body = req.json_object()
    typed_key, typed_secret = clean_keys(_text(body, "keystring"), _text(body, "shared_secret"))
    typed_redirect = _clean_text(_text(body, "redirect_uri"))
    saved_key, saved_secret = _saved_keys()
    keystring = typed_key or saved_key
    secret = typed_secret or (saved_secret if keystring == saved_key else "")
    if not keystring or not secret:
        missing = "keystring" if not keystring else "shared_secret"
        raise ApiError(422, "need_both_keys", "Both the keystring and the shared secret are needed.",
                       field=missing)
    _check_key("keystring", keystring)
    _check_key("shared_secret", secret)
    redirect = typed_redirect or _redirect()[0]
    try:
        auth.validate_redirect_uri(redirect)
    except AuthError as exc:
        raise ApiError(422, "bad_redirect", _first_line(exc), field="redirect_uri") from exc
    if ctx.jobs.busy():
        raise ApiError(409, "busy", "Wait for the running task to finish first.")

    other_app = bool(saved_key) and saved_key != keystring
    settings.save({
        "ETSY_KEYSTRING": keystring,
        "ETSY_SHARED_SECRET": secret,
        "ETSY_REDIRECT_URI": redirect,
    })
    token_cleared = other_app and auth.clear_token()
    ctx.reset_client()
    check, reason = _verify(ctx)
    status = ctx.refresh_status(force=True)
    return {
        "saved": True,
        "check": check,
        "reason": reason,
        "token_cleared": token_cleared,
        "keystring_prefix": keystring[:6],
        "secret_length": len(secret),
        "redirect_uri": redirect,
        "status": status,
    }


# --- POST /api/connect/start --------------------------------------------------------------


class _CancelFlag:
    """The one thing _capture_via_listener asks of its cancel Event, answered by the job."""

    def __init__(self, job: Job) -> None:
        self._job = job

    def is_set(self) -> bool:
        return self._job.cancelled


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except OSError:
        return False


def _stop_earlier_attempt(ctx: AppContext) -> None:
    """An earlier connect still waiting for Etsy is replaced; any other task blocks."""
    active = ctx.jobs.list(active=True)
    if any(job.kind != CONNECT_KIND for job in active):
        raise ApiError(409, "busy", "Wait for the running task to finish first.")
    for job in active:
        try:
            ctx.jobs.cancel(job.id)
        except ValueError:  # pragma: no cover - connect jobs are cancellable
            pass
        job.wait(LISTEN_WAIT)


def _extra_scopes(body: dict[str, Any]) -> list[str]:
    extra = body.get("extra_scopes") or []
    if not isinstance(extra, list) or not all(isinstance(s, str) for s in extra):
        raise ApiError(422, "invalid", "extra_scopes must be a list of scope names",
                       field="extra_scopes")
    unknown = [s for s in extra if s not in ETSY_SCOPES]
    if unknown:
        raise ApiError(422, "unknown_scope", f"Etsy has no scope {unknown[0]!r}.",
                       field="extra_scopes", scope=unknown[0])
    return extra


# --- the checks before Etsy's page opens (GET /api/connect/preflight, and /start) --------

# The HTTP status each refusal of /start answers with.
_REFUSAL_STATUS = {"offline": 503, "bad_redirect": 422}


def _check(ok: bool, code: str | None = None, message: str = "", **params: Any) -> dict[str, Any]:
    return {"ok": ok, "code": code, "message": message, "params": params}


def _keys_check(ctx: AppContext) -> dict[str, Any]:
    """Does Etsy accept the saved keys? The last status check says so when it got an
    answer; otherwise (never checked, refused before, offline) one key-only call now."""
    keystring, secret = _saved_keys()
    if not (keystring and secret):
        return _check(False, "setup_needed", "Save the Etsy app keys first.", step="keys")
    if ctx.status["state"] in KEYS_ACCEPTED:
        return _check(True)
    verdict, reason = _verify(ctx)
    if verdict == "rejected":
        return _check(False, "keys_rejected", reason, reason=reason)
    if verdict == "offline":
        return _check(False, "offline", reason)
    ctx.set_status_soon()  # the status the page shows is older than this answer
    return _check(True)  # "unknown" too: Etsy's own page will tell more than a guess


def _callback_check(redirect: str) -> dict[str, Any]:
    """Can the callback be caught here: valid for Etsy, plain http, localhost, a port?"""
    try:
        auth.validate_redirect_uri(redirect)
    except AuthError as exc:
        return _check(False, "bad_redirect", _first_line(exc), field="redirect_uri")
    if not auth.is_loopback(redirect):
        return _check(
            False, "callback_not_local",
            "Only an http://localhost callback can be caught on this computer.",
            redirect_uri=redirect, suggested=settings.ETSY_REDIRECT_DEFAULT,
        )
    if _callback_port(redirect) is None:
        return _check(False, "bad_redirect", f"{redirect} has no usable port.",
                      field="redirect_uri")
    return _check(True)


def _answers(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:  # refused, timed out, or no IPv6 on this machine
        return False


def _port_check(ctx: AppContext, port: int, *, own_listener_ok: bool = True) -> dict[str, Any]:
    """Is the callback port free? The browser tries localhost on IPv6 (::1) first, so a
    program there would get Etsy's answer even with our listener on 127.0.0.1."""
    job = _active_connect(ctx) if own_listener_ok else None
    if job is not None and job.params.get("port") == port:
        return _check(True, listening=True)  # our own listener, waiting for Etsy
    if port == ctx.port or not auth.port_is_free(port) or _answers("::1", port, 0.3):
        return _check(False, "port_in_use", f"Port {port} is already in use.", port=port)
    return _check(True)


def _preflight(ctx: AppContext) -> dict[str, Any]:
    redirect, saved = _redirect()
    port = _callback_port(redirect)
    callback = _callback_check(redirect)
    checks = {
        "keys": _keys_check(ctx),
        "callback": callback,
        "port": _port_check(ctx, port) if callback["ok"] and port is not None else None,
    }
    return {
        "ok": all(check["ok"] for check in checks.values() if check is not None),
        "checks": checks,
        "redirect_uri": redirect,
        "redirect_saved": saved,
        "port": port,
        "callback_confirmed": _callback_confirmed(ctx, redirect, _load_token()),
    }


def preflight(req: Request) -> dict[str, Any]:
    """What would stop "Etsy mağazanı bağla" now, checked before the person is sent to Etsy."""
    return _preflight(_ctx(req))


def _refuse(check: dict[str, Any]) -> ApiError:
    code = check["code"] or "internal"
    return ApiError(_REFUSAL_STATUS.get(code, 409), code, check["message"], **check["params"])


def start(req: Request) -> dict[str, Any]:
    """Check the keys, the callback and its port, open the listener, start the job.

    Answers only once the listener accepts connections, so the browser can be sent to
    Etsy straight away: {job_id, url, job}.
    """
    ctx = _ctx(req)
    body = req.json_object()
    extra = _extra_scopes(body)
    _stop_earlier_attempt(ctx)
    try:
        config = Config.load()
    except ConfigError as exc:
        raise ApiError(409, "setup_needed", _first_line(exc), step="keys") from exc

    changes: dict[str, str] = {}
    wanted = tuple(dict.fromkeys([*config.scopes, *extra]))
    if wanted != config.scopes:
        changes["ETSY_SCOPES"] = " ".join(wanted)
    if not config.redirect_uri:
        changes["ETSY_REDIRECT_URI"] = settings.ETSY_REDIRECT_DEFAULT
    if changes:
        settings.save(changes)
        config = Config.load()

    callback = _callback_check(config.redirect_uri)
    if not callback["ok"]:
        raise _refuse(callback)
    port = _callback_port(config.redirect_uri) or 80
    for check in (_port_check(ctx, port, own_listener_ok=False), _keys_check(ctx)):
        if not check["ok"]:
            raise _refuse(check)

    request = auth.build_authorization_url(config)
    return_url = f"http://localhost:{ctx.port}{DONE_PATH}"

    def work(job: Job) -> dict[str, Any]:
        return _connect(ctx, job, config, request, port, return_url)

    job = ctx.jobs.start(
        CONNECT_KIND, "connect:job.title", work,
        params={"port": port}, cancellable=True, refresh_status=True,
    )
    _wait_until_listening(job, port)
    return {"job_id": job.id, "url": request.url, "job": job.summary()}


def _wait_until_listening(job: Job, port: int) -> None:
    deadline = time.monotonic() + LISTEN_WAIT
    while time.monotonic() < deadline:
        if job.wait(0.02):
            if job.status == "cancelled":
                raise ApiError(409, "cancelled", "Cancelled.")
            error = job.error or {"code": "internal", "message": "", "params": {}}
            raise ApiError(409, error["code"], error["message"], **(error.get("params") or {}))
        if _listening(port):
            return
    log.warning("the Etsy callback listener on port %s did not answer in time", port)


def _phase(job: Job, phase: str, step: int) -> None:
    job.set_state(phase=phase)
    job.progress(step, 4, label=phase)
    job.emit("phase", phase=phase)


def _listener_error(job: Job, exc: AuthError, port: int, elapsed: float) -> BaseException:
    """Why the listener came back without a code, as a code the page can explain."""
    result = dict(auth._CallbackHandler.result)
    if job.cancelled:
        return JobCancelled()
    if "error" in result:
        return ApiError(400, "consent_denied", _first_line(exc), etsy_error=result["error"])
    if result:
        return ApiError(400, "state_mismatch", _first_line(exc))
    if elapsed < BIND_FAILS_WITHIN:
        return ApiError(409, "port_in_use", _first_line(exc), port=port)
    return ApiError(408, "connect_timeout", _first_line(exc), minutes=int(CONNECT_TIMEOUT // 60))


def _connect(
    ctx: AppContext, job: Job, config: Config, request: auth.AuthRequest, port: int,
    return_url: str,
) -> dict[str, Any]:
    job.set_state(url=request.url, port=port)
    _phase(job, "opened", 1)
    started = time.monotonic()
    with _listener_lock:
        auth._CallbackHandler.return_url = return_url
        try:
            code = auth._capture_via_listener(
                request, config, port, CONNECT_TIMEOUT, _CancelFlag(job)  # type: ignore[arg-type]
            )
        except AuthError as exc:
            raise _listener_error(job, exc, port, time.monotonic() - started) from exc
        finally:
            auth._CallbackHandler.return_url = None
    job.check_cancel()
    # Etsy sent the browser to this address, so it is on the app: never ask again.
    _remember_callback(ctx, config.redirect_uri)

    _phase(job, "code_received", 2)
    try:
        token = auth.exchange_code(config, code, request.verifier)
    except AuthUnreachable:
        raise
    except AuthError as exc:
        raise ApiError(502, "token_refused", _first_line(exc)) from exc
    ctx.reset_client()

    _phase(job, "fetching_shop", 3)
    shop = ctx.client().shop()
    name = str(shop.get("shop_name") or "")
    try:
        shops.remember(name, shop.get("shop_id"))
    except (OSError, EtsyotomasyonError):
        log.warning("could not remember the shop name")
    ctx.refresh_status(force=True)
    shown = ctx.anonymise(name) if name else None
    if name:
        # The real name is stored: the bell and the Panel hide it when they show it,
        # for as long as the hide-names preference is on (also for older notifications).
        note = ctx.notify("connect", "notify.connected", {"shop": name}, tone="success",
                          link="/kurulum/magaza")
    else:
        note = ctx.notify("connect", "notify.connected_plain", tone="success",
                          link="/kurulum/magaza")
    _phase(job, "done", 4)
    return {
        "shop_name": shown,
        "scopes": list(token.scopes),
        "missing_scopes": list(token.missing_scopes(config.scopes)),
        # For the bell's sake: the page that watched the connect end marks it read (the
        # card turning into "Mağaza bağlandı" said it already); on any other page it stays.
        "notification": note.get("id"),
    }


# --- POST /api/connect/disconnect ------------------------------------------------------------


def disconnect(req: Request) -> dict[str, Any]:
    """Delete token.json. The keys stay; Etsy's own record of the app is not touched."""
    ctx = _ctx(req)
    if ctx.jobs.busy():
        raise ApiError(409, "busy", "Wait for the running task to finish first.")
    redirect, _saved = _redirect()
    token = _load_token()
    if token is not None and _callback_confirmed(ctx, redirect, token):
        _remember_callback(ctx, redirect)  # the sign-in proved it: keep it past the token
    removed = auth.clear_token()
    ctx.reset_client()
    return {"removed": removed, "status": ctx.refresh_status(force=True)}
