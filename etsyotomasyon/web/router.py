"""Routes, requests and responses for the JSON API.

A handler is a plain function of one argument:

    def list_things(req: Request) -> dict | list | Response | None:
        page = req.int_query("page", 1, min=1)
        return {"items": [...], "page": page}

    r.get("/api/things", list_things)
    r.post("/api/things/{id:int}/rename", rename_thing)
    r.get("/api/mockups/{name}", one_mockup)          # {name}: one path segment, decoded
    r.post("/api/shops/switch", switch, exclusive=True)  # runs under the shop write lock

Returning a dict, list or None sends it as JSON with status 200 (None -> `{}`).
Return a `Response` for anything else. Raise `ApiError(status, code, message, **params)`
for an expected failure; library exceptions (ValidationError, EtsyApiError, ...) are
mapped to HTTP by `web.errors`, so handlers normally let them propagate.

Every /api handler runs while holding the shop *read* lock, so the open shop cannot
change underneath it; `exclusive=True` takes the *write* lock instead (only for
handlers that change which shop is open). See `context.ShopLock`.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # pragma: no cover
    from .context import AppContext

JSON_TYPE = "application/json; charset=utf-8"


class ApiError(Exception):
    """An expected failure with an error code the UI translates (`errors.<code>`)."""

    # Positional-only, so a param may itself be called status, code or message.
    def __init__(self, status: int, code: str, message: str = "", /, **params: Any) -> None:
        self.status = int(status)
        self.code = code
        self.message = message or code
        self.params = params
        super().__init__(f"{status} {code}: {self.message}")

    def to_dict(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "params": self.params}}


def json_default(value: Any) -> Any:
    """What json.dumps cannot encode on its own, the way the API sends it."""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "to_dict"):
        return value.to_dict()
    raise TypeError(f"{type(value).__name__} is not JSON serialisable")


def dumps(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=json_default).encode(
        "utf-8"
    )


class Response:
    """An HTTP response. Build one with the class methods."""

    def __init__(
        self,
        status: int = 200,
        body: bytes = b"",
        content_type: str = JSON_TYPE,
        headers: Mapping[str, str] | None = None,
        *,
        file_path: Path | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.content_type = content_type
        self.headers: dict[str, str] = dict(headers or {})
        self.file_path = file_path

    @classmethod
    def json(cls, data: Any, status: int = 200, headers: Mapping[str, str] | None = None) -> Response:
        return cls(status, dumps(data), JSON_TYPE, headers)

    @classmethod
    def text(
        cls, text: str, status: int = 200, content_type: str = "text/plain; charset=utf-8"
    ) -> Response:
        return cls(status, text.encode("utf-8"), content_type)

    @classmethod
    def bytes(
        cls,
        data: bytes,
        content_type: str,
        headers: Mapping[str, str] | None = None,
        status: int = 200,
    ) -> Response:
        return cls(status, bytes(data), content_type, headers)

    @classmethod
    def file(
        cls,
        path: Path,
        content_type: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        """Stream a file from disk. The content type defaults to the server's MIME map."""
        from .files import content_type_for

        path = Path(path)
        return cls(200, b"", content_type or content_type_for(path), headers, file_path=path)

    @classmethod
    def redirect(cls, location: str, status: int = 302) -> Response:
        return cls(status, b"", "text/plain; charset=utf-8", {"Location": location})

    @classmethod
    def empty(cls, status: int = 204) -> Response:
        return cls(status, b"", "text/plain; charset=utf-8")

    @classmethod
    def error(cls, error: ApiError) -> Response:
        return cls.json(error.to_dict(), error.status)


@dataclass
class Request:
    """One API request, as a handler sees it.

    `query` holds the last value of each query parameter; `query_list(name)` gives
    all of them. `headers` is case-insensitive (an email.message.Message).
    """

    method: str
    path: str
    query: dict[str, str]
    headers: Any
    body: bytes = b""
    params: dict[str, Any] = field(default_factory=dict)
    ctx: AppContext | None = None
    raw_query: str = ""

    def query_list(self, name: str) -> list[str]:
        return [v for k, v in urllib.parse.parse_qsl(self.raw_query, keep_blank_values=True) if k == name]

    def json(self) -> Any:
        """The body as JSON; `{}` for an empty body; 400 invalid_json otherwise."""
        if not self.body.strip():
            return {}
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ApiError(400, "invalid_json", f"The request body is not valid JSON: {exc}") from exc

    def json_object(self) -> dict[str, Any]:
        """Like json(), but the body must be an object (400 invalid_json otherwise)."""
        data = self.json()
        if not isinstance(data, dict):
            raise ApiError(400, "invalid_json", "The request body must be a JSON object.")
        return data

    def int_query(
        self, name: str, default: int | None = None, *, min: int | None = None, max: int | None = None  # noqa: A002
    ) -> int | None:
        """An integer query parameter; 422 invalid when it is not one or out of range."""
        raw = self.query.get(name, "")
        if raw == "":
            return default
        try:
            value = int(raw)
        except ValueError as exc:
            raise ApiError(422, "invalid", f"{name} must be a whole number", field=name) from exc
        if (min is not None and value < min) or (max is not None and value > max):
            raise ApiError(422, "invalid", f"{name} is out of range", field=name, min=min, max=max)
        return value

    def bool_query(self, name: str, default: bool = False) -> bool:
        raw = self.query.get(name, "").strip().lower()
        if raw == "":
            return default
        return raw in ("1", "true", "yes", "on")


