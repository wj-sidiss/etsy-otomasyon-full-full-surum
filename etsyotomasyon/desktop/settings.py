"""Where the desktop window keeps what it is told.

Credentials go in the same `.env` the command line reads — the current shop's home,
`~/.etsyotomasyon/.env` for the first shop — so a shop set up in the window works from a
terminal and the other way round.

Preferences are split in two, neither holding anything secret:

* `desktop.json` in the base home — the window's own: language, which shop is open;
* `desktop-shop.json` in each shop's home — that shop's products folder, template
  listing, last CSV files. Two shops never share a products folder by accident.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from .. import shops
from ..config import base_home, home_dir, load_env, write_env_file

ETSY_REDIRECT_DEFAULT = "http://localhost:3003/oauth/redirect"
PINTEREST_REDIRECT_DEFAULT = "http://localhost:8085/"

# Everything a shop's .env may set. Switching shops clears these from the process so
# the next shop's file is read fresh — load_env() never overrides a variable already set.
MANAGED_KEYS = (
    "ETSY_KEYSTRING",
    "ETSY_SHARED_SECRET",
    "ETSY_REDIRECT_URI",
    "ETSY_SHOP_ID",
    "ETSY_SCOPES",
    "ETSYOTOMASYON_RATE_PER_SEC",
    "PINTEREST_APP_ID",
    "PINTEREST_APP_SECRET",
    "PINTEREST_REDIRECT_URI",
    "PINTEREST_SANDBOX",
    "PINTEREST_ACCESS_TOKEN",
)


def env_path() -> Path:
    return home_dir() / ".env"


def app_prefs_path() -> Path:
    return base_home() / "desktop.json"


def shop_prefs_path() -> Path:
    return home_dir() / "desktop-shop.json"


def _file_values() -> dict[str, str]:
    path = env_path()
    if not path.is_file():
        return {}
    return {key: value for key, value in dotenv_values(path).items() if value is not None}


def current(key: str, default: str = "") -> str:
    """The value a command would see right now: environment first, then the files."""
    load_env()
    return (os.environ.get(key) or "").strip() or default


def save(updates: dict[str, str]) -> Path:
    """Merge `updates` into the current shop's .env and the running process.

    Keys that are not being changed are kept, including ones the window has no field
    for (ETSY_SHOP_ID, ETSY_SCOPES, a raised rate limit). An empty value removes the
    key. The process environment is updated too: `Config.load()` never overrides a
    variable that is already set, so without this a corrected keystring would only
    take effect after a restart.
    """
    values = _file_values()
    for key, raw in updates.items():
        value = (raw or "").strip()
        if value:
            values[key] = value
            os.environ[key] = value
        else:
            values.pop(key, None)
            os.environ.pop(key, None)
    path = env_path()
    write_env_file(path, values)
    return path


def use_shop(shop_id: str) -> shops.Shop:
    """Make `shop_id` the shop every command reads and writes ("" = the first shop)."""
    shop = shops.select(shop_id)
    for key in MANAGED_KEYS:
        os.environ.pop(key, None)
    load_env()
    return shop


def _load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(path: Path, prefs: dict[str, Any]) -> None:
    """Best effort: a preference that fails to save is not worth an error dialog."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(prefs, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def load_app_prefs() -> dict[str, Any]:
    return _load(app_prefs_path())


def save_app_prefs(prefs: dict[str, Any]) -> None:
    _save(app_prefs_path(), prefs)


def load_shop_prefs() -> dict[str, Any]:
    return _load(shop_prefs_path())


def save_shop_prefs(prefs: dict[str, Any]) -> None:
    _save(shop_prefs_path(), prefs)
