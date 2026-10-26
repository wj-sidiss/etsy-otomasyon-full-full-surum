"""Filigran: the seller's watermark, its settings and a live preview on a real mockup.

    GET    /api/watermark                  the mark's facts, its settings, what a run does
    PUT    /api/watermark/file?name=<n>    the picture as the raw body (PNG, JPG or WebP,
                                           at most 10 MB): becomes the mark
    DELETE /api/watermark/file             forget the picture (the settings stay)
    PATCH  /api/watermark                  {enabled?, scope?, position?, opacity?, size?,
                                            tile_size?}
    POST   /api/watermark/remove-ground    make an opaque mark's solid background clear
    GET    /api/watermark/image?v=         the stored mark (PNG, see-through kept)
    GET    /api/watermark/preview?mockup=<name|flat>&design=<sample|rel>&position=
           &opacity=&size=&max=            a mockup composited as a draft gets it, with
                                           the mark on it (unsaved values may be sent)

Everything is local (drop.watermark keeps watermark.png and watermark.json at the
workspace root); no Etsy call is made. The preview goes through the same door as the
drafts: mockup.compose onto the screen-sized mockup, then watermark.apply, whose sizes
are shares of the photo's width, so a 900 px preview looks like a 3000 px photo.
"""

from __future__ import annotations

import io
import threading
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from ...drop import catalog, mockup
from ...drop import watermark as wm
from ...errors import ValidationError
from .. import files
from ..router import ApiError, Request, Response
from . import mockups as mockups_api

if TYPE_CHECKING:  # pragma: no cover
    from ...drop.workspace import Workspace
    from ..context import AppContext
    from ..router import Router

FLAT = "flat"
PREVIEW_MAX_DEFAULT = 900

_preview_lock = threading.Lock()


def register(r: Router, ctx: AppContext) -> None:
    r.get("/api/watermark", get_watermark)
    r.patch("/api/watermark", patch_watermark)
    r.put("/api/watermark/file", upload_file)
    r.delete("/api/watermark/file", delete_file)
    r.post("/api/watermark/remove-ground", remove_ground)
    r.get("/api/watermark/image", watermark_image)
    r.get("/api/watermark/preview", preview)


def _ws(req: Request) -> Workspace:
    return mockups_api._ws(req)


def _listing_type(ws: Workspace) -> str | None:
    """The saved template's type (physical | download | both); None without one."""
    from ...drop.template import Template

    try:
        return Template.from_dict(ws.read_template()).listing_type
    except (ValidationError, OSError, ValueError, TypeError):
        return None


def _run_mockups(ws: Workspace, listing_type: str | None) -> list[str]:
    """The mockups a run composites onto, in order (Tasarım Yükle's own rule)."""
    from . import designs as designs_api

    try:
        return designs_api.used_mockups(ws, listing_type)[0]
    except (OSError, ValueError):
        return []


def _preview_design(ws: Workspace, listing_type: str | None) -> str:
    """The design the preview shows: the Mockuplar editor's default (the first
    transparent design, else the sample); for a digital template, whose opaque printables
    go onto the mockups too, the first design when none is transparent."""
    design = mockups_api.default_design(ws)
    if design == mockups_api.SAMPLE and listing_type in ("download", "both"):
        first = ws.product_files()[:1]
        if first:
            return first[0].relative_to(ws.products).as_posix()
    return design


def _error(exc: wm.WatermarkError) -> ApiError:
    status = 409 if exc.code in ("watermark_missing", "watermark_no_ground") else 422
    return ApiError(status, exc.code, str(exc), **exc.params)


def _payload(ws: Workspace) -> dict[str, Any]:
    settings = wm.load_settings(ws)
    info = wm.file_info(ws)
    listing_type = _listing_type(ws)
    on = bool(info and settings.enabled)
    used = _run_mockups(ws, listing_type)
    return {
        "file": info,
        "settings": settings.to_dict(),
        "on": on,
        "listing_type": listing_type,
        # Whether the next Tasarım Yükle run stamps (None: no template to tell by).
        "applies": None if listing_type is None else bool(
            on and settings.applies_to(listing_type)),
        "preview_mockups": used,
        "preview_default": used[0] if used else FLAT,
        "preview_design": _preview_design(ws, listing_type),
        "defaults": {"enabled": wm.DEFAULTS.enabled, "scope": wm.DEFAULTS.scope,
                     "position": wm.DEFAULTS.position, "opacity": wm.OPACITY_DEFAULT,
                     "size": wm.SIZE_DEFAULT, "tile_size": wm.TILE_SIZE_DEFAULT},
        "limits": {"opacity": [wm.OPACITY_MIN, wm.OPACITY_MAX],
                   "size": [wm.SIZE_MIN, wm.SIZE_MAX],
                   "tile_size": [wm.TILE_SIZE_MIN, wm.TILE_SIZE_MAX],
                   "max_mb": wm.MAX_UPLOAD_BYTES // 1024 // 1024, "min_edge": wm.MIN_EDGE,
                   "types": [s.lstrip(".") for s in wm.UPLOAD_SUFFIXES]},
    }


def get_watermark(req: Request) -> dict[str, Any]:
    return _payload(_ws(req))


