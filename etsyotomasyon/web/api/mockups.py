"""Mockuplar: the mockup grid, the catalog facts and the print-area editor.

Everything here is local: the mockups are image files in the workspace's
1-MOCKUPS folder, their type/colour/enabled facts live in `mockups.json`
(`drop.catalog`) and their print areas in `positions.json`. No Etsy call is made,
so the screen works before any key is saved.

The editor draws on exactly the image the compositor uses: `GET .../image` turns
the file upright (EXIF), converts it to sRGB and flattens it on white, the same
door `mockup.compose` goes through, and `GET .../preview` runs `mockup.compose`
itself. A rectangle the seller drags therefore lands where the drafts put designs.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import threading
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...config import MAX_LISTING_IMAGES, home_dir
from ...drop import catalog, mockup
from ...drop.workspace import IMAGE_SUFFIXES, MOCKUPS_DIR, PRODUCTS_DIR
from ...errors import ValidationError
from .. import files
from ..router import ApiError, Request, Response

if TYPE_CHECKING:  # pragma: no cover
    from ...drop.workspace import Workspace
    from ..context import AppContext
    from ..router import Router

log = logging.getLogger("etsyotomasyon.web")

SAMPLE = "sample"
SAMPLE_FILE = "sample-design-v1.png"
# Etsy's zoom viewer wants the short side at 2000 px; below 1500 a mockup looks soft.
SMALL_EDGE = 1500
IMAGE_MAX_DEFAULT = 1400
DESIGNS_LIMIT = 300
# How many designs are opened to find a transparent one for the default preview.
DEFAULT_SCAN = 30

_cache_lock = threading.Lock()
_sample_lock = threading.Lock()


def register(r: Router, ctx: AppContext) -> None:
    r.get("/api/mockups", list_mockups)
    r.post("/api/mockups/arrange", arrange_mockups)
    r.put("/api/mockups/files", upload_mockup)
    r.get("/api/mockups/designs", list_designs)
    r.get("/api/mockups/design-image", design_image)
    r.patch("/api/mockups/{name}", patch_mockup)
    r.delete("/api/mockups/{name}", delete_mockup)
    r.get("/api/mockups/{name}/area", get_area)
    r.post("/api/mockups/{name}/area", save_area)
    r.delete("/api/mockups/{name}/area", clear_area)
    r.get("/api/mockups/{name}/image", mockup_image)
    r.get("/api/mockups/{name}/preview", mockup_preview)


# --- helpers -----------------------------------------------------------------------------


def _ctx(req: Request) -> AppContext:
    assert req.ctx is not None
    return req.ctx


def _ws(req: Request) -> Workspace:
    """The open shop's workspace, folders created on first use."""
    ctx = _ctx(req)
    try:
        return ctx.workspace()
    except OSError as exc:
        raise ApiError(
            500, "workspace_unavailable", f"The products folder cannot be used: {exc}",
            path=str(ctx.workspace_root()),
        ) from exc


def _mockup_file(ws: Workspace, name: str) -> Path:
    """The file of mockup `name` in 1-MOCKUPS; 404 for anything else (no paths, no ..)."""
    if not name or name != Path(name).name or name in (".", ".."):
        raise ApiError(404, "not_found", "No such mockup.")
    for path in ws.mockup_files():
        if path.name == name:
            return path
    raise ApiError(404, "not_found", "No such mockup.")


def _version(path: Path) -> str:
    """A cache key for `?v=`: changes whenever the file is replaced or edited."""
    try:
        st = path.stat()
    except OSError:
        return "0"
    return f"{st.st_mtime_ns:x}-{st.st_size:x}"


def _cache_dir() -> Path:
    return home_dir() / "cache" / "mockups"


def _cache_headers(req: Request) -> dict[str, str]:
    # A URL that carries a version (?v=...) may be cached; any other may not.
    return {"Cache-Control": "private, max-age=86400"} if req.query.get("v") else {}


