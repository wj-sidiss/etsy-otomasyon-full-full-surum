"""Several Etsy shops on one computer.

Each shop is a home of its own: its `.env` with the app keys, its token, its Pin
queue, its window preferences. The first shop is the base home itself
(`~/.etsyotomasyon`), exactly where a single-shop install has always kept everything, so
nothing moves for anyone with one shop. Further shops live in `~/.etsyotomasyon/shops/<id>/`
and are selected with ETSYOTOMASYON_SHOP or the `--shop` option.

Etsy ties an OAuth token to one Etsy account, and one account has one shop, so two
shops means two sign-ins — and a home per shop is what keeps them apart.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import SHOPS_DIR, base_home, home_dir, read_json, write_json_private
from .errors import ConfigError

SHOP_ENV = "ETSYOTOMASYON_SHOP"
INFO_FILE = "shop.json"

# Short, filesystem-safe and unambiguous on a case-insensitive disk.
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


@dataclass(frozen=True)
class Shop:
    id: str
    """Empty for the base shop, else the directory name under shops/."""

    @property
    def home(self) -> Path:
        return base_home() / SHOPS_DIR / self.id if self.id else base_home()

    @property
    def name(self) -> str:
        """The Etsy shop name, once the shop has been connected at least once."""
        data = _read_info(self.home)
        return str(data.get("shop_name") or "")

    @property
    def connected(self) -> bool:
        return (self.home / "token.json").is_file()


def _read_info(home: Path) -> dict:
    try:
        data = read_json(home / INFO_FILE)
    except ConfigError:
        return {}
    return data if isinstance(data, dict) else {}


def validate_id(shop_id: str) -> str:
    shop_id = (shop_id or "").strip()
    if not _ID.match(shop_id):
        raise ConfigError(
            f"{shop_id!r} is not a shop id. Use lowercase letters, digits and hyphens, "
            "e.g. `second-shop`. See them with: etsyotomasyon shops list"
        )
    return shop_id


def current() -> Shop:
    shop_id = (os.environ.get(SHOP_ENV) or "").strip()
    return Shop(validate_id(shop_id) if shop_id else "")


def select(shop_id: str) -> Shop:
    """Make `shop_id` the shop every later call reads and writes. "" is the base shop."""
    if not shop_id:
        os.environ.pop(SHOP_ENV, None)
        return Shop("")
    shop = Shop(validate_id(shop_id))
    if not shop.home.is_dir():
        raise ConfigError(f"There is no shop {shop_id!r}. See them with: etsyotomasyon shops list")
    os.environ[SHOP_ENV] = shop.id
    return shop


def all_shops() -> list[Shop]:
    """The base shop first, then the others in the order they were added."""
    shops = [Shop("")]
    folder = base_home() / SHOPS_DIR
    if folder.is_dir():
        extra = [p.name for p in folder.iterdir() if p.is_dir() and _ID.match(p.name)]
        # Natural order, so shop-10 comes after shop-9 rather than after shop-1.
        extra.sort(key=lambda name: [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)])
        shops += [Shop(name) for name in extra]
    return shops


def add() -> Shop:
    """Create an empty home for one more shop and return it (not selected)."""
    folder = base_home() / SHOPS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    taken = {p.name for p in folder.iterdir()}
    number = 2
    while f"shop-{number}" in taken:
        number += 1
    shop = Shop(f"shop-{number}")
    shop.home.mkdir()
    return shop


def remove(shop_id: str) -> None:
    """Delete one extra shop's home: its keys, token, queue and preferences.

    The base shop cannot be removed — disconnecting it (`auth logout`) is the
    equivalent. The shop on Etsy and its listings are not touched.
    """
    shop = Shop(validate_id(shop_id))
    root = (base_home() / SHOPS_DIR).resolve()
    target = shop.home.resolve()
    if target.parent != root or not target.is_dir():
        raise ConfigError(f"There is no shop {shop_id!r}.")
    shutil.rmtree(target)


def remember(shop_name: str, shop_id: int | str | None = None) -> None:
    """Record which Etsy shop the current home is connected to, for listings."""
    data = _read_info(home_dir())
    data.update({"shop_name": shop_name, "etsy_shop_id": shop_id})
    write_json_private(home_dir() / INFO_FILE, data)


def describe(shop: Shop) -> dict[str, str]:
    """One row of `etsyotomasyon shops`."""
    return {
        "id": shop.id or "(default)",
        "name": shop.name or "—",
        "connected": "yes" if shop.connected else "no",
        "home": str(shop.home),
    }

