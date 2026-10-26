"""New-version notice: what GitHub's latest release is, and the switches for it.

    GET  /api/update             the checker's state (see web/update.py, UpdateChecker.state)
    POST /api/update/check       look now ("Şimdi kontrol et"); 409 update_check_off when
                                 ETSYOTOMASYON_NO_UPDATE_CHECK is set
    POST /api/update/dismiss     {"version": "0.3.1"}: hide the top-bar pill for that version
    POST /api/update/auto        {"enabled": bool}: the Ayarlar toggle (daily automatic look)

Every change is also pushed to open tabs on the SSE topic `update` (the same object).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..router import ApiError, Request

if TYPE_CHECKING:  # pragma: no cover
    from ..context import AppContext
    from ..router import Router


def register(r: Router, ctx: AppContext) -> None:
    r.get("/api/update", state)
    r.post("/api/update/check", check_now)
    r.post("/api/update/dismiss", dismiss)
    r.post("/api/update/auto", set_auto)


def _ctx(req: Request) -> AppContext:
    assert req.ctx is not None
    return req.ctx


def state(req: Request) -> dict[str, Any]:
    return _ctx(req).updates.state()


def check_now(req: Request) -> dict[str, Any]:
    updates = _ctx(req).updates
    if updates.blocked:
        raise ApiError(409, "update_check_off",
                       "Update checks are turned off (ETSYOTOMASYON_NO_UPDATE_CHECK).")
    return updates.check(force=True)


def dismiss(req: Request) -> dict[str, Any]:
    body = req.json_object()
    version = body.get("version")
    if not isinstance(version, str):
        raise ApiError(422, "invalid", "version must be text such as 0.3.1", field="version")
    try:
        return _ctx(req).updates.dismiss(version)
    except ValueError as exc:
        raise ApiError(422, "invalid", "version must look like 0.3.1", field="version") from exc


def set_auto(req: Request) -> dict[str, Any]:
    body = req.json_object()
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise ApiError(422, "invalid", "enabled must be true or false", field="enabled")
    return _ctx(req).updates.set_auto(enabled)
