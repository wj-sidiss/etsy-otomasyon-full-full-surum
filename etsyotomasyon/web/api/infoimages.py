"""Şablon İlan's info images: the pictures every draft ends with (drop.infoimages).

The seller ticks them among the template listing's photos on Etsy, or adds image files
of their own; etsyotomasyon keeps full-size copies in the workspace (info-images/), so runs
need no Etsy call for them and the CLI uses the same pictures.

    GET    /api/info-images
           -> <state>
    GET    /api/info-images/source
           -> {"listing_id": int | null, "problem": null | "no_template",
               "photos": [<photo>], "state": <state>}
    POST   /api/info-images/etsy {"listing_image_ids": [int, ...]}
           -> <state>   the template listing's pictures, downloaded full size, added at
                        the end in this order (one already there is not added again)
    PUT    /api/info-images/files?name=<file name>   the image as the raw body
           -> <state>   a picture of the seller's own, added at the end
    POST   /api/info-images/order {"names": [str, ...]}
           -> <state>   exactly these, in this order; the others are taken out
    DELETE /api/info-images/{name}
           -> <state>

    <state> = {"items": [<item>], "count": int, "max": MAX_INFO_IMAGES,
               "images_max": 20, "mockup_max": int,
               "mockups": {"enabled": int, "used": int, "over_limit": int},
               "unused": [names beyond "max", never sent], "template_listing_id": int | null}
    <item>  = {"name", "alt", "source": "etsy" | "file", "listing_id", "listing_image_id",
               "path": "info-images/<name>" (for /api/files/thumb), "v": mtime,
               "width", "height", "bytes"}
    <photo> = {"listing_image_id", "rank", "url", "thumb", "alt", "width", "height",
               "picked": <item name> | null}

Only the saved template listing's pictures can be taken (the server reads them from
Etsy itself, getListingImages); a picture's bytes come from Etsy's image CDN
(client.download_image), never from an address the page sends.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...config import MAX_LISTING_IMAGES
from ...drop import catalog, infoimages
from ...drop.workspace import INFO_DIR
from ...errors import ValidationError
from .. import files
from ..router import ApiError, Request

if TYPE_CHECKING:  # pragma: no cover
    from ...drop.workspace import Workspace
    from ..context import AppContext
    from ..router import Router

# One change at a time: two quick ticks must not both copy the same picture.
_CHANGE = threading.Lock()
# Pictures downloaded side by side (Etsy's CDN, not the API: no quota is spent).
DOWNLOADS = 4
MAX_ID = 10**13


def register(r: Router, ctx: AppContext) -> None:
    r.get("/api/info-images", get_state)
    r.get("/api/info-images/source", get_source)
    r.post("/api/info-images/etsy", add_from_etsy)
    r.put("/api/info-images/files", upload_file)
    r.post("/api/info-images/order", set_order)
    r.delete("/api/info-images/{name}", delete_image)


# --------------------------------------------------------------------------- helpers


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


def template_listing_id(ws: Workspace) -> int | None:
    """The saved template listing (product.json's source_listing_id), or None."""
    try:
        data = json.loads(ws.template_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get("source_listing_id") if isinstance(data, dict) else None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < MAX_ID:
        return None
    return value


def _size(path: Path) -> tuple[int | None, int | None]:
    from PIL import Image

    try:
        with Image.open(path) as opened:
            return opened.size
    except (OSError, ValueError, Image.DecompressionBombError):
        return None, None


def _item(image: infoimages.InfoImage) -> dict[str, Any]:
    try:
        stat = image.path.stat()
        mtime, size = round(stat.st_mtime, 3), stat.st_size
    except OSError:
        mtime, size = 0.0, 0
    width, height = _size(image.path)
    return {
        "name": image.name,
        "alt": image.alt,
        "source": "etsy" if image.listing_image_id else "file",
        "listing_id": image.listing_id,
        "listing_image_id": image.listing_image_id,
        "path": f"{INFO_DIR}/{image.name}",
        "v": mtime,
        "width": width,
        "height": height,
        "bytes": size,
    }


def state(ws: Workspace) -> dict[str, Any]:
    """What the page shows: the pictures in order, and what they leave the mockups."""
    items = infoimages.load(ws)
    use = catalog.usage(ws, info=len(items))
    return {
        "items": [_item(image) for image in items],
        "count": len(items),
        "max": infoimages.MAX_INFO_IMAGES,
        "images_max": MAX_LISTING_IMAGES,
        "mockup_max": use["max"],
        "mockups": {"enabled": use["enabled"], "used": len(use["used"]),
                    "over_limit": len(use["over_limit"])},
        "unused": [image.name for image in infoimages.unused(ws)],
        "template_listing_id": template_listing_id(ws),
    }


def _too_many(count: int) -> ApiError:
    return ApiError(
        422, "too_many_info_images",
        f"A listing can end with at most {infoimages.MAX_INFO_IMAGES} info images.",
        max=infoimages.MAX_INFO_IMAGES, n=count,
    )


def _photo_id(photo: dict[str, Any]) -> int | None:
    value = photo.get("listing_image_id")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _display_url(photo: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    # The Şablon İlan module's rule for what may reach an <img src> (https only).
    from . import template as template_api

    for key in keys:
        url = template_api._safe_image_url(photo.get(key))
        if url:
            return url
    return None


def _file_name(photo: dict[str, Any], image_id: int) -> str:
    """etsy-<image id>.<ext of the full-size address> (.jpg when it says nothing)."""
    url = str(photo.get("url_fullxfull") or "")
    suffix = Path(url.split("?", 1)[0]).suffix.lower()
    if suffix not in (".jpg", ".jpeg", ".png", ".gif"):
        suffix = ".jpg"
    return f"etsy-{image_id}{suffix}"


def _template_photos(ctx: AppContext, listing_id: int) -> list[dict[str, Any]]:
    client = ctx.client()
    with client.attempts(3):
        photos = client.listing_images(listing_id)
    return [p for p in photos if isinstance(p, dict) and _photo_id(p)]


# --------------------------------------------------------------------------- handlers


def get_state(req: Request) -> dict[str, Any]:
    return state(_ws(req))


def get_source(req: Request) -> dict[str, Any]:
    """The saved template listing's photos, for ticking; none without a template."""
    ctx = _ctx(req)
    ws = _ws(req)
    listing_id = template_listing_id(ws)
    current = state(ws)
    if listing_id is None:
        return {"listing_id": None, "problem": "no_template", "photos": [], "state": current}
    picked = {item["listing_image_id"]: item["name"] for item in current["items"]
              if item["listing_image_id"]}
    photos = []
    for photo in _template_photos(ctx, listing_id):
        image_id = _photo_id(photo)
        alt = photo.get("alt_text")
        photos.append({
            "listing_image_id": image_id,
            "rank": photo.get("rank"),
            "url": _display_url(photo, ("url_570xN", "url_fullxfull", "url_170x135")),
            "thumb": _display_url(photo, ("url_170x135", "url_570xN", "url_75x75")),
            "alt": alt.strip() if isinstance(alt, str) else "",
            "width": photo.get("full_width"),
            "height": photo.get("full_height"),
            "picked": picked.get(image_id),
        })
    return {"listing_id": listing_id, "problem": None, "photos": photos, "state": current}


def _ids(body: dict[str, Any]) -> list[int]:
    value = body.get("listing_image_ids")
    if not isinstance(value, list) or not value:
        raise ApiError(422, "invalid", "listing_image_ids must be a list of picture numbers",
                       field="listing_image_ids")
    out: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or not 0 < item < MAX_ID:
            raise ApiError(422, "invalid", "listing_image_ids must be a list of picture numbers",
                           field="listing_image_ids")
        if item not in out:
            out.append(item)
    if len(out) > infoimages.MAX_INFO_IMAGES:
        raise _too_many(len(out))
    return out


def add_from_etsy(req: Request) -> dict[str, Any]:
    """Copy the template listing's pictures (full size) to the end of the info images."""
    ctx = _ctx(req)
    ids = _ids(req.json_object())
    ws = _ws(req)
    listing_id = template_listing_id(ws)
    if listing_id is None:
        raise ApiError(409, "no_template", "Save a template listing first.")
    with _CHANGE:
        wanted = [i for i in ids if infoimages.find_etsy(ws, i) is None]
        if not wanted:
            return state(ws)
        count = infoimages.count(ws)
        if count + len(wanted) > infoimages.MAX_INFO_IMAGES:
            raise _too_many(count + len(wanted))
        photos = {_photo_id(p): p for p in _template_photos(ctx, listing_id)}
        missing = [i for i in wanted if i not in photos]
        if missing:
            raise ApiError(404, "photo_gone",
                           "That picture is no longer on the template listing.",
                           listing_image_id=missing[0])
        client = ctx.client()

        def fetch(image_id: int) -> bytes:
            url = photos[image_id].get("url_fullxfull")
            with client.attempts(3):
                return client.download_image(str(url or ""))

        # Every picture is fetched before any is kept: nothing changes when one fails.
        try:
            with ThreadPoolExecutor(max_workers=min(DOWNLOADS, len(wanted))) as pool:
                blobs = list(pool.map(fetch, wanted))
        except ValidationError as exc:
            raise ApiError(422, "bad_image", str(exc)) from exc
        for image_id, data in zip(wanted, blobs):
            photo = photos[image_id]
            name = _file_name(photo, image_id)
            try:
                infoimages.add(ws, name, data, alt=str(photo.get("alt_text") or ""),
                               listing_id=listing_id, listing_image_id=image_id)
            except infoimages.TooManyInfoImages as exc:
                raise _too_many(exc.count) from exc
            except catalog.TooManyPixels as exc:
                raise files.too_many_pixels(name, exc.width, exc.height) from exc
            except ValidationError as exc:
                raise ApiError(422, "bad_image", str(exc), name=name) from exc
        return state(ws)


def upload_file(req: Request) -> dict[str, Any]:
    """PUT /api/info-images/files?name=<file name> with the picture as the raw body."""
    ws = _ws(req)
    filename = (req.query.get("name") or "").strip()
    if not filename:
        raise ApiError(422, "invalid", "name is required", field="name")
    safe = infoimages.safe_name(filename)
    from ...drop.workspace import IMAGE_SUFFIXES

    if Path(safe).suffix.lower() not in IMAGE_SUFFIXES:
        raise ApiError(
            422, "bad_type", f"{safe}: info images must be PNG, JPG, WEBP, GIF, BMP or TIFF "
            "pictures.", name=safe,
        )
    if not req.body:
        raise ApiError(422, "bad_image", f"{safe} is empty.", name=safe)
    with _CHANGE:
        try:
            infoimages.add(ws, filename, req.body)
        except infoimages.TooManyInfoImages as exc:
            raise _too_many(exc.count) from exc
        except catalog.TooManyPixels as exc:
            raise files.too_many_pixels(safe, exc.width, exc.height) from exc
        except ValidationError as exc:
            raise ApiError(422, "bad_image", str(exc), name=safe) from exc
        return state(ws)


def set_order(req: Request) -> dict[str, Any]:
    body = req.json_object()
    names = body.get("names")
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ApiError(422, "invalid", "names must be a list of info image names", field="names")
    if len(set(names)) != len(names):
        raise ApiError(422, "invalid", "An info image is listed twice.", field="names")
    if len(names) > infoimages.MAX_INFO_IMAGES:
        raise _too_many(len(names))
    ws = _ws(req)
    with _CHANGE:
        # Pictures past the limit (put in the folder by hand) are not on the page: they
        # stay where they are, after the ones sent, rather than being taken out.
        extra = [image.name for image in infoimages.unused(ws) if image.name not in names]
        try:
            infoimages.set_order(ws, [*names, *extra])
        except FileNotFoundError as exc:
            raise ApiError(404, "not_found", f"No such info image: {exc}", name=str(exc)) from exc
        return state(ws)


def delete_image(req: Request) -> dict[str, Any]:
    ws = _ws(req)
    name = req.params["name"]
    with _CHANGE:
        try:
            infoimages.remove(ws, name)
        except FileNotFoundError as exc:
            raise ApiError(404, "not_found", "No such info image.", name=name) from exc
        return state(ws)
