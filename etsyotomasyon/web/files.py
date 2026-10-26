"""Serving files safely: static assets, workspace images and their thumbnails.

`resolve_inside(root, rel)` is the one gate: it returns a path only when the
resolved target lies inside `root`, so "..", absolute paths, drive letters and
symlinks pointing out of the folder all end in "not found".
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from .router import ApiError

# Hard-coded on purpose: `mimetypes` reads the Windows registry, where .js is often
# text/plain — and a module script served as text/plain with nosniff never runs.
MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
    # Workspace images the drop folder accepts; browsers show some of them.
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}
DEFAULT_TYPE = "application/octet-stream"

THUMB_MIN, THUMB_MAX = 64, 1600
_thumb_lock = threading.Lock()


def content_type_for(path: Path | str) -> str:
    return MIME.get(Path(path).suffix.lower(), DEFAULT_TYPE)


def resolve_inside(root: Path, rel: str, *, follow_links: bool = True) -> Path | None:
    """`root / rel` if that is a path inside `root`, else None. Never raises.

    "..", empty and drive-like segments are refused outright. With follow_links
    (the default, for the user's folders) symlinks are resolved and must still end
    inside `root`. follow_links=False is for the app's own static files: a frozen
    macOS app may ship them as symlinks into Contents/Resources, which are ours.
    """
    if not rel or "\x00" in rel:
        return None
    rel = rel.replace("\\", "/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." or ":" in p for p in parts):
        return None
    try:
        base = root.resolve()
        target = base.joinpath(*parts)
        if follow_links:
            target = target.resolve()
        target.relative_to(base)
    except (OSError, ValueError, RuntimeError):
        return None
    return target


def workspace_image(root: Path, rel: str) -> Path | None:
    """An existing image file inside the workspace root, or None."""
    from ..drop.workspace import IMAGE_SUFFIXES

    target = resolve_inside(root, rel)
    if target is None or target.suffix.lower() not in IMAGE_SUFFIXES or not target.is_file():
        return None
    return target


def too_many_pixels(name: str, width: int, height: int) -> ApiError:
    """422 too_many_pixels: the image is too large to decode safely (see catalog limits)."""
    from ..drop import catalog

    return ApiError(
        422, "too_many_pixels",
        f"{name} is {width}x{height} px. Images can be at most {catalog.MAX_EDGE} px on a "
        f"side and {catalog.MAX_PIXELS // 1_000_000} million pixels.",
        name=name, width=width, height=height, max_edge=catalog.MAX_EDGE,
        max_mp=catalog.MAX_PIXELS // 1_000_000,
    )


@contextmanager
def decoding(name: str, size: Callable[[], tuple[int, int]]) -> Iterator[None]:
    """Turn "this image is too big to decode" into 422 too_many_pixels, never a 500.

    Covers Pillow's own bomb refusal and a MemoryError while decoding; `size()` gives
    the dimensions for the message (it is only asked when something went wrong).
    """
    from PIL import Image

    from ..drop import catalog

    try:
        yield
    except Image.DecompressionBombError as exc:
        raise too_many_pixels(name, *catalog.bomb_size(exc)) from exc
    except MemoryError as exc:
        try:
            width, height = size()
        except Exception:  # noqa: BLE001 — only for the message
            width, height = 0, 0
        raise too_many_pixels(name, width, height) from exc


def check_decodable(name: str, size: tuple[int, int]) -> None:
    """ApiError 422 too_many_pixels before decoding an image over the pixel limits."""
    from ..drop import catalog

    if catalog.too_many_pixels(size):
        raise too_many_pixels(name, *size)


def thumbnail(source: Path, width: int, cache: Path, *, keep_alpha: bool = False) -> Path:
    """A JPEG of `source` at most `width` px wide, upright, sRGB, flattened on white.

    keep_alpha: a PNG that keeps the transparency instead (a design the page shows on
    its checkerboard). Cached under `cache`, keyed by path + mtime + size + width (+ the
    kind), so an edited file gets a new thumbnail and an unchanged one is rendered once.
    An image too large to decode safely raises ApiError 422 too_many_pixels (never a
    MemoryError / 500).
    """
    from PIL import Image

    from ..drop import mockup

    width = max(THUMB_MIN, min(THUMB_MAX, int(width)))
    stat = source.stat()
    kind = "|alpha" if keep_alpha else ""
    key = hashlib.sha256(
        f"{source.resolve()}|{stat.st_mtime_ns}|{stat.st_size}|{width}{kind}".encode()
    ).hexdigest()[:32]
    target = cache / f"{key}.{'png' if keep_alpha else 'jpg'}"
    if target.is_file():
        return target
    dims: list[tuple[int, int]] = [(0, 0)]
    with decoding(source.name, lambda: dims[0]), Image.open(source) as opened:
        dims[0] = opened.size
        if opened.format == "JPEG":
            # Let libjpeg decode at a fraction of the size when that is plenty.
            opened.draft("RGB", (width, width * 4))
        # After draft() the size is what will really be decoded: a huge JPEG can
        # still get a thumbnail, a huge PNG cannot.
        check_decodable(source.name, opened.size)
        image = mockup._as_displayed(opened)
        if keep_alpha:
            image = image.convert("RGBA")
        else:
            image = mockup.flatten_onto(image, mockup.WHITE)
        if image.width > width:
            height = max(1, round(image.height * width / image.width))
            image = image.resize((width, height), Image.LANCZOS)
        cache.mkdir(parents=True, exist_ok=True)
        tmp = cache / f"{key}.{threading.get_ident()}.tmp"
        if keep_alpha:
            image.save(tmp, format="PNG", optimize=True)
        else:
            image.save(tmp, format="JPEG", quality=85, optimize=True)
    with _thumb_lock:
        tmp.replace(target)
    return target


def open_path(path: Path) -> None:
    """Show a folder or file in the system's own file manager."""
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 — a local folder of the user's own
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])