def upload_file(req: Request) -> dict[str, Any]:
    """PUT /api/watermark/file?name=<file name> with the picture as the raw body."""
    ws = _ws(req)
    filename = (req.query.get("name") or "").strip()
    if not filename:
        raise ApiError(422, "invalid", "name is required", field="name")
    name = catalog.safe_name(filename)
    try:
        with files.decoding(name, lambda: (0, 0)):
            wm.save_upload(ws, filename, req.body)
    except catalog.TooManyPixels as exc:
        raise files.too_many_pixels(name, exc.width, exc.height) from exc
    except wm.WatermarkError as exc:
        raise _error(exc) from exc
    return _payload(ws)


def delete_file(req: Request) -> dict[str, Any]:
    ws = _ws(req)
    try:
        removed = wm.remove(ws)
    except OSError as exc:
        raise ApiError(409, "file_locked", f"The watermark could not be deleted: {exc}",
                       name=wm.WATERMARK_FILE) from exc
    payload = _payload(ws)
    payload["removed"] = removed
    return payload


def patch_watermark(req: Request) -> dict[str, Any]:
    """PATCH /api/watermark {enabled?, scope?, position?, opacity?, size?, tile_size?}."""
    ws = _ws(req)
    body = req.json_object()
    if not body:
        raise ApiError(422, "invalid", "Nothing to change.")
    try:
        wm.update_settings(ws, body)
    except wm.WatermarkError as exc:
        raise _error(exc) from exc
    return _payload(ws)


def remove_ground(req: Request) -> dict[str, Any]:
    ws = _ws(req)
    try:
        with files.decoding(wm.WATERMARK_FILE, lambda: (0, 0)):
            wm.remove_ground(ws)
    except wm.WatermarkError as exc:
        raise _error(exc) from exc
    except ValidationError as exc:
        raise ApiError(422, "watermark_unreadable", str(exc)) from exc
    return _payload(ws)


def watermark_image(req: Request) -> Response:
    ws = _ws(req)
    path = wm.watermark_path(ws)
    if not path.is_file():
        raise ApiError(404, "watermark_missing", "There is no watermark yet.")
    return Response.file(path, "image/png", mockups_api._cache_headers(req))


def _overrides(req: Request) -> dict[str, Any]:
    """Unsaved values from the query (the sliders), checked like PATCH does."""
    changes: dict[str, Any] = {}
    if req.query.get("position"):
        changes["position"] = req.query["position"]
    for key in ("opacity", "size", "tile_size"):
        raw = req.query.get(key)
        if raw is None or raw == "":
            continue
        try:
            changes[key] = int(raw)
        except ValueError as exc:
            raise ApiError(422, "invalid", f"{key} must be a whole number", field=key) from exc
    try:
        return wm.validate(changes)
    except wm.WatermarkError as exc:
        raise _error(exc) from exc


def _base_image(ws: Workspace, target: str, design: str, max_edge: int) -> Any:
    """The picture a draft would get (screen-sized): the design on a mockup, or flat."""
    from PIL import Image

    source = mockups_api._design_path(ws, design)
    art = mockups_api.design_png(source, max_edge)
    if target == FLAT:
        with Image.open(art) as opened:
            design_image = opened.convert("RGBA")
        edge = max_edge
        canvas = Image.new("RGB", (edge, edge), mockup.WHITE)
        scale = min(edge * 0.86 / design_image.width, edge * 0.86 / design_image.height)
        size = (max(1, round(design_image.width * scale)),
                max(1, round(design_image.height * scale)))
        design_image = design_image.resize(size, Image.LANCZOS)
        canvas.paste(design_image, ((edge - size[0]) // 2, (edge - size[1]) // 2), design_image)
        return canvas
    path = mockups_api._mockup_file(ws, target)
    area = catalog.effective_area(ws, target)[0]
    base = mockups_api.display_image(path, max_edge)
    out = mockups_api._cache_dir() / f"watermark-{threading.get_ident()}.jpg"
    try:
        mockup.compose(art, base, out, area=area, min_edge=0,
                       kind=catalog.kind_of(catalog.load(ws), target))
        with Image.open(out) as opened:
            opened.load()
            return opened.copy()
    finally:
        try:
            out.unlink()
        except OSError:
            pass


def preview(req: Request) -> Response:
    """GET /api/watermark/preview: a real composite with the (unsaved) mark on it."""
    ws = _ws(req)
    try:
        loaded = wm.load(ws)
    except wm.WatermarkError as exc:
        raise _error(exc) from exc
    if loaded is None:
        raise ApiError(404, "watermark_missing", "There is no watermark yet.")
    settings = replace(loaded.settings, **_overrides(req))
    max_edge = req.int_query("max", PREVIEW_MAX_DEFAULT, min=200, max=1600) or PREVIEW_MAX_DEFAULT
    target = (req.query.get("mockup") or "").strip()
    design = (req.query.get("design") or "").strip()
    if not target or not design:
        listing_type = _listing_type(ws)
        if not target:
            used = _run_mockups(ws, listing_type)
            target = used[0] if used else FLAT
        if not design:
            design = _preview_design(ws, listing_type)
    with _preview_lock:
        base = _base_image(ws, target, design, max_edge)
        stamped = wm.apply(base, loaded.mark, settings).convert("RGB")
    buffer = io.BytesIO()
    stamped.save(buffer, "JPEG", quality=88, optimize=True)
    return Response.bytes(buffer.getvalue(), "image/jpeg", {"Cache-Control": "no-store"})
