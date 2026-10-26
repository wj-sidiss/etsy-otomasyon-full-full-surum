"""Ayarlar: the products folder and the setup checklist.

    GET  /api/settings             version, licence, repository, the folders
    POST /api/settings/workspace   {"path": "<absolute folder>" | ""} ("" = the default)
    POST /api/settings/doctor      run the setup checklist (`etsyotomasyon doctor`) now

A folder of a workspace (1-MOCKUPS, 2-PRODUCTS, 3-DRAFTS, anything inside them) chosen
as the workspace means that workspace: its root is saved, never a second workspace
nested inside the first (`drop.workspace.root_for`). The answer then carries
`adjusted: {"chosen", "root", "inside"}` so the page can say so (`inside` false: the
folder is only named like one, e.g. a new "...\\2-PRODUCTS", and its parent was used).
A folder saved that way by an older version is reported as `workspace.nested_in` (the
root it belongs to); switching to that root carries the nested workspace's template and
upload history over (`drop.workspace.adopt_nested`), listed in `migrated`.

Language, hiding shop names, the shop list and quitting use the core endpoints
(/api/prefs, /api/shops/*, /api/quit); the Etsy connection uses /api/connect/*.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...desktop import settings as desktop_settings
from ...errors import EtsyApiError, EtsyotomasyonError
from ..router import ApiError, Request

if TYPE_CHECKING:  # pragma: no cover
    from ..context import AppContext
    from ..router import Router

LICENSE = "MIT"
REPO_URL = "https://github.com/wj-sidiss/etsy-otomasyon-full-full-surum"
# setup.build_steps numbers its steps; the page words each one by this key.
STEP_KEYS = {
    1: "python", 2: "installed", 3: "shop", 4: "app", 5: "credentials", 6: "redirect",
    7: "callback", 8: "api", 9: "connected", 10: "workspace",
}


def register(r: Router, ctx: AppContext) -> None:
    r.get("/api/settings", overview)
    r.post("/api/settings/workspace", set_workspace)
    r.post("/api/settings/doctor", doctor)


def _ctx(req: Request) -> AppContext:
    assert req.ctx is not None
    return req.ctx


def _same_path(a: Path, b: Path) -> bool:
    """The same folder as far as the path says (case and separators as the OS sees them)."""
    return os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(os.path.normpath(str(b)))


def _folders(ctx: AppContext) -> dict[str, Any]:
    from ...drop import workspace as workspace_mod

    root = ctx.workspace_root()
    ws = workspace_mod.Workspace(root)
    paths = {"workspace": ws.root, "mockups": ws.mockups, "products": ws.products,
             "drafts": ws.drafts}
    # Saved before the guard: a folder of another, existing workspace (see the module
    # doc). A workspace merely named 2-PRODUCTS, with none around it, is its own.
    belongs_to = workspace_mod.enclosing_root(root) if root.is_absolute() else None
    return {
        "root": str(root),
        "default": str(workspace_mod.default_root()),
        "custom": bool(str(ctx.shop_prefs().get("workspace") or "").strip()),
        "nested_in": (None if belongs_to is None or _same_path(belongs_to, root)
                      else str(belongs_to)),
        "folders": [
            {"which": which, "name": path.name, "path": str(path), "exists": path.is_dir()}
            for which, path in paths.items()
        ],
    }


def overview(req: Request) -> dict[str, Any]:
    ctx = _ctx(req)
    return {
        "version": ctx.version,
        "license": LICENSE,
        "repo": REPO_URL,
        "env_file": str(desktop_settings.env_path()),
        "workspace": _folders(ctx),
    }


def set_workspace(req: Request) -> dict[str, Any]:
    """Use another products folder for the open shop (created, with its subfolders).

    A folder of a workspace means that workspace (module doc); the default folder,
    however it is reached, is saved as "the default" rather than as a custom path.
    """
    from ...drop import automation
    from ...drop import workspace as workspace_mod
    from ...errors import ValidationError

    ctx = _ctx(req)
    body = req.json_object()
    raw = body.get("path")
    if not isinstance(raw, str):
        raise ApiError(422, "invalid", "path must be text", field="path")
    raw = raw.strip().strip('"').strip("'").strip()
    if ctx.jobs.busy():
        raise ApiError(409, "busy", "Wait for the running task to finish first.")
    adjusted: dict[str, Any] | None = None
    migrated: list[str] = []
    # The folder in use now: a workspace nested by an older version hands its template
    # and upload history over to the workspace around it (workspace_mod.adopt_nested).
    current = ctx.workspace_root()
    if not raw:
        path = workspace_mod.default_root()
    else:
        chosen = Path(raw).expanduser()
        if not chosen.is_absolute():
            raise ApiError(422, "workspace_not_absolute", "Give the full path of a folder.",
                           field="path")
        if chosen.exists() and not chosen.is_dir():
            raise ApiError(422, "workspace_not_folder", "That is a file, not a folder.",
                           field="path")
        path = workspace_mod.root_for(chosen)
        if not _same_path(path, chosen):
            # inside: `chosen` is a folder of an existing workspace; otherwise it is only
            # named like one (a new "...\2-PRODUCTS"), and its parent was made the workspace.
            inside = workspace_mod.enclosing_root(chosen) is not None
            adjusted = {"chosen": str(chosen), "root": str(path), "inside": inside}
        try:
            workspace_mod.Workspace(path).create()
        except OSError as exc:
            raise ApiError(422, "workspace_unwritable",
                           f"Cannot create the folders there: {exc.strerror or exc}",
                           field="path") from exc
    outer = workspace_mod.enclosing_root(current) if current.is_absolute() else None
    if (outer is not None and _same_path(outer, path) and not _same_path(current, path)
            and workspace_mod.is_workspace(current)):
        try:
            migrated = workspace_mod.adopt_nested(current, path)
        except automation.UploadLocked as exc:
            raise ApiError(409, "workspace_locked", str(exc)) from exc
        except ValidationError as exc:
            raise ApiError(422, "workspace_history", str(exc)) from exc
    if not raw:
        ctx.update_shop_prefs(workspace=None)
    else:
        default = _same_path(path, workspace_mod.default_root())
        ctx.update_shop_prefs(workspace=None if default else str(path))
    try:
        ctx.workspace()
    except OSError as exc:
        raise ApiError(422, "workspace_unwritable",
                       f"Cannot create the folders there: {exc.strerror or exc}",
                       field="path") from exc
    ctx.set_status_soon(0.0)
    return {"workspace": _folders(ctx), "adjusted": adjusted, "migrated": migrated}


def _check_api(ctx: AppContext) -> Any:
    """Step 8 through the app's own client (its rate limit, one quick try)."""
    from ... import setup

    try:
        client = ctx.client(require_auth=False)
    except ApiError:
        return setup.StepResult(setup.MISSING, "no credentials to test with",
                                ["Finish step 5 first."])
    try:
        with client.attempts(1):
            client.ping()
    except EtsyApiError as exc:
        if exc.status == 0:
            return setup.StepResult(setup.WARN, f"Etsy cannot be reached right now: {exc.message}")
        return setup.StepResult(setup.MISSING, str(exc).splitlines()[0],
                                ["Check BOTH halves on your app page at "
                                 "https://www.etsy.com/developers/your-apps"])
    return setup.StepResult(setup.OK, "Etsy accepted your keystring and shared secret")


def doctor(req: Request) -> dict[str, Any]:
    """setup.build_steps, checked now. Steps only the person can answer stay "unknown"."""
    from ... import setup

    ctx = _ctx(req)
    results = []
    items = []
    for step in setup.build_steps(ctx.workspace_root()):
        if step.check is setup.check_api_reachable:
            result = _check_api(ctx)
        else:
            try:
                result = step.check()
            except (EtsyotomasyonError, OSError) as exc:
                result = setup.StepResult(setup.MISSING, str(exc).splitlines()[0] if str(exc) else "")
        results.append((step, result))
        items.append({
            "number": step.number,
            "key": STEP_KEYS.get(step.number, str(step.number)),
            "title": step.title,
            "state": result.state,
            "detail": result.detail,
            "fix": list(result.fix),
            "required": step.required,
            "question": step.question,
        })
    return {
        "items": items,
        "ok": sum(1 for item in items if item["state"] == setup.OK),
        "total": len(items),
        "next": setup.next_command(results),
        "checked_at": round(time.time(), 3),
    }