def _cache_key(source: Path, *parts: Any) -> str:
    st = source.stat()
    raw = "|".join([str(source.resolve()), str(st.st_mtime_ns), str(st.st_size), *map(str, parts)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _save_atomic(image: Any, target: Path, fmt: str, **options: Any) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.stem}.{threading.get_ident()}.tmp")
    image.save(tmp, format=fmt, **options)
    with _cache_lock:
        os.replace(tmp, target)
    return target


def _shrink(image: Any, max_edge: int) -> Any:
    from PIL import Image

    if max(image.size) <= max_edge:
        return image
    factor = max_edge / max(image.size)
    size = (max(1, round(image.width * factor)), max(1, round(image.height * factor)))
    return image.resize(size, Image.LANCZOS)


def _unreadable(name: str, exc: Exception) -> ApiError:
    return ApiError(422, "bad_image", f"{name} cannot be opened as an image ({exc}).", name=name)


def display_image(source: Path, max_edge: int) -> Path:
    """The mockup as the compositor sees it (upright, sRGB, on white), as a cached JPEG.

    The editor draws the print area on this picture, so its shape is exactly the
    shape the fractions are measured on.
    """
    from PIL import Image

    key = _cache_key(source, max_edge, "display")
    target = _cache_dir() / f"{key}.jpg"
    if target.is_file():
        return target
    dims: list[tuple[int, int]] = [(0, 0)]
    with files.decoding(source.name, lambda: dims[0]):
        try:
            with Image.open(source) as opened:
                dims[0] = opened.size
                files.check_decodable(source.name, opened.size)
                image = mockup.flatten_onto(mockup._as_displayed(opened), mockup.WHITE)
        except (OSError, ValueError) as exc:
            raise _unreadable(source.name, exc) from exc
        return _save_atomic(_shrink(image, max_edge), target, "JPEG", quality=88, optimize=True)


def design_png(source: Path, max_edge: int) -> Path:
    """A design, upright and in sRGB with its transparency kept, as a cached PNG."""
    from PIL import Image

    key = _cache_key(source, max_edge, "design")
    target = _cache_dir() / f"{key}.png"
    if target.is_file():
        return target
    dims: list[tuple[int, int]] = [(0, 0)]
    with files.decoding(source.name, lambda: dims[0]):
        try:
            with Image.open(source) as opened:
                dims[0] = opened.size
                files.check_decodable(source.name, opened.size)
                image = mockup._as_displayed(opened).convert("RGBA")
        except (OSError, ValueError) as exc:
            raise _unreadable(source.name, exc) from exc
        return _save_atomic(_shrink(image, max_edge), target, "PNG", optimize=False)


def sample_design() -> Path:
    """A bundled sample design (a sunset badge on a transparent ground), drawn once.

    It lets the editor show a preview before the seller has any design of their own.
    """
    target = _cache_dir() / SAMPLE_FILE
    if target.is_file():
        return target
    with _sample_lock:
        if target.is_file():
            return target
        _save_atomic(_draw_sample(), target, "PNG", optimize=True)
    return target


def _draw_sample(size: int = 1200) -> Any:
    from PIL import Image, ImageDraw

    scale = 3  # drawn large, then scaled down: smooth edges without anti-aliasing support
    big = size * scale
    art = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    centre, radius = big / 2, big * 0.46

    sky = Image.new("RGBA", (big, big))
    top, mid, low = (255, 214, 140), (246, 136, 110), (132, 82, 150)
    draw = ImageDraw.Draw(sky)
    for y in range(big):
        t = y / (big - 1)
        a, b, f = (top, mid, t / 0.55) if t < 0.55 else (mid, low, (t - 0.55) / 0.45)
        draw.line([(0, y), (big, y)], fill=(*[round(a[i] + (b[i] - a[i]) * f) for i in range(3)], 255))
    scene = ImageDraw.Draw(sky)
    sun_r = big * 0.2
    scene.ellipse(
        [centre - sun_r, big * 0.5 - sun_r, centre + sun_r, big * 0.5 + sun_r],
        fill=(255, 238, 190, 255),
    )
    ranges = (
        ((120, 70, 128), ((0, 0.7), (0.12, 0.62), (0.24, 0.68), (0.38, 0.54), (0.52, 0.66),
                          (0.66, 0.57), (0.8, 0.67), (0.92, 0.6), (1, 0.66))),
        ((84, 48, 102), ((0, 0.8), (0.15, 0.72), (0.3, 0.79), (0.45, 0.69), (0.6, 0.8),
                         (0.75, 0.73), (0.9, 0.8), (1, 0.76))),
    )
    for colour, ridge in ranges:
        points = [(big * fx, big * fy) for fx, fy in ridge] + [(big, big), (0, big)]
        scene.polygon(points, fill=(*colour, 255))
    for bx, by in ((0.36, 0.3), (0.42, 0.26), (0.62, 0.33)):
        w = big * 0.03
        x, y = big * bx, big * by
        scene.line([(x - w, y - w * 0.4), (x, y), (x + w, y - w * 0.4)], fill=(96, 56, 110, 255),
                   width=max(3, big // 220))

    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).ellipse(
        [centre - radius, centre - radius, centre + radius, centre + radius], fill=255
    )
    art.paste(sky, (0, 0), mask)
    ring = ImageDraw.Draw(art)
    width = round(big * 0.018)
    ring.ellipse(
        [centre - radius, centre - radius, centre + radius, centre + radius],
        outline=(72, 40, 92, 255), width=width,
    )
    return art.resize((size, size), Image.LANCZOS)


# --- listing -----------------------------------------------------------------------------


def _items(ws: Workspace) -> dict[str, Any]:
    """The grid: every mockup in the seller's order, and which ones drafts use.

    items[i]["position"] is the image number a draft gives it (1 = main image) or None;
    "over_limit" marks a switched-on mockup that does not fit (beyond "max_enabled":
    MAX_ENABLED less one per info image, catalog.usage).
    "default_area" counts the mockups still on the default print area, with the first
    one to fix (in order, preferring one that drafts use) for the page's banner.
    """
    paths = {p.name: p for p in ws.mockup_files()}
    infos = catalog.load(ws)
    sizes = mockup.mockup_sizes(list(paths.values()))
    positions_error = None
    try:
        areas = catalog.effective_areas(ws, sizes=sizes)
    except ValidationError as exc:
        positions_error = str(exc)
        areas = {}
    orphans: list[str] = []
    if positions_error is None:
        try:
            stored = mockup.load_positions(ws.positions_path)
            orphans = sorted(set(stored) - set(paths))
        except ValidationError:
            orphans = []
    per_size = Counter(sizes.values())
    use = catalog.usage(ws, infos)
    position = {name: n for n, name in enumerate(use["used"], 1)}
    over = set(use["over_limit"])
    items = []
    for index, (name, info) in enumerate(infos.items()):
        path = paths[name]
        size = sizes.get(name)
        area, source = areas.get(name, (mockup.DEFAULT_PRINT_AREA, catalog.SOURCE_DEFAULT))
        items.append({
            "name": name,
            "path": f"{MOCKUPS_DIR}/{name}",
            "version": _version(path),
            "type": info.type,
            "color": info.color,
            "enabled": info.enabled,
            "in_use": name in position,
            "position": position.get(name),
            "over_limit": name in over,
            "order": index,
            "width": size[0] if size else None,
            "height": size[1] if size else None,
            "small": bool(size) and min(size) < SMALL_EDGE,
            "area": area.to_dict(),
            "area_source": source,
            "same_size_count": per_size[size] - 1 if size else 0,
        })
    return {
        "items": items,
        "types": dict(Counter(item["type"] for item in items)),
        "counts": {"total": use["total"], "enabled": use["enabled"], "in_use": len(use["used"])},
        "usage": use,
        # 19 (Etsy's 20 pictures less the flat design), less one per info image.
        "max_enabled": use["max"],
        "info_images": use["info"],
        "images_max": MAX_LISTING_IMAGES,
        "limit_note": (
            {"enabled": use["enabled"], "max": use["max"]} if use["over_limit"] else None
        ),
        "default_area": _default_area(items, sizes),
        "positions_error": positions_error,
        "orphans": orphans,
        "type_order": list(catalog.TYPES),
    }


def _default_area(items: list[dict[str, Any]], sizes: dict[str, tuple[int, int]]) -> dict[str, Any]:
    """How many mockups still put designs in the default (centred) area, and where to start.

    `first` is the first such mockup in order that drafts use (else the first of all);
    `first_same_size` is how many of the others share its pixel size, so one save in the
    editor with "apply to same-size mockups" fixes them together.
    """
    waiting = [item for item in items if item["area_source"] == catalog.SOURCE_DEFAULT]
    if not waiting:
        return {"count": 0, "used": 0, "first": None, "first_same_size": 0}
    first = next((item for item in waiting if item["in_use"]), waiting[0])
    size = sizes.get(first["name"])
    same = sum(1 for item in waiting if item is not first and size and sizes.get(item["name"]) == size)
    return {
        "count": len(waiting),
        "used": sum(1 for item in waiting if item["in_use"]),
        "first": first["name"],
        "first_same_size": same,
    }


def _item(ws: Workspace, name: str) -> dict[str, Any]:
    for item in _items(ws)["items"]:
        if item["name"] == name:
            return item
    raise ApiError(404, "not_found", "No such mockup.")


def list_mockups(req: Request) -> dict[str, Any]:
    return _items(_ws(req))


# --- changes -----------------------------------------------------------------------------


def upload_mockup(req: Request) -> dict[str, Any]:
    """PUT /api/mockups/files?name=<file name> with the image as the raw body."""
    ws = _ws(req)
    filename = (req.query.get("name") or "").strip()
    if not filename:
        raise ApiError(422, "invalid", "name is required", field="name")
    safe = catalog.safe_name(filename)
    if Path(safe).suffix not in IMAGE_SUFFIXES:
        raise ApiError(
            422, "bad_type", f"{safe}: mockups must be PNG, JPG, WEBP, GIF, BMP or TIFF images.",
            name=safe,
        )
    if not req.body:
        raise ApiError(422, "bad_image", f"{safe} is empty.", name=safe)
    try:
        name = catalog.add(ws, filename, req.body)
    except catalog.TooManyPixels as exc:
        raise files.too_many_pixels(safe, exc.width, exc.height) from exc
    except ValidationError as exc:
        raise ApiError(422, "bad_image", str(exc), name=safe) from exc
    _ctx(req).set_status_soon()
    return {"name": name, "item": _item(ws, name)}


def _names(body: dict[str, Any], field: str) -> list[str] | None:
    if field not in body or body[field] is None:
        return None
    value = body[field]
    if not isinstance(value, list) or not all(isinstance(n, str) for n in value):
        raise ApiError(422, "invalid", f"{field} must be a list of mockup names", field=field)
    return value


def arrange_mockups(req: Request) -> dict[str, Any]:
    """POST /api/mockups/arrange {order?: [names], enabled?: [names]} -> the grid.

    `order` is first (main image) to last; `enabled` is exactly the set drafts may use
    (every other mockup is switched off). One write for the whole selection, so 37
    mockups are chosen in one go. A name that is not a mockup: 404, nothing saved.
    """
    ws = _ws(req)
    body = req.json_object()
    order = _names(body, "order")
    enabled = _names(body, "enabled")
    if order is None and enabled is None:
        raise ApiError(422, "invalid", "Send order and/or enabled.")
    try:
        catalog.arrange(ws, order=order, enabled=enabled)
    except FileNotFoundError as exc:
        raise ApiError(404, "not_found", f"No such mockup: {exc}", name=str(exc)) from exc
    except ValidationError as exc:
        raise ApiError(422, "invalid", str(exc)) from exc
    _ctx(req).set_status_soon()
    return _items(ws)


def patch_mockup(req: Request) -> dict[str, Any]:
    """PATCH /api/mockups/{name} {type?, color?, enabled?}."""
    ws = _ws(req)
    name = req.params["name"]
    _mockup_file(ws, name)
    body = req.json_object()
    changes: dict[str, Any] = {}
    if "type" in body:
        if body["type"] not in catalog.TYPES:
            raise ApiError(422, "invalid", f"type must be one of {', '.join(catalog.TYPES)}",
                           field="type")
        changes["type"] = body["type"]
    if "color" in body:
        if not isinstance(body["color"], str):
            raise ApiError(422, "invalid", "color must be text", field="color")
        changes["color"] = body["color"]
    if "enabled" in body:
        if not isinstance(body["enabled"], bool):
            raise ApiError(422, "invalid", "enabled must be true or false", field="enabled")
        changes["enabled"] = body["enabled"]
    if not changes:
        raise ApiError(422, "invalid", "Nothing to change: send type, color or enabled.")
    catalog.update(ws, name, **changes)
    return {"item": _item(ws, name)}


def delete_mockup(req: Request) -> dict[str, Any]:
    ws = _ws(req)
    name = req.params["name"]
    _mockup_file(ws, name)
    try:
        catalog.remove(ws, name)
    except FileNotFoundError as exc:
        raise ApiError(404, "not_found", "No such mockup.") from exc
    except OSError as exc:
        raise ApiError(409, "file_locked", f"{name} could not be deleted: {exc}", name=name) from exc
    _ctx(req).set_status_soon()
    return {"removed": name}


# --- print areas -------------------------------------------------------------------------


def _area_payload(ws: Workspace, name: str) -> dict[str, Any]:
    """The editor's view of one mockup's print area.

    "area" is what is stored or borrowed (x/y/w/h, plus quad/realism/curve when set);
    "style" is what a render uses: realism and curve with the type defaults filled in,
    the defaults themselves, and whether the curve is offered for this type.
    """
    item = _item(ws, name)
    siblings = catalog.same_size_names(ws, name)
    area, _source = catalog.effective_area(ws, name)
    kind = item["type"]
    used = mockup.styled(area, kind)
    return {
        "name": name,
        "width": item["width"],
        "height": item["height"],
        "area": item["area"],
        "source": item["area_source"],
        "default_area": mockup.DEFAULT_PRINT_AREA.to_dict(),
        "same_size": [n for n in siblings if n != name],
        "style": {
            "kind": kind,
            "realism": used.realism,
            "curve": used.curve,
            "realism_default": mockup.REALISM_DEFAULTS.get(kind, 0),
            "curve_default": mockup.CURVE_DEFAULTS.get(kind, 0),
            "curve_offered": kind in mockup.CURVE_TYPES or bool(used.curve),
        },
    }


def get_area(req: Request) -> dict[str, Any]:
    ws = _ws(req)
    name = req.params["name"]
    _mockup_file(ws, name)
    return _area_payload(ws, name)


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ApiError(422, "invalid", f"{field} must be a number", field=field)
    try:
        number = float(value)
    except ValueError as exc:
        raise ApiError(422, "invalid", f"{field} must be a number", field=field) from exc
    if not math.isfinite(number):
        raise ApiError(422, "invalid", f"{field} must be a number", field=field)
    return number


def _level(value: Any, field: str) -> int | None:
    """A 0-100 setting (realism, curve): None when absent or null (the type default)."""
    if value is None or value == "":
        return None
    number = _number(value, field)
    if not 0 <= number <= 100:
        raise ApiError(422, "invalid", f"{field} must be between 0 and 100", field=field)
    return int(round(number))


def _quad(raw: Any) -> list[list[float]]:
    """Four [x, y] corners from a JSON list, or from a query's eight comma-separated numbers."""
    if isinstance(raw, str):
        parts = [part for part in raw.split(",") if part.strip()]
        if len(parts) != 8:
            raise ApiError(422, "invalid", "quad must be four x,y corners", field="quad")
        numbers = [_number(part.strip(), "quad") for part in parts]
        return [[numbers[i], numbers[i + 1]] for i in range(0, 8, 2)]
    if not isinstance(raw, list) or len(raw) != 4:
        raise ApiError(422, "invalid", "quad must be four [x, y] corners", field="quad")
    corners = []
    for point in raw:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ApiError(422, "invalid", "quad must be four [x, y] corners", field="quad")
        corners.append([_number(point[0], "quad"), _number(point[1], "quad")])
    return corners


def _area(values: Any, *, style_from: mockup.PrintArea | None = None) -> mockup.PrintArea:
    """A PrintArea from x, y, w, h fractions - or four corners in "quad" - with its
    realism and curve (422 invalid when they do not make one).

    A realism or curve that is not given comes from `style_from` (the saved area a
    preview starts from); without one it is None, the mockup type's default.
    """
    if "realism" in values:
        realism = _level(values.get("realism"), "realism")
    else:
        realism = style_from.realism if style_from is not None else None
    if "curve" in values:
        curve = _level(values.get("curve"), "curve")
    else:
        curve = style_from.curve if style_from is not None else None
    if values.get("quad") not in (None, ""):
        corners = [[round(v, 5) for v in point] for point in _quad(values.get("quad"))]
        try:
            return mockup.PrintArea.from_quad(corners, realism=realism, curve=curve)
        except ValidationError as exc:
            raise ApiError(422, "bad_area", str(exc)) from exc
    x, y, w, h = (_number(values.get(k), k) for k in ("x", "y", "w", "h"))
    x, y, w, h = (round(v, 5) for v in (x, y, w, h))
    # Rounding may push the far edge a hair past 1; pull it back rather than refuse.
    if 1.0 < x + w <= 1.0001:
        w = round(1.0 - x, 5)
    if 1.0 < y + h <= 1.0001:
        h = round(1.0 - y, 5)
    try:
        return mockup.PrintArea(x, y, w, h, realism=realism, curve=curve)
    except ValidationError as exc:
        raise ApiError(422, "bad_area", str(exc)) from exc


def save_area(req: Request) -> dict[str, Any]:
    """POST /api/mockups/{name}/area -> {applied_to: [names]}.

    Body: {x, y, w, h} or {quad: [[x, y] x4]} (top-left, top-right, bottom-right,
    bottom-left), optional realism and curve (0-100, null for the type default), and
    same_size, which gives every mockup of this size all of it.
    """
    ws = _ws(req)
    name = req.params["name"]
    _mockup_file(ws, name)
    body = req.json_object()
    area = _area(body)
    same_size = body.get("same_size", False)
    if not isinstance(same_size, bool):
        raise ApiError(422, "invalid", "same_size must be true or false", field="same_size")
    applied = catalog.save_area(ws, name, area, same_size=same_size)
    _ctx(req).set_status_soon()
    payload = _area_payload(ws, name)
    payload["applied_to"] = applied
    return payload


def clear_area(req: Request) -> dict[str, Any]:
    """DELETE /api/mockups/{name}/area: back to the shared or default rectangle."""
    ws = _ws(req)
    name = req.params["name"]
    _mockup_file(ws, name)
    cleared = catalog.clear_area(ws, name)
    _ctx(req).set_status_soon()
    payload = _area_payload(ws, name)
    payload["cleared"] = cleared
    return payload


# --- images ------------------------------------------------------------------------------


def mockup_image(req: Request) -> Response:
    """GET /api/mockups/{name}/image?max=1400: the upright, flattened mockup as JPEG."""
    ws = _ws(req)
    path = _mockup_file(ws, req.params["name"])
    max_edge = req.int_query("max", IMAGE_MAX_DEFAULT, min=200, max=2400) or IMAGE_MAX_DEFAULT
    return Response.file(display_image(path, max_edge), "image/jpeg", _cache_headers(req))


def _design_path(ws: Workspace, design: str) -> Path:
    """`sample`, or a design file inside 2-PRODUCTS (given relative to that folder)."""
    design = (design or SAMPLE).strip()
    if design == SAMPLE:
        return sample_design()
    rel = design.replace("\\", "/")
    if rel.startswith(PRODUCTS_DIR + "/"):
        rel = rel[len(PRODUCTS_DIR) + 1 :]
    target = files.resolve_inside(ws.products, rel)
    if target is None or target.suffix.lower() not in IMAGE_SUFFIXES or not target.is_file():
        raise ApiError(404, "not_found", "No such design in the products folder.")
    return target


def design_image(req: Request) -> Response:
    """GET /api/mockups/design-image?design=<sample|rel>&max=800: the design as a PNG."""
    ws = _ws(req)
    source = _design_path(ws, req.query.get("design", SAMPLE))
    max_edge = req.int_query("max", 800, min=64, max=2000) or 800
    return Response.file(design_png(source, max_edge), "image/png", _cache_headers(req))


def mockup_preview(req: Request) -> Response:
    """GET /api/mockups/{name}/preview?design=&x=&y=&w=&h=&max=: a real composite.

    `quad=` (eight numbers: four x,y corners) in place of x/y/w/h gives four corners;
    `realism=` and `curve=` (0-100) override the saved ones. `mockup.compose` places
    the design exactly as the drafts do (ratio kept, centred, the mockup type's
    realism and curve). Both inputs are the cached screen-sized copies, so a preview
    takes a fraction of a full-resolution render.
    """
    ws = _ws(req)
    name = req.params["name"]
    path = _mockup_file(ws, name)
    saved = catalog.effective_area(ws, name)[0]
    if any(k in req.query for k in ("x", "y", "w", "h", "quad")):
        area = _area(req.query, style_from=saved)
    elif any(k in req.query for k in ("realism", "curve")):
        area = _area({**saved.to_dict(), **dict(req.query)}, style_from=saved)
    else:
        area = saved
    max_edge = req.int_query("max", IMAGE_MAX_DEFAULT, min=200, max=2000) or IMAGE_MAX_DEFAULT
    source = _design_path(ws, req.query.get("design", SAMPLE))
    base = display_image(path, max_edge)
    art = design_png(source, max_edge)
    out = _cache_dir() / f"preview-{threading.get_ident()}.jpg"
    kind = catalog.kind_of(catalog.load(ws), name)
    try:
        mockup.compose(art, base, out, area=area, min_edge=0, kind=kind)
        data = out.read_bytes()
    finally:
        try:
            out.unlink()
        except OSError:
            pass
    return Response.bytes(data, "image/jpeg", {"Cache-Control": "no-store"})


# --- designs for the preview ------------------------------------------------------------------


_WORDS = re.compile(r"[-_\s]+")


def _label(stem: str) -> str:
    words = [w for w in _WORDS.split(stem) if w]
    return " ".join(w[:1].upper() + w[1:] for w in words) or stem


def _has_alpha(path: Path) -> bool:
    """Whether the file declares transparency (header only; no pixels decoded)."""
    from PIL import Image

    try:
        with Image.open(path) as image:
            return image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info
    except (OSError, ValueError, Image.DecompressionBombError):
        return False


def default_design(ws: Workspace) -> str:
    """The design a preview starts with: the first transparent one in 2-PRODUCTS (of the
    first DEFAULT_SCAN), else the bundled sample."""
    for path in ws.product_files()[:DEFAULT_SCAN]:
        if _has_alpha(path):
            return path.relative_to(ws.products).as_posix()
    return SAMPLE


def list_designs(req: Request) -> dict[str, Any]:
    """GET /api/mockups/designs: loose designs in 2-PRODUCTS to preview on a mockup."""
    ws = _ws(req)
    items = []
    default = SAMPLE
    for n, path in enumerate(ws.product_files()[:DESIGNS_LIMIT]):
        rel = path.relative_to(ws.products).as_posix()
        transparent = _has_alpha(path) if n < DEFAULT_SCAN else None
        if transparent and default == SAMPLE:
            default = rel
        items.append({
            "id": rel,
            "name": path.name,
            "label": _label(path.stem),
            "path": f"{PRODUCTS_DIR}/{rel}",
            "version": _version(path),
            "transparent": transparent,
        })
    return {"items": items, "default": default, "sample": SAMPLE}
