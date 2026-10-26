"""Core endpoints: session, status, preferences, shops, jobs, notifications, files, quit.

See the build spec, section 4, for the shapes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...config import home_dir
from .. import files, i18n
from ..router import ApiError, Request, Response

if TYPE_CHECKING:  # pragma: no cover
    from ..context import AppContext
    from ..router import Router

FIRST_STATUS_WAIT = 3.0
FOLDERS = ("workspace", "mockups", "products", "drafts")


def register(r: Router, ctx: AppContext) -> None:
    r.get("/api/ping", ping)
    r.get("/api/session", session)
    r.get("/api/status", status)
    r.post("/api/status/refresh", status_refresh)
    r.get("/api/prefs", prefs)
    r.post("/api/prefs", save_prefs)
    r.get("/api/shops", shops_list)
    r.post("/api/shops/switch", shops_switch, exclusive=True)
    r.post("/api/shops/add", shops_add, exclusive=True)
    r.post("/api/shops/remove", shops_remove, exclusive=True)
    r.get("/api/jobs", jobs_list)
    r.get("/api/jobs/{id}", job_get)
    r.post("/api/jobs/{id}/cancel", job_cancel)
    r.get("/api/notifications", notifications)
    r.post("/api/notifications/read", notifications_read)
    r.post("/api/quit", quit_app)
    r.get("/api/files/workspace", workspace_file)
    r.get("/api/files/thumb", workspace_thumb)
    r.post("/api/open-folder", open_folder)


def _ctx(req: Request) -> AppContext:
    assert req.ctx is not None
    return req.ctx


# --- session and status ---------------------------------------------------------------


def ping(req: Request) -> dict[str, Any]:
    ctx = _ctx(req)
    return {"app": "etsyotomasyon", "version": ctx.version, "instance": ctx.instance}


def session(req: Request) -> dict[str, Any]:
    ctx = _ctx(req)
    return {
        "version": ctx.version,
        "language": ctx.language,
        "anonymise": ctx.anonymise_names,
        "port": ctx.port,
        "shop_id": ctx.shop_id,
        "shops": ctx.shops_list(),
        # The sidebar shows the Pinterest item only while Pinterest is in use.
        "pinterest": pinterest_in_use(),
    }


def pinterest_in_use() -> bool:
    """Pinterest is set up or was used: an app, a sign-in or Pins in the queue.

    Local files only, no network. A file that cannot be read counts as in use: the
    Pinterest page is where such a problem is explained.
    """
    from ... import pinterest

    try:
        if pinterest.token_path().is_file():
            return True
        config = pinterest.PinterestConfig.load()
        if config.app_id or config.access_token:
            return True
        return bool(pinterest.Queue.load().entries)
    except Exception:  # noqa: BLE001 — never let the session call fail over Pinterest
        return True


def status(req: Request) -> dict[str, Any]:
    ctx = _ctx(req)
    # The very first check starts with the app; give it a moment rather than show
    # "checking" when the answer (no keys, say) needs no network at all.
    if not ctx.wait_first_status(0):
        ctx.set_status_soon(0.0)
        ctx.wait_first_status(FIRST_STATUS_WAIT)
    return ctx.status


def status_refresh(req: Request) -> dict[str, Any]:
    body = req.json_object()
    return _ctx(req).refresh_status(force=bool(body.get("force")))


# --- preferences ------------------------------------------------------------------------


def _prefs_payload(ctx: AppContext) -> dict[str, Any]:
    data: dict[str, Any] = dict(ctx.shop_prefs())
    data.update(
        language=ctx.language,
        anonymise=ctx.anonymise_names,
        workspace=str(ctx.workspace_root()),
    )
    return data


def prefs(req: Request) -> dict[str, Any]:
    return _prefs_payload(_ctx(req))


def save_prefs(req: Request) -> dict[str, Any]:
    ctx = _ctx(req)
    body = req.json_object()
    app = ctx.app_prefs()
    names_changed = False
    if "language" in body:
        language = i18n.normalise(body["language"])
        if language is None or body["language"] not in i18n.LANGUAGES:
            raise ApiError(422, "invalid", "language must be 'tr' or 'en'", field="language")
        app["language"] = language
    if "anonymise" in body:
        if not isinstance(body["anonymise"], bool):
            raise ApiError(422, "invalid", "anonymise must be true or false", field="anonymise")
        names_changed = bool(app.get("anonymise", False)) != body["anonymise"]
        app["anonymise"] = body["anonymise"]
    ctx.save_app_prefs(app)
    if names_changed or "language" in body:
        ctx.publish_status()
    return _prefs_payload(ctx)


# --- shops ------------------------------------------------------------------------------------


def shops_list(req: Request) -> dict[str, Any]:
    ctx = _ctx(req)
    return {"current": ctx.shop_id, "shops": ctx.shops_list()}


def shops_switch(req: Request) -> dict[str, Any]:
    ctx = _ctx(req)
    body = req.json_object()
    shop_id = body.get("id")
    if not isinstance(shop_id, str):
        raise ApiError(422, "invalid", "id must be a shop id", field="id")
    ctx.switch_shop(shop_id)
    return {"current": ctx.shop_id, "shops": ctx.shops_list()}


def shops_add(req: Request) -> dict[str, Any]:
    ctx = _ctx(req)
    ctx.add_shop()
    return {"current": ctx.shop_id, "shops": ctx.shops_list()}


def shops_remove(req: Request) -> dict[str, Any]:
    ctx = _ctx(req)
    body = req.json_object()
    shop_id = body.get("id")
    if not isinstance(shop_id, str):
        raise ApiError(422, "invalid", "id must be a shop id", field="id")
    if not shop_id:
        raise ApiError(400, "cannot_remove_base", "The first shop cannot be removed.")
    if body.get("confirm") is not True:
        raise ApiError(400, "confirm_required", "Removing a shop needs {\"confirm\": true}.")
    ctx.remove_shop(shop_id)
    return {"current": ctx.shop_id, "shops": ctx.shops_list()}


# --- jobs ---------------------------------------------------------------------------------------


def jobs_list(req: Request) -> list[dict[str, Any]]:
    ctx = _ctx(req)
    kind = req.query.get("kind") or None
    return [job.summary() for job in ctx.jobs.list(kind=kind, active=req.bool_query("active"))]


def _job(req: Request) -> Any:
    job = _ctx(req).jobs.get(str(req.params["id"]))
    if job is None:
        raise ApiError(404, "not_found", "No such task.")
    return job


def job_get(req: Request) -> dict[str, Any]:
    return _job(req).to_dict()


def job_cancel(req: Request) -> dict[str, Any]:
    ctx = _ctx(req)
    job = _job(req)
    try:
        ctx.jobs.cancel(job.id)
    except ValueError as exc:
        raise ApiError(409, "not_cancellable", "This task cannot be stopped halfway.") from exc
    return job.summary()


# --- notifications ----------------------------------------------------------------------------


def notifications(req: Request) -> dict[str, Any]:
    items = _ctx(req).notifications()
    return {"items": items, "unread": sum(1 for item in items if not item.get("read"))}


def notifications_read(req: Request) -> dict[str, Any]:
    ctx = _ctx(req)
    body = req.json_object()
    ids = body.get("ids")
    if ids is not None and not (isinstance(ids, list) and all(isinstance(i, str) for i in ids)):
        raise ApiError(422, "invalid", "ids must be a list of notification ids", field="ids")
    unread = ctx.mark_notifications_read(ids)
    return {"items": ctx.notifications(), "unread": unread}


# --- quit ---------------------------------------------------------------------------------------


def quit_app(req: Request) -> dict[str, Any]:
    ctx = _ctx(req)
    body = req.json_object()
    if ctx.jobs.busy() and body.get("force") is not True:
        raise ApiError(409, "busy", "A task is still running.")
    ctx.request_quit()
    return {"ok": True}


# --- files ----------------------------------------------------------------------------------------


def _image(req: Request) -> Any:
    ctx = _ctx(req)
    rel = req.query.get("path", "")
    target = files.workspace_image(ctx.workspace_root(), rel)
    if target is None:
        raise ApiError(404, "not_found", "No such image in the products folder.")
    return target


def _cache_headers(req: Request) -> dict[str, str]:
    # A URL that carries a version (?v=<mtime>) may be cached; any other may not.
    return {"Cache-Control": "private, max-age=86400"} if req.query.get("v") else {}


def workspace_file(req: Request) -> Response:
    target = _image(req)
    return Response.file(target, files.content_type_for(target), _cache_headers(req))


def workspace_thumb(req: Request) -> Response:
    from PIL import Image

    target = _image(req)
    width = req.int_query("w", 400)
    width = max(files.THUMB_MIN, min(files.THUMB_MAX, width or 400))
    # bg=checker: the page draws a transparent design on its checkerboard, so the
    # thumbnail keeps its alpha (a PNG); anything else is flattened on white (a JPEG).
    keep_alpha = req.query.get("bg") == "checker"
    try:
        thumb = files.thumbnail(target, width, home_dir() / "cache" / "thumbs",
                                keep_alpha=keep_alpha)
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ApiError(404, "not_found", "This file cannot be shown as an image.") from exc
    return Response.file(thumb, "image/png" if keep_alpha else "image/jpeg",
                         _cache_headers(req))


def open_folder(req: Request) -> dict[str, Any]:
    ctx = _ctx(req)
    body = req.json_object()
    which = body.get("which", "workspace")
    if which not in FOLDERS:
        raise ApiError(422, "invalid", f"which must be one of {', '.join(FOLDERS)}", field="which")
    ws = ctx.workspace()
    path = {"workspace": ws.root, "mockups": ws.mockups, "products": ws.products,
            "drafts": ws.drafts}[which]
    path.mkdir(parents=True, exist_ok=True)
    try:
        files.open_path(path)
    except OSError as exc:
        raise ApiError(500, "internal", f"Could not open the folder: {exc}") from exc
    return {"ok": True, "path": str(path)}