Handler = Callable[[Request], Any]

_PARAM = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::(int|str|path))?\}")


@dataclass
class Route:
    method: str
    pattern: str
    handler: Handler
    exclusive: bool
    regex: re.Pattern[str]
    converters: dict[str, str]


def _compile(pattern: str) -> tuple[re.Pattern[str], dict[str, str]]:
    if not pattern.startswith("/"):
        raise ValueError(f"route {pattern!r} must start with /")
    converters: dict[str, str] = {}
    out, pos = "", 0
    for match in _PARAM.finditer(pattern):
        out += re.escape(pattern[pos : match.start()])
        name, kind = match.group(1), match.group(2) or "str"
        converters[name] = kind
        out += {"int": r"(?P<%s>[0-9]+)", "str": r"(?P<%s>[^/]+)", "path": r"(?P<%s>.+)"}[kind] % name
        pos = match.end()
    out += re.escape(pattern[pos:])
    return re.compile(f"^{out}$"), converters


class Router:
    """Method + path pattern -> handler. See the module docstring for the conventions."""

    def __init__(self) -> None:
        self.routes: list[Route] = []

    def add(self, method: str, pattern: str, handler: Handler, *, exclusive: bool = False) -> Handler:
        regex, converters = _compile(pattern)
        method = method.upper()
        for route in self.routes:
            if route.method == method and route.pattern == pattern:
                raise ValueError(f"{method} {pattern} is registered twice")
        self.routes.append(Route(method, pattern, handler, exclusive, regex, converters))
        return handler

    def _verb(self, method: str, pattern: str, handler: Handler | None, exclusive: bool) -> Any:
        if handler is None:  # used as a decorator
            return lambda fn: self.add(method, pattern, fn, exclusive=exclusive)
        return self.add(method, pattern, handler, exclusive=exclusive)

    def get(self, pattern: str, handler: Handler | None = None, *, exclusive: bool = False) -> Any:
        return self._verb("GET", pattern, handler, exclusive)

    def post(self, pattern: str, handler: Handler | None = None, *, exclusive: bool = False) -> Any:
        return self._verb("POST", pattern, handler, exclusive)

    def put(self, pattern: str, handler: Handler | None = None, *, exclusive: bool = False) -> Any:
        return self._verb("PUT", pattern, handler, exclusive)

    def patch(self, pattern: str, handler: Handler | None = None, *, exclusive: bool = False) -> Any:
        return self._verb("PATCH", pattern, handler, exclusive)

    def delete(self, pattern: str, handler: Handler | None = None, *, exclusive: bool = False) -> Any:
        return self._verb("DELETE", pattern, handler, exclusive)

    def match(self, method: str, raw_path: str) -> tuple[Route, dict[str, Any]]:
        """The route for a request and its decoded path parameters.

        Matching is done on the raw (still percent-encoded) path, so an encoded
        slash inside `{name}` stays inside that one parameter; each parameter is
        decoded afterwards. ApiError 404 / 405 when nothing fits.
        """
        method = method.upper()
        allowed: list[str] = []
        for route in self.routes:
            found = route.regex.match(raw_path)
            if not found:
                continue
            if route.method != method and not (method == "HEAD" and route.method == "GET"):
                allowed.append(route.method)
                continue
            params: dict[str, Any] = {}
            for name, raw in found.groupdict().items():
                value = urllib.parse.unquote(raw)
                if route.converters[name] == "int":
                    params[name] = int(value)
                else:
                    if "\x00" in value:
                        raise ApiError(404, "not_found", "No such path")
                    params[name] = value
            return route, params
        if allowed:
            raise ApiError(405, "method_not_allowed", f"{method} is not allowed here", allowed=allowed)
        raise ApiError(404, "not_found", f"No API endpoint {raw_path}")
