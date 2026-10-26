"""The JSON API, one module per area. Each module exposes `register(r, ctx)`.

    def register(r: Router, ctx: AppContext) -> None:
        r.get("/api/things", list_things)

Modules are imported statically (not by name) so a frozen build always has them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import (
    connect,
    core,
    dashboard,
    designs,
    infoimages,
    listings,
    mockups,
    orders,
    pinterest,
    profit,
    seo,
    settings,
    template,
    updates,
    watermark,
)

if TYPE_CHECKING:  # pragma: no cover
    from ..context import AppContext
    from ..router import Router

MODULES = (
    core,
    connect,
    settings,
    mockups,
    watermark,
    template,
    infoimages,
    designs,
    listings,
    seo,
    orders,
    profit,
    dashboard,
    pinterest,
    updates,
)


def register_all(r: Router, ctx: AppContext) -> None:
    for module in MODULES:
        module.register(r, ctx)
