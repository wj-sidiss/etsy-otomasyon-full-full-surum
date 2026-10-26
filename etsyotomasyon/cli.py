"""Command line interface."""

from __future__ import annotations

import os
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__, auth, clipboard, csvio
from . import listings as listings_mod
from . import orders as orders_mod
from . import pinterest as pinterest_mod
from . import seo as seo_mod
from . import setup as setup_mod
from . import shops as shops_mod
from .client import EtsyClient, walk_taxonomy
from .config import Config, home_dir, split_credential, token_path, write_env_file
from .drop import automation, pipeline
from .drop import catalog as catalog_mod
from .drop import description as description_mod
from .drop import generate as generate_mod
from .drop import infoimages as infoimages_mod
from .drop import mockup as mockup_mod
from .drop import template as template_mod
from .drop import watermark as watermark_mod
from .drop import workspace as workspace_mod
from .errors import AuthError, EtsyotomasyonError, ValidationError


def _force_utf8(stream: Any) -> None:
    """Make status markers survive redirection on Windows.

    A Windows console renders Unicode fine, but the moment output is piped or
    redirected Python falls back to the legacy ANSI code page — cp1252 on an English
    install, cp1254 on a Turkish one. Neither can encode '✓', so every command that
    printed a status marker died with UnicodeEncodeError as soon as you sent it to a
    file. Reconfiguring to UTF-8 fixes redirection and changes nothing on a console.
    """
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass  # Not a reconfigurable stream (pytest capture, an odd shell). _symbol covers it.


def _symbol(preferred: str, fallback: str) -> str:
    """Fall back to ASCII if the stream still cannot represent the glyph."""
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        preferred.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return fallback
    return preferred


_force_utf8(sys.stdout)
_force_utf8(sys.stderr)

TICK = _symbol("✓", "OK")
CROSS = _symbol("✗", "X")
BULLET = _symbol("·", "-")

# Output is screenshotted and shared far more often than anyone plans for, and a
# listing title is enough to find the shop it belongs to. With this on, the findings
# stay readable and the identity does not survive the screenshot.
ANONYMISE = os.environ.get("ETSYOTOMASYON_ANONYMISE", "").strip().lower() in {"1", "true", "yes"}

# Guillemets, not square brackets: Rich reads "[hidden]" as markup and prints nothing,
# which looks like a bug rather than a redaction.
_HIDDEN = {"id": "#" + "•" * 8, "title": "‹hidden›", "shop": "‹your shop›", "url": "‹hidden›"}


def _hide(value: Any, kind: str = "title") -> str:
    """Return the value, or a placeholder when anonymised output is on."""
    if not ANONYMISE:
        return str(value)
    return _HIDDEN.get(kind, "‹hidden›")

console = Console()
err_console = Console(stderr=True)

app = typer.Typer(
    name="etsyotomasyon",
    help="Etsy seller automation: bulk listings, order/tracking sync, and SEO analysis.",
    no_args_is_help=True,
    add_completion=False,
)
auth_app = typer.Typer(help="Authorise etsyotomasyon against your Etsy account.", no_args_is_help=True)
shop_app = typer.Typer(help="Shop metadata you need to fill in a listing CSV.", no_args_is_help=True)
listings_app = typer.Typer(help="Export and bulk-create/update listings.", no_args_is_help=True)
orders_app = typer.Typer(help="Export orders and upload tracking numbers.", no_args_is_help=True)
seo_app = typer.Typer(help="Audit your listings and research the market.", no_args_is_help=True)
drop_app = typer.Typer(
    help="Drop designs in a folder, get a ready-to-push listing CSV.", no_args_is_help=True
)

app.add_typer(auth_app, name="auth")
app.add_typer(shop_app, name="shop")
app.add_typer(listings_app, name="listings")
app.add_typer(orders_app, name="orders")
app.add_typer(seo_app, name="seo")
app.add_typer(drop_app, name="drop")
pinterest_app = typer.Typer(
    help="Optional: queue Pins for your published listings on your own Pinterest account.",
    no_args_is_help=True,
)
app.add_typer(pinterest_app, name="pinterest")
shops_app = typer.Typer(
    help="Several Etsy shops on one computer: list, add and remove them.", no_args_is_help=True
)
app.add_typer(shops_app, name="shops")


def _client(*, require_auth: bool = True) -> EtsyClient:
    return EtsyClient(Config.load(), require_auth=require_auth)


def _ok(msg: str) -> None:
    console.print(f"[green]{TICK}[/] {msg}")


def _warn(msg: str) -> None:
    console.print(f"[yellow]![/] {msg}")


def _fail(msg: str) -> None:
    err_console.print(f"[red]{CROSS}[/] {msg}")


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"etsyotomasyon {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False,
        "--version",
        help="Print the version and exit.",
        # Eager, so it resolves while parsing — otherwise the app rejects
        # `etsyotomasyon --version` for having no subcommand before we ever see the flag.
        is_eager=True,
        callback=_version_callback,
    ),
    anonymise: bool = typer.Option(
        False,
        "--anonymise",
        "--anonymize",
        help="Hide your shop name, listing ids, titles and URLs in the output, so a "
        "screenshot can be shared without exposing your shop. Also ETSYOTOMASYON_ANONYMISE=1.",
    ),
    shop: Optional[str] = typer.Option(
        None,
        "--shop",
        help="Which of your shops to use, by the id `etsyotomasyon shops list` shows. "
        "Default: the first one. Also ETSYOTOMASYON_SHOP.",
    ),
) -> None:
    """etsyotomasyon — Etsy seller automation over the official Open API v3."""
    global ANONYMISE
    if anonymise:
        ANONYMISE = True
    # The option and the environment variable resolve the same way, with the same
    # "no such shop" error — a typo must not quietly create an empty shop.
    requested = shop if shop is not None else os.environ.get(shops_mod.SHOP_ENV)
    if requested is not None:
        requested = requested.strip()
        shops_mod.select("" if requested.lower() in ("", "default") else requested)


# ---------------------------------------------------------------- auth


@auth_app.command("login")
def auth_login(
    no_browser: bool = typer.Option(False, "--no-browser", help="Print the URL instead of opening it."),
    listen: Optional[int] = typer.Option(
        None,
        "--listen",
        help="Force catching the redirect on this local port (e.g. behind a tunnel). "
        "A localhost callback already does this automatically.",
    ),
    paste: bool = typer.Option(
        False, "--paste", help="Force the manual flow: paste the redirected URL back in."
    ),
) -> None:
    """Run the OAuth consent flow and store a token.

    A localhost callback is caught automatically; any other host uses the paste flow.
    """
    config = Config.load()
    auth.validate_redirect_uri(config.redirect_uri)
    if listen and not auth.port_is_free(listen):
        _warn(f"Something is already listening on port {listen}.")
    token = auth.login(
        config, open_browser=not no_browser, listen_port=listen, paste=paste
    )
    _ok(f"Authorised. Token saved to {token_path()}")
    console.print(f"  scopes: {' '.join(token.scopes)}")
    missing = token.missing_scopes(config.scopes)
    if missing:
        _warn(
            f"Etsy granted fewer scopes than requested — missing: {' '.join(missing)}. "
            "Commands needing those will fail with 403. Re-run `etsyotomasyon auth login` "
            "and approve everything."
        )
    with EtsyClient(config, token=token) as client:
        shop = client.shop()
        console.print(
            f"  shop:   {_hide(shop.get('shop_name'), 'shop')} "
            f"(id {_hide(shop.get('shop_id'), 'id')})"
        )
    shops_mod.remember(str(shop.get("shop_name") or ""), shop.get("shop_id"))


@auth_app.command("status")
def auth_status() -> None:
    """Show whether a valid token is stored, and for which shop."""
    config = Config.load(require_key=False)
    token = auth.load_token()
    if token is None:
        _warn("Not authenticated. Run: etsyotomasyon auth login")
        raise typer.Exit(1)

    table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
    table.add_row("token file", str(token_path()))
    table.add_row("user id", _hide(token.user_id or "unknown", "id"))
    table.add_row("scopes", " ".join(token.scopes) or "unknown")
    table.add_row(
        "expires in",
        "[red]expired[/]" if token.expired else f"{token.seconds_left // 60} min",
    )
    if config.keystring:
        with EtsyClient(config, token=token) as client:
            shop = client.shop()
            table.add_row(
                "shop",
                f"{_hide(shop.get('shop_name'), 'shop')} (id {_hide(shop.get('shop_id'), 'id')})",
            )
            table.add_row("active listings", str(shop.get("listing_active_count", "?")))
            if client.quota_remaining is not None:
                table.add_row("daily quota left", str(client.quota_remaining))
    console.print(table)


@auth_app.command("refresh")
def auth_refresh() -> None:
    """Force a token refresh."""
    token = auth.load_token()
    if token is None:
        raise AuthError("Nothing to refresh. Run: etsyotomasyon auth login")
    fresh = auth.refresh(token, Config.load())
    _ok(f"Refreshed. Valid for another {fresh.seconds_left // 60} minutes.")


@auth_app.command("logout")
def auth_logout() -> None:
    """Delete the stored token."""
    if auth.clear_token():
        _ok("Token deleted.")
    else:
        _warn("No token was stored.")


@app.command("init")
def init(
    keystring: Optional[str] = typer.Option(None, "--keystring", help="Your app's keystring."),
    redirect_uri: Optional[str] = typer.Option(
        None, "--redirect-uri", help="A callback URL registered on your Etsy app."
    ),
    shop_id: Optional[int] = typer.Option(None, "--shop-id", help="Only if you own several shops."),
    path: Optional[Path] = typer.Option(
        None, "--path", help="Where to write the file. Default: the selected shop's home, "
        "~/.etsyotomasyon/.env for the first shop — where the desktop app reads it too."
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing .env."),
    check: bool = typer.Option(True, "--check/--no-check", help="Verify the credential with Etsy."),
    from_clipboard: bool = typer.Option(
        False,
        "--from-clipboard",
        help="Take the shared secret from the clipboard instead of a prompt. "
        "Useful right after clicking 'copy' on your Etsy app page.",
    ),
) -> None:
    """Write a .env interactively.

    The shared secret is typed hidden, is never echoed, and goes straight into a
    0600 file — it does not appear in your shell history or on screen.
    """
    path = path or home_dir() / ".env"
    if path.exists() and not force:
        _warn(f"{path} already exists. Re-run with --force to overwrite it.")
        raise typer.Exit(1)

    console.print(
        "Find both halves of your credential at "
        "[cyan]https://www.etsy.com/developers/your-apps[/]\n"
    )

    keystring = keystring or typer.prompt("Keystring")
    if from_clipboard:
        secret = clipboard.read_text()
        # Length only. The value is never printed, by anything, ever.
        console.print(f"Shared secret: read {len(secret)} characters from the clipboard")
    else:
        secret = typer.prompt("Shared secret", hide_input=True)
    keystring, secret = split_credential(keystring, secret)
    if not keystring or not secret:
        _fail("Both the keystring and the shared secret are required.")
        raise typer.Exit(1)

    if not redirect_uri:
        redirect_uri = typer.prompt(
            "Redirect URI (must match a callback registered on your app)",
            default="http://localhost:3003/oauth/redirect",
        )
    auth.validate_redirect_uri(redirect_uri)

    values = {
        "ETSY_KEYSTRING": keystring,
        "ETSY_SHARED_SECRET": secret,
        "ETSY_REDIRECT_URI": redirect_uri,
    }
    if shop_id:
        values["ETSY_SHOP_ID"] = str(shop_id)
    write_env_file(path, values)
    _ok(f"Wrote {path} (keystring {keystring[:6]}…, shared secret {len(secret)} chars)")

    if not check:
        return

    config = Config(keystring=keystring, shared_secret=secret, redirect_uri=redirect_uri)
    with EtsyClient(config, require_auth=False) as client:
        try:
            client.ping()
        except EtsyotomasyonError as exc:
            _fail(f"Etsy rejected the credential: {exc}")
            err_console.print(
                "[yellow]→[/] Check both halves on your app page. The keystring and the "
                "shared secret are different values, and both must be exact."
            )
            raise typer.Exit(1) from exc
    _ok("Etsy accepted the credential.")
    console.print("\nNext: [cyan]etsyotomasyon auth login[/]")


def _run_checklist(*, interactive: bool, workspace: Optional[Path] = None) -> int:
    """Walk every prerequisite, ask about the ones no code can verify, and report."""
    console.print("[bold]etsyotomasyon setup[/] — everything that must be true before this works\n")

    results: list[tuple[setup_mod.Step, setup_mod.StepResult]] = []
    blocked = False

    for step in setup_mod.build_steps(workspace):
        # Once something required is missing, later checks would only report knock-on
        # failures. Show them as pending rather than as new problems.
        if blocked and step.required:
            results.append((step, setup_mod.StepResult(setup_mod.UNKNOWN, "not checked yet")))
            console.print(f" [dim]{step.number:>2} ·[/] [dim]{step.title}[/]")
            continue

        result = step.check()

        if result.state == setup_mod.UNKNOWN and step.question:
            if interactive:
                # Show the step, ask underneath it, then replace the line's detail —
                # so the question reads as part of the row rather than colliding with it.
                console.print(f" [yellow]{step.number:>2} ?[/] {step.title}")
                answered = typer.confirm(f"      {step.question}", default=True)
                if answered:
                    result = setup_mod.StepResult(setup_mod.OK, "you confirmed this")
                else:
                    result = setup_mod.StepResult(
                        setup_mod.MISSING,
                        "you said no",
                        setup_mod.ANSWER_FIXES.get(step.number, []),
                    )
                colour = "green" if result.state == setup_mod.OK else "red"
                mark = TICK if result.state == setup_mod.OK else CROSS
                console.print(f"      [{colour}]{mark}[/] [dim]{result.detail}[/]")
                results.append((step, result))
                if step.required and result.state == setup_mod.MISSING:
                    blocked = True
                continue

            result = setup_mod.StepResult(
                setup_mod.UNKNOWN,
                "only you can confirm this",
                setup_mod.ANSWER_FIXES.get(step.number, []),
            )

        results.append((step, result))
        _print_step(step, result)

        # Only a verified absence blocks. An unverifiable step ("do you have a shop?")
        # tells us nothing about whether the next one would pass, so keep checking.
        if step.required and result.state == setup_mod.MISSING:
            blocked = True

    done = sum(1 for _s, r in results if r.state == setup_mod.OK)
    problems = [
        (s, r)
        for s, r in results
        if r.state in (setup_mod.MISSING, setup_mod.UNKNOWN) and r.detail != "not checked yet"
    ]

    console.print()
    if not problems:
        _ok(f"All {len(results)} checks passed. You are ready.")
        console.print("\nTry: [cyan]etsyotomasyon shop info[/]  or  [cyan]etsyotomasyon seo audit[/]")
        return 0

    console.print(f"[bold]{done} of {len(results)} done.[/] What is missing:\n")
    for step, result in problems:
        colour = "red" if step.required else "yellow"
        console.print(f"  [{colour}]{step.number:>2}[/] [bold]{step.title}[/]")
        if result.detail:
            console.print(f"     [dim]{result.detail}[/]")
        for line in result.fix:
            console.print(f"     {line}")
        console.print()

    command = setup_mod.next_command(results)
    if command:
        console.print(f"[bold]Next:[/] [cyan]{command}[/]")
    return 1 if any(s.required for s, _r in problems) else 0


def _print_step(step: setup_mod.Step, result: setup_mod.StepResult) -> None:
    marks = {
        setup_mod.OK: ("green", TICK),
        setup_mod.MISSING: ("red", CROSS),
        setup_mod.WARN: ("yellow", "!"),
        setup_mod.UNKNOWN: ("yellow", "?"),
    }
    colour, mark = marks[result.state]
    console.print(f" [{colour}]{step.number:>2} {mark}[/] {step.title}")
    if result.detail:
        console.print(f"     [dim]{result.detail}[/]")


@app.command("setup")
def setup_command(
    check: bool = typer.Option(
        False, "--check", help="Do not ask anything — for scripts and CI."
    ),
) -> None:
    """Walk every prerequisite one by one and say exactly what is missing.

    Run this first, and run it again any time something does not work.
    """
    raise typer.Exit(_run_checklist(interactive=not check))


@app.command("doctor")
def doctor(
    path: Optional[Path] = typer.Option(
        None, "--path", help="The products folder to check. Default: the shop's own."
    ),
) -> None:
    """The same checklist, without asking anything. Good for scripts."""
    # A folder of a workspace (2-PRODUCTS, ...) means that workspace, as for `drop`.
    workspace = _workspace(path).root if path else None
    raise typer.Exit(_run_checklist(interactive=False, workspace=workspace))


@app.command("desktop")
def desktop(
    port: Optional[int] = typer.Option(
        None, "--port", help="Port to serve on (default: 3000, or the next free one)."
    ),
    no_browser: bool = typer.Option(
        False, "--no-browser", help="Start the app without opening the browser."
    ),
) -> None:
    """Open etsyotomasyon in your browser: every command behind a button, no terminal needed."""
    from .web import launch

    raise typer.Exit(launch(port=port, open_browser=not no_browser))


# ---------------------------------------------------------------- shop


@shop_app.command("info")
def shop_info() -> None:
    """Print your shop's identifiers and headline numbers."""
    with _client() as client:
        shop = client.shop()
    identifying = {"shop_name": "shop", "shop_id": "id", "url": "url"}
    table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
    for label, key in [
        ("shop name", "shop_name"),
        ("shop id", "shop_id"),
        ("url", "url"),
        ("currency", "currency_code"),
        ("country", "shop_location_country_iso"),
        ("active listings", "listing_active_count"),
        ("digital listings", "digital_listing_count"),
        ("sales", "transaction_sold_count"),
        ("reviews", "review_count"),
        ("rating", "review_average"),
        ("on vacation", "is_vacation"),
    ]:
        value = shop.get(key, "—")
        table.add_row(label, _hide(value, identifying[key]) if key in identifying else str(value))
    console.print(table)


@shop_app.command("profiles")
def shop_profiles() -> None:
    """List shipping profiles, return policies and sections — the IDs your CSV needs."""
    with _client() as client:
        shipping = client.shipping_profiles()
        policies = client.return_policies()
        sections = client.shop_sections()

    if shipping:
        table = Table(title="Shipping profiles", title_justify="left")
        table.add_column("shipping_profile_id", style="cyan")
        table.add_column("title")
        table.add_column("origin")
        table.add_column("processing")
        for profile in shipping:
            table.add_row(
                str(profile.get("shipping_profile_id")),
                str(profile.get("title", "")),
                str(profile.get("origin_country_iso", "")),
                f"{profile.get('min_processing_days', '?')}–{profile.get('max_processing_days', '?')} days",
            )
        console.print(table)
    else:
        _warn(
            "No shipping profiles yet. You can still stage drafts without one, "
            "but you cannot publish a physical listing until you create one in Etsy."
        )

    if policies:
        table = Table(title="Return policies", title_justify="left")
        table.add_column("return_policy_id", style="cyan")
        table.add_column("accepts returns")
        table.add_column("accepts exchanges")
        table.add_column("deadline (days)")
        for policy in policies:
            table.add_row(
                str(policy.get("return_policy_id")),
                str(policy.get("accepts_returns", "")),
                str(policy.get("accepts_exchanges", "")),
                str(policy.get("return_deadline", "—")),
            )
        console.print(table)

    if sections:
        table = Table(title="Shop sections", title_justify="left")
        table.add_column("shop_section_id", style="cyan")
        table.add_column("title")
        table.add_column("listings")
        for section in sections:
            table.add_row(
                str(section.get("shop_section_id")),
                str(section.get("title", "")),
                str(section.get("active_listing_count", "")),
            )
        console.print(table)


@shop_app.command("taxonomy")
def shop_taxonomy(
    query: str = typer.Argument(..., help="Category text to search for, e.g. 'necklace'."),
    limit: int = typer.Option(25, help="Maximum matches to show."),
) -> None:
    """Find the taxonomy_id for a category. Every new listing needs one."""
    with _client(require_auth=False) as client:
        nodes = client.taxonomy_nodes()

    flat = list(walk_taxonomy(nodes))
    needle = query.lower()
    matches = [(nid, path) for nid, path in flat if needle in path.lower()]
    if not matches:
        _warn(f"No category matched {query!r}. Try a broader word.")
        raise typer.Exit(1)

    # Prefer leaf categories: those are the ones Etsy accepts on a listing.
    matches.sort(key=lambda m: (needle not in m[1].split(" > ")[-1].lower(), len(m[1])))
    table = Table()
    table.add_column("taxonomy_id", style="cyan")
    table.add_column("category path")
    for nid, path in matches[:limit]:
        table.add_row(str(nid), path)
    console.print(table)
    console.print(f"[dim]{len(matches)} match(es); showing {min(limit, len(matches))}.[/]")


# ---------------------------------------------------------------- listings


@listings_app.command("template")
def listings_template(
    out: Path = typer.Option(Path("listings.csv"), "--out", "-o", help="Where to write the template."),
) -> None:
    """Write a starter CSV with the correct headers and one example row."""
    example = {
        "listing_id": "",
        "title": "Handmade Ceramic Coffee Mug, Minimalist Stoneware Cup, Gift for Coffee Lover",
        "description": "Wheel-thrown stoneware mug, glazed in matte cream.\n\nHolds 300ml. Dishwasher safe.",
        "price": "24.00",
        "quantity": "5",
        "who_made": "i_did",
        "when_made": "made_to_order",
        "taxonomy_id": "1633",
        "type": "physical",
        "tags": "ceramic mug|stoneware cup|handmade pottery|coffee lover gift|minimalist mug",
        "materials": "stoneware|glaze",
        "shipping_profile_id": "",
        "return_policy_id": "",
        "shop_section_id": "",
        "processing_min": "1",
        "processing_max": "3",
        "is_supply": "false",
        "is_customizable": "false",
        "is_taxable": "true",
        "should_auto_renew": "true",
        "item_weight": "450",
        "item_weight_unit": "g",
        "item_length": "",
        "item_width": "",
        "item_height": "",
        "item_dimensions_unit": "",
        "images": "photos/mug-1.jpg|photos/mug-2.jpg",
        "files": "",
        "state": "",
    }
    csvio.write_rows(out, [example], columns=listings_mod.LISTING_COLUMNS)
    _ok(f"Template written to {out}")
    console.print(
        "  Fill [cyan]shipping_profile_id[/] from `etsyotomasyon shop profiles` and "
        "[cyan]taxonomy_id[/] from `etsyotomasyon shop taxonomy <word>`.\n"
        "  Multi-value cells use [cyan]|[/] as the separator. Image paths are relative to the CSV.\n"
        "  [cyan]files[/]: what a buyer downloads (type download or both), relative to the CSV "
        "like images; leave it empty for a physical item."
    )


@listings_app.command("pull")
def listings_pull(
    out: Path = typer.Option(Path("listings-export.csv"), "--out", "-o"),
    state: str = typer.Option("active", help="active, inactive, draft, expired or sold_out."),
    limit: Optional[int] = typer.Option(None, help="Stop after this many listings."),
) -> None:
    """Export your listings to CSV (the same shape `push` reads back)."""
    with _client() as client:
        with console.status(f"Fetching {state} listings…"):
            rows = listings_mod.pull(client, state=state, max_items=limit)
    if not rows:
        _warn(f"No {state} listings found.")
        raise typer.Exit(1)
    # Etsy's ShopListing (OAS) has url and num_favorers but no view count: no "views".
    columns = listings_mod.LISTING_COLUMNS + ["url", "num_favorers"]
    csvio.write_rows(out, rows, columns=columns)
    _ok(f"Exported {len(rows)} listing(s) to {out}")


@listings_app.command("push")
def listings_push(
    src: Path = typer.Argument(..., help="CSV to read. Empty listing_id creates, filled updates."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate everything, send nothing."),
    no_images: bool = typer.Option(False, "--no-images", help="Skip image uploads."),
    partial: bool = typer.Option(
        False,
        "--partial",
        help="Push the valid rows even when others fail validation. Off by default: "
        "a half-pushed file leaves drafts you never meant to create.",
    ),
    out: Optional[Path] = typer.Option(
        None, "--out", "-o", help="Write results (with new listing_ids) to this CSV."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
    inventory_from: Optional[int] = typer.Option(
        None,
        "--inventory-from",
        help="Copy this listing's variations (options, prices, quantities) onto every "
        "new draft.",
    ),
) -> None:
    """Create or update listings in bulk. New listings are always created as drafts."""
    rows = csvio.read_rows(src)
    if not rows:
        _warn(f"{src} has no data rows.")
        raise typer.Exit(1)

    creates = sum(1 for r in rows if not r.get("listing_id"))
    updates = len(rows) - creates

    if not dry_run and not yes:
        console.print(
            Panel(
                f"About to create [bold]{creates}[/] draft listing(s) and "
                f"update [bold]{updates}[/] existing listing(s) in your live Etsy shop.\n"
                f"New listings are created as [bold]drafts[/] and will not go public until you "
                f"publish them in Etsy.",
                title="Confirm",
                border_style="yellow",
            )
        )
        if not typer.confirm("Proceed?"):
            _warn("Cancelled.")
            raise typer.Exit(1)

    push_args = {
        "base_dir": src.resolve().parent,
        "dry_run": dry_run,
        "upload_images": not no_images,
        "allow_partial": partial,
        "on_progress": _print_row_result,
    }
    if dry_run:
        # Validation is entirely local, so this works before you even have API access.
        if inventory_from:
            console.print(
                f"[dim]The real run copies the variations of listing {inventory_from} onto "
                f"each new draft. The dry run is offline, so it does not fetch them.[/]"
            )
        report = listings_mod.push(None, rows, **push_args)
    else:
        with _client() as client:
            if inventory_from:
                inventory = listings_mod.inventory_for_copy(
                    client.listing_inventory(inventory_from)
                )
                console.print(
                    f"[dim]Copying {len(inventory['products'])} variation(s) from listing "
                    f"{inventory_from} onto each new draft.[/]"
                )
                push_args["inventory"] = inventory
            # A drop batch's review.csv: its watermarked copies and info alt texts.
            batch = automation.for_batch(client, push_args["base_dir"])
            report = listings_mod.push(batch, rows, **push_args)

    console.print()
    if dry_run:
        valid = sum(1 for r in report.results if not r.failed)
        _ok(f"Dry run: {valid} row(s) valid, {report.errors} with problems. Nothing was sent.")
    elif report.aborted:
        _fail(report.aborted_reason)
    else:
        files = f", {report.files} download file(s)" if report.files else ""
        _ok(
            f"Created {report.created}, updated {report.updated}, "
            f"uploaded {report.images} image(s){files}, {report.errors} error(s)."
        )
        if report.partial:
            _warn(
                f"{report.partial} listing(s) were created but are not complete — "
                "missing images or variations. They exist in your shop — see the rows "
                "marked 'partial' above."
            )
        if report.created:
            console.print("[dim]New listings are drafts — publish them from your Etsy dashboard.[/]")

    if out:
        csvio.write_rows(
            out,
            [
                {
                    "row": r.row,
                    "listing_id": r.listing_id or "",
                    "action": r.action,
                    "status": r.status,
                    "title": r.title,
                    "images_uploaded": r.images_uploaded,
                    "files_uploaded": r.files_uploaded,
                    "message": r.message,
                }
                for r in report.results
            ],
            columns=["row", "listing_id", "action", "status", "title", "images_uploaded",
                     "files_uploaded", "message"],
        )
        _ok(f"Results written to {out}")

    if report.errors:
        raise typer.Exit(1)


def _print_row_result(result: listings_mod.RowResult) -> None:
    if result.failed:
        err_console.print(
            f"[red]{CROSS} row {result.row}[/] {_hide(result.title)} — {result.message}"
        )
    elif result.status == "partial":
        console.print(f"[yellow]! row {result.row}[/] {_hide(result.title)} — {result.message}")
    elif result.status == "skipped":
        console.print(f"[dim]{BULLET} row {result.row}[/] skipped — {result.message}")
    elif result.status == "dry-run":
        console.print(
            f"[dim]{BULLET} row {result.row}[/] {result.action}: "
            f"{_hide(result.title)} ({result.message})"
        )
    else:
        extra = f", {result.images_uploaded} image(s)" if result.images_uploaded else ""
        if result.files_uploaded:
            extra += f", {result.files_uploaded} download file(s)"
        console.print(
            f"[green]{TICK} row {result.row}[/] {result.action} "
            f"{_hide(result.listing_id, 'id')} — {_hide(result.title)}{extra}"
        )
    for warning in result.warnings:
        console.print(f"    [yellow]![/] {warning}")


# ---------------------------------------------------------------- orders


@orders_app.command("pull")
def orders_pull(
    out: Path = typer.Option(Path("orders.csv"), "--out", "-o"),
    since: str = typer.Option("30d", help="30d, 6w, 3m, or a date like 2026-01-01."),
    unshipped: bool = typer.Option(False, "--unshipped", help="Only orders not yet marked shipped."),
    include_unpaid: bool = typer.Option(False, "--include-unpaid", help="Include unpaid orders."),
    limit: Optional[int] = typer.Option(None, help="Stop after this many orders."),
) -> None:
    """Export orders to CSV."""
    min_created = orders_mod.parse_since(since)
    with _client() as client:
        with console.status("Fetching orders…"):
            rows = orders_mod.pull(
                client,
                since=min_created,
                unshipped_only=unshipped,
                paid_only=not include_unpaid,
                max_items=limit,
            )
    if not rows:
        _warn("No orders matched those filters.")
        raise typer.Exit(1)
    csvio.write_rows(out, rows, columns=orders_mod.ORDER_COLUMNS)
    _ok(f"Exported {len(rows)} order(s) to {out}")

    if unshipped:
        console.print(
            "[dim]Tip: add tracking_code and carrier_name columns to this file, "
            "then run `etsyotomasyon orders ship`.[/]"
        )


@orders_app.command("carriers")
def orders_carriers(
    country: str = typer.Option(..., "--country", "-c", help="Origin country ISO code, e.g. TR, US, DE."),
    search: Optional[str] = typer.Option(None, help="Filter the list."),
) -> None:
    """List the carrier_name values Etsy accepts for your shipping origin."""
    with _client() as client:
        carriers = client.shipping_carriers(country.upper())
    if search:
        needle = search.lower()
        carriers = [c for c in carriers if needle in str(c.get("name", "")).lower()]
    if not carriers:
        _warn(f"No carriers returned for {country.upper()}.")
        raise typer.Exit(1)
    table = Table(title=f"Carriers shipping from {country.upper()}", title_justify="left")
    table.add_column("carrier_name", style="cyan")
    table.add_column("domestic")
    table.add_column("international")
    for carrier in carriers:
        table.add_row(
            str(carrier.get("name", "")),
            str(carrier.get("domestic_classes") and len(carrier["domestic_classes"]) or "—"),
            str(carrier.get("international_classes") and len(carrier["international_classes"]) or "—"),
        )
    console.print(table)


@orders_app.command("ship")
def orders_ship(
    src: Path = typer.Argument(..., help="CSV with receipt_id, tracking_code, carrier_name."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate only."),
    country: Optional[str] = typer.Option(
        None, "--country", "-c", help="Validate carrier names against this origin country."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
) -> None:
    """Upload tracking numbers. Etsy emails each buyer and marks the order shipped."""
    all_rows = csvio.read_rows(src)
    # Rows without a tracking code are skipped so an `orders pull` export can be fed
    # straight back in — but say so out loud, because a silently dropped row here is
    # an order the seller believes they shipped.
    rows = [r for r in all_rows if r.get("tracking_code")]
    skipped = len(all_rows) - len(rows)
    if skipped:
        _warn(f"{skipped} row(s) have no tracking_code and will be skipped.")
    if not rows:
        _warn(f"{src} has no rows with a tracking_code.")
        raise typer.Exit(1)

    if not dry_run and not yes:
        console.print(
            Panel(
                f"About to submit tracking for [bold]{len(rows)}[/] order(s).\n"
                f"Etsy will [bold]email each buyer[/] and mark the order shipped. "
                f"This cannot be undone from the API.",
                title="Confirm",
                border_style="yellow",
            )
        )
        if not typer.confirm("Proceed?"):
            _warn("Cancelled.")
            raise typer.Exit(1)

    if dry_run and not country:
        # No network needed: nothing is sent and no carrier list is being checked.
        report = orders_mod.ship(None, rows, dry_run=True, on_progress=_print_ship_result)
    else:
        with _client() as client:
            valid = None
            if country:
                valid = [str(c.get("name", "")) for c in client.shipping_carriers(country.upper())]
            report = orders_mod.ship(
                client, rows, dry_run=dry_run, valid_carriers=valid, on_progress=_print_ship_result
            )

    console.print()
    if dry_run:
        valid = sum(1 for r in report.results if not r.failed)
        _ok(f"Dry run: {valid} row(s) valid, {report.errors} problem(s). Nothing was sent.")
    else:
        _ok(f"Submitted {report.shipped} shipment(s), {report.errors} error(s).")
    if report.errors:
        raise typer.Exit(1)


def _print_ship_result(result: orders_mod.ShipResult) -> None:
    if result.failed:
        err_console.print(
            f"[red]{CROSS} row {result.row}[/] receipt {result.receipt_id} — {result.message}"
        )
    else:
        prefix = BULLET if result.status == "dry-run" else TICK
        colour = "dim" if result.status == "dry-run" else "green"
        console.print(f"[{colour}]{prefix} row {result.row}[/] receipt {result.receipt_id} — {result.message}")


# ---------------------------------------------------------------- seo


@seo_app.command("audit")
def seo_audit(
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Write the full report to CSV."),
    state: str = typer.Option("active", help="Which listings to audit."),
    limit: Optional[int] = typer.Option(None, help="Stop after this many listings."),
    worst: int = typer.Option(15, help="How many of the weakest listings to print."),
) -> None:
    """Score your listings against Etsy's limits and search mechanics."""
    with _client() as client:
        with console.status("Auditing listings…"):
            listings = list(client.listings_by_shop(state=state, max_items=limit))
    if not listings:
        _warn(f"No {state} listings to audit.")
        raise typer.Exit(1)

    # The shop first: two of the checks (tags shared with another listing, a tag list of
    # the shop's generic tags) compare each listing with the others.
    shop = seo_mod.audit_shop(listings)
    audits = [seo_mod.audit_listing(listing, shop) for listing in listings]

    audits.sort(key=lambda a: a.score)
    average = sum(a.score for a in audits) / len(audits)
    poor = sum(1 for a in audits if a.grade == "poor")

    table = Table(title=f"Weakest listings ({len(audits)} audited, average score {average:.0f})",
                  title_justify="left")
    table.add_column("score", justify="right")
    table.add_column("listing_id", style="cyan")
    table.add_column("title", max_width=42, overflow="ellipsis")
    table.add_column("issues", overflow="fold")
    for item in audits[:worst]:
        colour = {"good": "green", "fair": "yellow", "poor": "red"}[item.grade]
        table.add_row(
            f"[{colour}]{item.score}[/]",
            _hide(item.listing_id, "id"),
            _hide(item.title),
            item.summary(),
        )
    console.print(table)

    counter: dict[str, int] = {}
    for item in audits:
        for issue in item.issues:
            counter[issue.code] = counter.get(issue.code, 0) + 1
    if counter:
        summary = Table(title="Most common problems", title_justify="left")
        summary.add_column("issue", style="cyan")
        summary.add_column("listings", justify="right")
        for code, count in sorted(counter.items(), key=lambda kv: -kv[1])[:10]:
            summary.add_row(code, str(count))
        console.print(summary)

    console.print(f"\n{poor} listing(s) scored below 60.")

    # Everything above judges each listing on its own, which cannot see a shop whose
    # listings all say the same thing. This can.
    if shop.issues:
        console.print()
        for issue in shop.issues:
            shade = {"error": "red", "warn": "yellow", "info": "dim"}[issue.severity]
            console.print(f"[{shade}]![/] [bold]Across the whole shop:[/] {issue.message}")

        if shop.shop_wide:
            table = Table(title="Tags shared across your own listings", title_justify="left")
            table.add_column("tag", style="cyan")
            table.add_column("your listings", justify="right")
            # A tag names the niche as surely as the shop does.
            for tag, count in shop.shop_wide[:10]:
                table.add_row(_hide(tag), f"{count}/{shop.listings}")
            console.print(table)

        console.print(
            f"[dim]Median tags that distinguish a listing from its siblings: "
            f"{shop.median_distinctive:.0f} of 13.[/]"
        )

        pairs = seo_mod.overlapping_pairs(listings)
        if pairs:
            table = Table(title="Listings most alike, and so competing", title_justify="left")
            table.add_column("overlap", justify="right")
            table.add_column("listing", max_width=34, overflow="ellipsis")
            table.add_column("and", max_width=34, overflow="ellipsis")
            for title_a, title_b, similarity in pairs:
                table.add_row(f"{similarity:.0%}", _hide(title_a), _hide(title_b))
            console.print(table)

    if out:
        csvio.write_rows(
            out,
            [
                {
                    "listing_id": a.listing_id,
                    "score": a.score,
                    "grade": a.grade,
                    "title": a.title,
                    "url": a.url,
                    "issue_count": len(a.issues),
                    "issues": a.summary(),
                }
                for a in audits
            ],
            columns=["listing_id", "score", "grade", "title", "url", "issue_count", "issues"],
        )
        _ok(f"Full report written to {out}")


@seo_app.command("keywords")
def seo_keywords(
    keyword: str = typer.Argument(..., help="The search term to study."),
    sample: int = typer.Option(200, help="How many ranking listings to sample (max ~1000)."),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Write tag frequencies to CSV."),
) -> None:
    """Study the listings Etsy ranks for a term: their tags, phrases and price band.

    Works without logging in — only your API keystring is needed.
    """
    with _client(require_auth=False) as client:
        with console.status(f"Sampling listings for {keyword!r}…"):
            report = seo_mod.research(client, keyword, sample=sample)

    if report.empty:
        _warn(f"Etsy returned no active listings for {keyword!r}.")
        raise typer.Exit(1)

    lines = [f"Sampled [bold]{report.sampled}[/] listings ranking for [bold]{keyword}[/]"]
    if report.price_median is not None:
        lines.append(
            f"Price  {report.price_min:.2f} – [bold]{report.price_median:.2f}[/] – "
            f"{report.price_max:.2f} {report.currency}"
        )
        if report.price_band_is_partial:
            # Etsy prices each listing in its shop's own currency, so this band
            # covers one currency only. Say which, and how much of the sample.
            lines.append(
                f"[dim]{report.price_sample} of {report.sampled} listings priced in "
                f"{report.currency}; {report.currency_count} currencies in the sample.[/]"
            )
    if report.median_favorers is not None:
        lines.append(f"Median favourites: {report.median_favorers:.0f}")
    console.print(Panel("\n".join(lines), border_style="cyan"))

    tags_table = Table(
        title=f"Tags used by ranking listings (of {report.sampled} sampled)",
        title_justify="left",
    )
    tags_table.add_column("tag", style="cyan")
    tags_table.add_column("listings", justify="right")
    tags_table.add_column("share", justify="right")
    for tag, count in report.tags[:20]:
        tags_table.add_row(tag, str(count), f"{count / report.sampled:.0%}")
    console.print(tags_table)

    phrase_table = Table(title="Recurring title phrases", title_justify="left")
    phrase_table.add_column("phrase", style="cyan")
    phrase_table.add_column("listings", justify="right")
    for phrase, count in report.phrases[:20]:
        phrase_table.add_row(phrase, str(count))
    console.print(phrase_table)

    top_table = Table(title="Most-favourited in the sample", title_justify="left")
    top_table.add_column("favourites", justify="right")
    top_table.add_column("price", justify="right")
    top_table.add_column("title", max_width=60, overflow="ellipsis")
    for row in report.top_listings[:8]:
        top_table.add_row(str(row["num_favorers"]), f"{row['price']} {row['currency']}", row["title"])
    console.print(top_table)

    if out:
        csvio.write_rows(
            out,
            [
                {"type": "tag", "value": tag, "listings": count, "share": f"{count / report.sampled:.4f}"}
                for tag, count in report.tags
            ]
            + [
                {"type": "phrase", "value": phrase, "listings": count, "share": ""}
                for phrase, count in report.phrases
            ],
            columns=["type", "value", "listings", "share"],
        )
        _ok(f"Written to {out}")


@seo_app.command("suggest")
def seo_suggest(
    listing_id: int = typer.Argument(..., help="One of your listing IDs."),
    keyword: Optional[str] = typer.Option(
        None, help="Term to research. Defaults to the listing's own title."
    ),
    sample: int = typer.Option(200, help="How many ranking listings to sample."),
) -> None:
    """Audit one listing, then suggest tags drawn from what ranks for its keyword."""
    with _client() as client:
        # Through client.listing, so the title and tags are decoded plain text (Etsy
        # sends them HTML-escaped); the signed-in view also covers the seller's drafts.
        listing = client.listing(listing_id, authed=True)
        audit = seo_mod.audit_listing(listing)

        colour = {"good": "green", "fair": "yellow", "poor": "red"}[audit.grade]
        console.print(
            Panel(
                f"[bold]{_hide(audit.title)}[/]\n"
                f"Score: [{colour}]{audit.score}/100[/] ({audit.grade})",
                border_style=colour,
            )
        )
        if audit.issues:
            table = Table(show_header=True)
            table.add_column("severity")
            table.add_column("issue", style="cyan")
            table.add_column("detail", overflow="fold")
            for issue in audit.issues:
                shade = {"error": "red", "warn": "yellow", "info": "dim"}[issue.severity]
                table.add_row(f"[{shade}]{issue.severity}[/]", issue.code, issue.message)
            console.print(table)
        else:
            _ok("No issues found.")

        term = keyword or " ".join((listing.get("title") or "").split()[:4])
        if not term.strip():
            return
        with console.status(f"Researching {term!r}…"):
            report = seo_mod.research(client, term, sample=sample)

    if report.empty:
        _warn(f"No market data for {term!r}.")
        return

    # The draft builder's rules: no near-duplicate of a tag the listing has, no adhesive
    # claim ("removable") the listing does not make, and no more of the generic tags than
    # a listing can afford.
    chosen = generate_mod.suggest_additions(
        report, existing=listing.get("tags") or [], title=listing.get("title") or "",
        text=generate_mod.hint_from(
            listing.get("title") or "", listing.get("tags") or [],
            listing.get("description") or "", listing.get("materials") or [],
        ),
    )
    suggestions = seo_mod.suggest_tags(
        report, existing=listing.get("tags") or [], candidates=[tag for tag, _n in chosen]
    )

    def _line(tag: str) -> str:
        count = next((c for t, c in report.tags if t == tag), 0)
        return (
            f"  [cyan]{tag}[/] [dim](in {count} of the {report.sampled} listings sampled, "
            f"{count / report.sampled:.0%})[/]"
        )

    if suggestions.add_now:
        console.print(
            f"\n[bold]You have {suggestions.free_slots} free tag slot(s). "
            f"Common in this market and unused by you:[/]"
        )
        for tag in suggestions.add_now:
            console.print(_line(tag))
    elif suggestions.used_slots >= 13:
        _warn("All 13 tag slots are full — anything below would mean dropping one first.")
    else:
        _ok("Your tags already cover the common ones in this market.")

    if suggestions.needs_a_swap:
        console.print(
            f"\n[bold]Also common, but only if you drop an existing tag "
            f"({suggestions.used_slots}/13 used):[/]"
        )
        for tag in suggestions.needs_a_swap:
            console.print(_line(tag))

    if suggestions.add_now or suggestions.needs_a_swap:
        console.print(
            "\n[dim]These are usage shares among listings Etsy returned for this term — "
            "not search volume, and not demand. Only add tags that genuinely describe "
            "your item; irrelevant tags pull in traffic that does not convert, and Etsy "
            "weights conversion heavily.[/]"
        )


# ---------------------------------------------------------------- drop


def _workspace(path: Optional[Path]) -> workspace_mod.Workspace:
    """The workspace for --path, or the default one.

    A folder of a workspace (1-MOCKUPS, 2-PRODUCTS, 3-DRAFTS, anything inside them) means
    that workspace, never a second one nested inside it (workspace_mod.root_for).
    """
    if not path:
        return workspace_mod.Workspace(workspace_mod.default_root())
    chosen = Path(path).expanduser()
    absolute = Path(os.path.normpath(str(chosen.absolute())))
    root = workspace_mod.root_for(absolute)
    if root == absolute:
        return workspace_mod.Workspace(chosen)
    if workspace_mod.enclosing_root(absolute) is None:
        # Only the name: a new "...\2-PRODUCTS" whose parent holds nothing else.
        _warn(f"{chosen} is named like a workspace folder; its parent {root} is the "
              "workspace.")
        return workspace_mod.Workspace(root)
    _warn(f"{chosen} is a folder of the workspace {root}; using {root}.")
    if workspace_mod.is_workspace(absolute):
        # A workspace an older version made inside this one, and worked in: its
        # template and upload history come along, or its drafts would be made again.
        try:
            moved = workspace_mod.adopt_nested(absolute, root)
        except EtsyotomasyonError as exc:
            _warn(f"Its template and upload history could not be carried over: {exc}")
            raise typer.Exit(1) from exc
        if moved:
            what = " and ".join(
                {"template": "template (product.json)",
                 "history": "upload history (upload-history.json)"}[m] for m in moved)
            _ok(f"Carried its {what} over to {root}.")
    return workspace_mod.Workspace(root)


@drop_app.command("init")
def drop_init(
    path: Optional[Path] = typer.Option(None, "--path", help="Where to create it."),
) -> None:
    """Create the desktop folder you drop designs into."""
    ws = _workspace(path).create()
    _ok(f"Workspace ready at {ws.root}")
    console.print(
        f"  [cyan]{workspace_mod.MOCKUPS_DIR}[/]   your mockup templates (once)\n"
        f"  [cyan]{workspace_mod.PRODUCTS_DIR}[/]  the designs you want listed\n"
        f"  [cyan]{workspace_mod.DRAFTS_DIR}[/]    what comes out\n"
    )
    console.print(
        "Next: build ONE listing properly in Etsy by hand, then copy its settings:\n"
        "  [cyan]etsyotomasyon drop template --from-listing <listing_id>[/]"
    )


@drop_app.command("template")
def drop_template(
    from_listing: int = typer.Option(..., "--from-listing", help="A listing you built by hand."),
    path: Optional[Path] = typer.Option(None, "--path"),
) -> None:
    """Copy the settings every future draft will inherit from one real listing."""
    ws = _workspace(path).require()
    with _client(require_auth=False) as client:
        listing = client.listing(from_listing)

    captured = template_mod.capture(listing)
    # A description template saved for this same listing (the app's Şablon İlan) stays;
    # another listing starts from its own description.
    before = None
    if ws.template_path.is_file():
        try:
            saved = ws.read_template()
            before = template_mod.Template.from_dict(saved) if isinstance(saved, dict) else None
        except EtsyotomasyonError:
            before = None
    if before is not None and before.source_listing_id == captured.source_listing_id:
        captured.description_template = before.description_template
        captured.category_path = before.category_path
    ws.write_template(captured.to_dict())
    _ok(f"Captured listing {_hide(captured.source_listing_id, 'id')} into {ws.template_path}")

    table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
    for label, value in captured.describe():
        table.add_row(label, _hide(value) if label in {"Copied from", "Its tags"} else value)
    console.print(table)

    gaps = captured.missing_for_a_physical_draft()
    if gaps:
        _warn(
            f"This listing has no {', '.join(gaps)}. Drafts will still be created, "
            "but you cannot publish them until that is set."
        )
    flagged = description_mod.template_flags(captured, lang="en")
    if flagged:
        words = _hide(", ".join(description_mod.flag_words(flagged)[:4]))
        _warn(
            f"{len(flagged)} sentence(s) of the description look specific to this listing's "
            f"own design ({words}) and would be copied onto every draft. Edit the "
            "description template in the app's Template page, or set "
            f"\"{template_mod.DESCRIPTION_TEMPLATE}\" in {ws.template_path} "
            "({title} and {design} are filled in per draft)."
        )
    console.print(
        f"\nNow put designs in [cyan]{ws.products}[/] and run: [cyan]etsyotomasyon drop run[/]"
    )


_MOCKUPS_HELP = (
    "Use only the first N of the chosen mockups. Default: every mockup switched on in the "
    "app's Mockups page, in its order (the first is the main image), at most 19 less one "
    "per info image."
)
_SECTION_HELP = (
    "Put every draft of this run in this shop section: its name or id (see `etsyotomasyon shop "
    "profiles`), or `none` for no section. Default: the template listing's section."
)


def _drop_section(
    tmpl: template_mod.Template, wanted: Optional[str], client: Optional[EtsyClient]
) -> template_mod.Template:
    """The template with `--section` applied: the named section (checked against the
    shop's sections, so a deleted one is refused before anything runs), or none."""
    if wanted is None:
        return tmpl
    if client is None:
        raise ValidationError(
            "--section reads your shop's sections, which needs your Etsy keys and the "
            "sign-in. Run: etsyotomasyon auth login"
        )
    found = template_mod.resolve_section(client.shop_sections(), wanted)
    if found is None:
        console.print("[dim]Shop section: none (the drafts go into no section).[/]")
        return tmpl.with_section(None)
    section_id = template_mod.section_number(found.get("shop_section_id"))
    console.print(f"[dim]Shop section: {_hide(found.get('title') or '')} ({section_id}).[/]")
    return tmpl.with_section(section_id)


def _drop_info_images(ws: workspace_mod.Workspace) -> list[infoimages_mod.InfoImage]:
    """The shop's info images every draft ends with (Şablon İlan, or info-images/).

    Says once which ones are used, and which are left out for being past the limit.
    """
    images = infoimages_mod.load(ws)
    if images:
        names = ", ".join(image.name for image in images)
        console.print(
            f"[dim]Info images: {len(images)} after each product's own photos ({names}). "
            f"Choose them in the app's Template listing page or in {ws.info_images}.[/]"
        )
    extra = infoimages_mod.unused(ws)
    if extra:
        _warn(
            f"{len(extra)} picture(s) in {ws.info_images} are past the limit of "
            f"{infoimages_mod.MAX_INFO_IMAGES} info images and are not used: "
            + ", ".join(image.name for image in extra)
        )
    return images


def _drop_mockups(ws: workspace_mod.Workspace, count: Optional[int],
                  listing_type: Optional[str] = None) -> list[Path]:
    """The mockups a drop run composites onto: the app's selection and order (Mockuplar),
    less those showing a physical product for a download-only template, as the app's
    run does (catalog.run_mockups).

    `--mockups N` keeps the first N of them. Says once which ones are used.
    """
    infos = catalog_mod.load(ws)
    use = catalog_mod.usage(ws, infos, listing_type=listing_type)
    chosen = catalog_mod.run_mockups(ws, listing_type, infos)
    if count is not None:
        chosen = chosen[: max(0, count)]
    available = len(ws.mockup_files())
    if available:
        off = use["total"] - use["enabled"] - len(use["left_out"])
        notes = [f"main image {chosen[0].name}"] if chosen else []
        if off:
            notes.append(f"{off} switched off")
        if use["left_out"]:
            notes.append(
                f"{len(use['left_out'])} left out because they show a physical product and "
                f"the template is a download ({', '.join(use['left_out'])})"
            )
        if use["over_limit"]:
            notes.append(f"{len(use['over_limit'])} over the {use['max']}-mockup limit")
        if count is not None and len(chosen) < len(use["used"]):
            notes.append(f"--mockups {count} keeps the first {len(chosen)}")
        console.print(
            f"[dim]Mockups: {len(chosen)} of {available} used"
            + (f" ({'; '.join(notes)})" if notes else "")
            + ". Choose and order them in the app's Mockups page.[/]"
        )
        if not chosen and use["left_out"]:
            _warn("No mockup a download can use is switched on (a poster, canvas or other): "
                  "the designs get only the flat preview.")
        elif not chosen:
            _warn("No mockup is switched on: transparent designs get only the flat render.")
    return chosen


_NO_WATERMARK_HELP = "Leave the workspace's watermark off the photos for this run."


def _drop_watermark(ws: workspace_mod.Workspace, tmpl: template_mod.Template,
                    use: bool) -> None:
    """Say once whether the run stamps the workspace's watermark (drop watermark)."""
    info = watermark_mod.describe(ws, tmpl.listing_type)
    if not info["file"]:
        return
    if not use and info["applies"]:
        console.print("[dim]Watermark: left off for this run (--no-watermark).[/]")
        return
    console.print(f"[dim]{watermark_mod.summary(info)}[/]")


@drop_app.command("run")
def drop_run(
    path: Optional[Path] = typer.Option(None, "--path"),
    mockups: Optional[int] = typer.Option(None, "--mockups", min=0, help=_MOCKUPS_HELP),
    no_flat: bool = typer.Option(False, "--no-flat", help="Do not append the flat artwork."),
    sample: int = typer.Option(200, "--sample", help="Listings to sample per concept."),
    no_cache: bool = typer.Option(False, "--no-cache", help="Ignore cached research."),
    no_watermark: bool = typer.Option(False, "--no-watermark", help=_NO_WATERMARK_HELP),
    section: Optional[str] = typer.Option(None, "--section", help=_SECTION_HELP),
) -> None:
    """Turn the designs in your folder into a review CSV. Sends nothing to Etsy."""
    ws = _workspace(path).require()
    tmpl = template_mod.Template.from_dict(ws.read_template())

    designs = ws.product_groups()
    if not designs:
        _warn(f"No designs in {ws.products}. Drop some image files in and run again.")
        raise typer.Exit(1)
    if not ws.mockup_files():
        _warn(
            f"No mockups in {ws.mockups}. Transparent artwork needs one; finished "
            "product photos do not."
        )

    client = None
    try:
        client = EtsyClient(Config.load(), require_auth=False)
    except EtsyotomasyonError as exc:
        if section is not None:
            raise
        _warn(f"Running without market research ({exc.args[0].splitlines()[0]}).")

    try:
        tmpl = _drop_section(tmpl, section, client)
    except BaseException:
        if client:
            client.close()
        raise

    chosen = _drop_mockups(ws, mockups, tmpl.listing_type)
    _drop_watermark(ws, tmpl, not no_watermark)
    info = _drop_info_images(ws)
    images_each = len(chosen) + (0 if no_flat else 1) + len(info)
    # A digital template's drafts also upload what the buyer downloads, one request each.
    to_order = pipeline.made_to_order(tmpl)  # a made-to-order draft may go without one
    files = (
        sum(len(pipeline.deliverables(source, made_to_order=to_order)[0])
            for source, _ in designs)
        if tmpl.digital else 0
    )
    downloads = f" and {files} download file(s)" if files else ""
    estimate = pipeline.estimate_requests(len(designs), len(designs), images_each, files=files)
    console.print(
        f"[dim]{len(designs)} design(s), about {images_each} image(s) each{downloads}. "
        f"Creating the drafts later will cost roughly {estimate} requests "
        f"of your 5,000 daily allowance.[/]\n"
    )

    try:
        with console.status("Preparing…") as status:
            report = pipeline.run(
                ws,
                tmpl,
                client=client,
                mockups=chosen,
                include_flat=not no_flat,
                sample=sample,
                use_cache=not no_cache,
                on_progress=lambda msg: status.update(msg),
                watermark=not no_watermark,
                info_images=info,
            )
    finally:
        if client:
            client.close()

    for row in report.rows:
        if not row.ok:
            err_console.print(f"[red]{CROSS}[/] {row.source.name} — {'; '.join(row.warnings)}")
        else:
            note = f" [yellow]({len(row.warnings)} warning(s))[/]" if row.warnings else ""
            console.print(
                f"[green]{TICK}[/] {row.source.name} → [dim]{row.title[:60]}[/]{note}"
            )

    console.print()
    _ok(
        f"{len(report.ready)} ready, {len(report.skipped)} skipped. "
        f"{report.images_made} image(s) made across {report.concepts} concept(s) "
        f"({report.researched} researched, {report.cached} from cache)."
    )
    if not report.csv_path:
        raise typer.Exit(1)

    console.print(f"\nReview it: [cyan]{report.csv_path}[/]")
    console.print(
        "Then, when it looks right:\n"
        f"  [cyan]etsyotomasyon listings push \"{report.csv_path}\" --dry-run[/]\n"
        f"  [cyan]etsyotomasyon listings push \"{report.csv_path}\"[/]   [dim](creates drafts)[/]"
    )
    console.print(
        "\n[dim]If a design landed in the wrong place on the mockup, move the print area "
        "and look at it first:[/] [cyan]etsyotomasyon drop calibrate --preview[/]"
    )


@drop_app.command("calibrate")
def drop_calibrate(
    path: Optional[Path] = typer.Option(None, "--path"),
    mockup_name: Optional[str] = typer.Option(
        None, "--mockup", help="Which template, e.g. shirt-white.jpg. The extension is optional."
    ),
    area: Optional[str] = typer.Option(
        None, "--area", help="Print area as fractions of the mockup: x,y,w,h, each 0 to 1."
    ),
    same_size: bool = typer.Option(
        False, "--same-size", help="Give --area to every mockup of the same pixel size."
    ),
    import_from: Optional[Path] = typer.Option(
        None, "--import", help="Convert an older pixel-based mockup-positions.json."
    ),
    reset: bool = typer.Option(
        False, "--reset", help="Forget a saved area and go back to the default."
    ),
    preview: bool = typer.Option(
        False, "--preview", help="Write a JPEG of each mockup with its area drawn on it."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Preview the change without writing positions.json."
    ),
) -> None:
    """Move the print area on a mockup, and look at it before you commit.

    With no options it lists what every mockup is using today.
    """
    ws = _workspace(path).require()

    if sum([bool(area), bool(reset), import_from is not None]) > 1:
        _warn("Use one of --area, --reset or --import at a time.")
        raise typer.Exit(1)
    if import_from is not None and mockup_name:
        _warn("--import brings over every entry it can match, so it does not take --mockup.")
        raise typer.Exit(1)
    if same_size and not area:
        _warn("--same-size chooses which mockups get --area, so it needs --area.")
        raise typer.Exit(1)
    if (area or reset) and not mockup_name:
        _warn("Which mockup? Add --mockup <filename>. Run `etsyotomasyon drop calibrate` to see them.")
        raise typer.Exit(1)

    mockups = ws.mockup_files()
    if not mockups:
        _warn(f"No mockups in {ws.mockups}. Put your templates there first.")
        raise typer.Exit(1)

    # Measured from the files themselves: the whole reason areas are stored as fractions
    # is that one rectangle fits every sibling that happens to be the same size.
    sizes = mockup_mod.mockup_sizes(mockups)
    positions = mockup_mod.load_positions(ws.positions_path)

    selected = mockups
    if mockup_name:
        matches = _match_mockups(mockup_name, mockups)
        if not matches:
            _warn(f"No mockup named {mockup_name!r} in {ws.mockups}.")
            console.print("[dim]" + ", ".join(p.name for p in mockups) + "[/]")
            raise typer.Exit(1)
        if len(matches) > 1:
            _warn(
                f"{mockup_name!r} matches {', '.join(p.name for p in matches)}. "
                "Use the full filename."
            )
            raise typer.Exit(1)
        selected = matches

    changed: list[Path] = []
    if import_from is not None:
        legacy = mockup_mod.load_legacy_positions(import_from)
        dropped: list[str] = []
        converted = mockup_mod.import_pixel_positions(
            legacy, sizes, on_skip=lambda name, reason: dropped.append(f"{name} — {reason}")
        )
        for note in dropped:
            _warn(f"Skipped {note}")
        if not converted:
            _warn(f"Nothing in {import_from} named a mockup in {ws.mockups}.")
            raise typer.Exit(1)
        positions.update(converted)
        changed = [p for p in mockups if p.name in converted]
        selected = changed
    elif reset:
        target = selected[0]
        if positions.pop(target.name, None) is None:
            _warn(f"{target.name} has no saved area; it is already using the default.")
            raise typer.Exit(1)
        changed = [target]
    elif area:
        new_area = mockup_mod.parse_area(area)
        if same_size and not catalog_mod.same_size_names(ws, selected[0].name, sizes=sizes):
            _warn(
                f"Cannot read the pixel size of {selected[0].name}, "
                "so --same-size has nothing to match."
            )
            raise typer.Exit(1)
        # Writes positions.json itself unless this is a dry run.
        # --area moves the print; corners aside, each mockup keeps its realism and curve.
        names = catalog_mod.save_area(
            ws, selected[0].name, new_area, same_size=same_size, dry_run=dry_run, sizes=sizes,
            keep_style=True,
        )
        targets = [p for p in mockups if p.name in names]
        for target in targets:
            old = positions.get(target.name)
            positions[target.name] = (
                mockup_mod.PrintArea(new_area.x, new_area.y, new_area.w, new_area.h,
                                     realism=old.realism, curve=old.curve)
                if old is not None else new_area
            )
        changed = targets
        selected = targets

    if changed and not dry_run and not area:
        mockup_mod.save_positions(ws.positions_path, positions)

    _print_print_areas(
        selected,
        sizes,
        positions,
        changed={p.name for p in changed},
        saved=bool(changed) and not dry_run,
    )

    orphans = sorted(set(positions) - {p.name for p in mockups})
    if orphans:
        # A renamed mockup is the quiet way calibration stops working: the entry is
        # still there, the lookup is by filename, and nothing else would ever say so.
        _warn(
            f"{workspace_mod.POSITIONS_FILE} still names {', '.join(orphans)}, which is not "
            f"in {workspace_mod.MOCKUPS_DIR} any more. Those entries do nothing until a file "
            "with that exact name is back."
        )

    if preview or dry_run:
        # A dry run always draws: four fractions on their own tell the seller nothing
        # they could not have worked out on paper, and the picture is the point.
        console.print(f"\nPreviews in [cyan]{ws.calibration}[/]")
        for target in selected:
            out = ws.calibration / f"{target.stem}--area.jpg"
            try:
                mockup_mod.draw_preview(
                    target, out, positions.get(target.name, mockup_mod.DEFAULT_PRINT_AREA)
                )
            except EtsyotomasyonError as exc:
                # Already names the file; one unreadable template must not stop the rest.
                _warn(str(exc))
                continue
            console.print(f"[dim]{BULLET} {out.name}[/]")

    console.print()
    if not changed:
        example = ",".join(format(value, "g") for value in mockup_mod.DEFAULT_AREA)
        _ok(f"{len(selected)} mockup(s). Nothing was changed.")
        console.print(
            f"Move one: [cyan]etsyotomasyon drop calibrate --mockup <filename> --area {example}[/]\n"
            "Add [cyan]--dry-run[/] to draw it on the mockup before it is saved."
        )
        return
    if dry_run:
        _ok(f"Dry run: {len(changed)} print area(s) would change. Nothing was saved.")
        console.print("Look at the preview, then run the same command without [cyan]--dry-run[/].")
        return

    if reset:
        _ok(f"{changed[0].name} is back on the default print area.")
    else:
        _ok(f"Saved {len(changed)} print area(s) to {ws.positions_path}")
    if area and not same_size:
        size = sizes.get(changed[0].name)
        siblings = [p for p in mockups if p != changed[0] and size and sizes.get(p.name) == size]
        if siblings:
            console.print(
                f"[dim]{len(siblings)} other mockup(s) measure {size[0]}x{size[1]} as well. "
                "Add --same-size to give them the same rectangle.[/]"
            )
    console.print(
        "Then re-run [cyan]etsyotomasyon drop run[/]. Batches already in "
        f"{workspace_mod.DRAFTS_DIR} are left alone."
    )


def _match_mockups(name: str, mockups: list[Path]) -> list[Path]:
    """Accept `shirt-white.jpg`, or just `shirt-white`.

    positions.json is keyed by the filename with its extension, because that is what the
    compositor looks up — but nobody types the extension when one stem is unambiguous.
    """
    needle = name.strip().casefold()
    exact = [p for p in mockups if p.name.casefold() == needle]
    return exact or [p for p in mockups if p.stem.casefold() == needle]


def _print_print_areas(
    mockups: list[Path],
    sizes: dict[str, tuple[int, int]],
    positions: dict[str, mockup_mod.PrintArea],
    *,
    changed: set[str],
    saved: bool,
) -> None:
    """One row per mockup: the rectangle it uses, and where that rectangle came from."""
    table = Table(title=f"Print areas in {workspace_mod.MOCKUPS_DIR}", title_justify="left")
    table.add_column("mockup", style="cyan")
    table.add_column("size")
    table.add_column("area x,y,w,h")
    table.add_column("from")
    for path in mockups:
        size = sizes.get(path.name)
        stored = positions.get(path.name)
        area = stored or mockup_mod.DEFAULT_PRINT_AREA
        if path.name in changed:
            source = "changed" if saved else "not saved"
        else:
            source = workspace_mod.POSITIONS_FILE if stored else "default"
        table.add_row(
            path.name,
            f"{size[0]}x{size[1]}" if size else "unreadable",
            ",".join(format(value, "g") for value in (area.x, area.y, area.w, area.h))
            + (" (4 corners)" if area.quad is not None else ""),
            source,
        )
    console.print(table)


@drop_app.command("auto")
def drop_auto(
    path: Optional[Path] = typer.Option(None, "--path", help="Etsy Studio folder."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Prepare and validate offline, without uploading."),
    mockups: Optional[int] = typer.Option(None, "--mockups", min=0, help=_MOCKUPS_HELP),
    no_watermark: bool = typer.Option(False, "--no-watermark", help=_NO_WATERMARK_HELP),
    section: Optional[str] = typer.Option(None, "--section", help=_SECTION_HELP),
) -> None:
    """Prepare new products and upload Etsy drafts. Previously attempted products are skipped."""
    ws = _workspace(path).require()
    tmpl = template_mod.Template.from_dict(ws.read_template())
    chosen = _drop_mockups(ws, mockups, tmpl.listing_type)
    _drop_watermark(ws, tmpl, not no_watermark)
    _drop_info_images(ws)
    if dry_run:
        if section is not None:
            # A check sends nothing, but the section is still looked up (read only).
            with _client(require_auth=False) as client:
                tmpl = _drop_section(tmpl, section, client)
        report = automation.run(ws, tmpl, dry_run=True, mockups=chosen,
                                watermark=not no_watermark)
    else:
        with _client() as client:
            tmpl = _drop_section(tmpl, section, client)
            report = automation.run(ws, tmpl, client=client, mockups=chosen,
                                    watermark=not no_watermark)
    if report.prepared and report.prepared.csv_path:
        console.print(f"Review: {report.prepared.csv_path}")
        for product in report.prepared.ready:
            for warning in product.warnings:
                _warn(f"{product.source.name}: {warning}")
    for result in report.uploaded.results:
        _print_row_result(result)
    _ok(f"{len(report.already_done)} already uploaded; "
        f"{len(report.uploaded.results)} validated." if dry_run else
        f"Created {report.uploaded.created} draft(s); {len(report.already_done)} already uploaded.")
    for item in report.needs_review:
        _warn(f"Needs Etsy review before retrying: {item}")
    if report.needs_review or report.uploaded.errors or report.uploaded.partial:
        raise typer.Exit(1)


_WATERMARK_WHERE = {"center": "centre", "corner": "bottom-right corner",
                    "tiled": "repeated diagonally"}


@drop_app.command("watermark")
def drop_watermark(
    path: Optional[Path] = typer.Option(None, "--path", help="Etsy Studio folder."),
    file: Optional[Path] = typer.Option(
        None, "--file", help="A PNG (transparent background recommended), JPG or WebP, at "
        "most 10 MB. Replaces the current watermark.",
    ),
    on: Optional[bool] = typer.Option(None, "--on/--off", help="Switch the watermark on or off."),
    scope: Optional[str] = typer.Option(
        None, "--scope", help="digital (only listings of a download/both template) or all."),
    position: Optional[str] = typer.Option(
        None, "--position", help="center, corner (bottom right) or tiled (repeated diagonally)."),
    opacity: Optional[int] = typer.Option(None, "--opacity", help="10-90 (percent)."),
    size: Optional[int] = typer.Option(
        None, "--size", help="Percent of the photo width: 5-60 for center/corner, 5-40 for tiled."),
    remove: bool = typer.Option(False, "--remove", help="Delete the watermark picture."),
) -> None:
    """Show or set the watermark stamped on listing photos (never on download files).

    The same watermark.png and watermark.json the app's Mockups page sets, at the
    workspace root; `drop run`, `drop auto` and the app's runs all use it.
    """
    ws = _workspace(path).require()
    try:
        if remove:
            if watermark_mod.remove(ws):
                _ok(f"Removed {watermark_mod.watermark_path(ws)}.")
            else:
                _warn("There is no watermark to remove.")
        if file is not None:
            try:
                data = file.read_bytes()
            except OSError as exc:
                _fail(f"Cannot read {file}: {exc}")
                raise typer.Exit(1) from exc
            watermark_mod.save_upload(ws, file.name, data)
            _ok(f"Watermark set from {file.name}.")
        changes: dict[str, Any] = {}
        if on is not None:
            changes["enabled"] = on
        if scope is not None:
            changes["scope"] = scope
        if position is not None:
            changes["position"] = position
        if opacity is not None:
            changes["opacity"] = opacity
        if size is not None:
            target = position or watermark_mod.load_settings(ws).position
            changes["tile_size" if target == watermark_mod.TILED else "size"] = size
        if changes:
            watermark_mod.update_settings(ws, **changes)
    except watermark_mod.WatermarkError as exc:
        _fail(str(exc))
        raise typer.Exit(1) from exc
    except EtsyotomasyonError as exc:  # too many pixels
        _fail(str(exc))
        raise typer.Exit(1) from exc

    info = watermark_mod.describe(ws)
    settings = watermark_mod.load_settings(ws)
    table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
    table.add_row("picture", str(watermark_mod.watermark_path(ws)) if info["file"] else "none")
    table.add_row("state", "on" if info["on"] else "off")
    table.add_row("applies to", "digital products only" if settings.scope == "digital"
                  else "every listing")
    table.add_row("position", _WATERMARK_WHERE[settings.position])
    table.add_row("opacity", f"{settings.opacity}%")
    table.add_row("size", f"{settings.size}% of the photo width (center/corner), "
                  f"{settings.tile_size}% each (tiled)")
    console.print(table)
    if info["problem"]:
        _warn("The watermark picture cannot be read; set it again with --file.")
    elif not info["file"]:
        console.print("[dim]Set one with: etsyotomasyon drop watermark --file logo.png[/]")


# --- pinterest -----------------------------------------------------------------


def _pinterest() -> pinterest_mod.PinterestClient:
    return pinterest_mod.PinterestClient(pinterest_mod.PinterestConfig.load())


@pinterest_app.command("login")
def pinterest_login(
    paste: bool = typer.Option(False, "--paste", help="Paste the redirect address instead of listening."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Print the URL without opening it."),
) -> None:
    """Connect your own Pinterest account through your own Pinterest app."""
    config = pinterest_mod.PinterestConfig.load()
    token = pinterest_mod.login(config, paste=paste, open_browser=not no_browser)
    _ok(f"Pinterest connected. Token saved to {pinterest_mod.token_path()}")
    console.print(f"  scopes: {token.scope or ', '.join(pinterest_mod.SCOPES)}")


@pinterest_app.command("status")
def pinterest_status() -> None:
    """Show whether Pinterest is connected and what is waiting in the queue."""
    config = pinterest_mod.PinterestConfig.load()
    token = pinterest_mod.load_token()
    if config.access_token:
        console.print("token        PINTEREST_ACCESS_TOKEN from .env (not refreshable)")
    elif token is None:
        _warn("Pinterest is not connected. Run: etsyotomasyon pinterest login")
    else:
        left = int(token.expires_at - time.time()) if token.expires_at else 0
        console.print(f"token        {pinterest_mod.token_path()}")
        console.print(f"expires in   {'expired' if left <= 0 else f'{left // 3600}h'} (refreshed automatically)")
    console.print(f"environment  {'sandbox' if config.sandbox else 'production'}")
    counts = pinterest_mod.Queue.load().counts()
    console.print("queue        " + (", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "empty"))


@pinterest_app.command("logout")
def pinterest_logout() -> None:
    """Delete the stored Pinterest token."""
    if pinterest_mod.clear_token():
        _ok("Pinterest token deleted.")
    else:
        _warn("No Pinterest token was stored.")


@pinterest_app.command("boards")
def pinterest_boards() -> None:
    """List your boards, to pick one for `pinterest queue --board`."""
    with _pinterest() as client:
        boards = list(client.boards())
    if not boards:
        _warn("No boards on this account. Create one on Pinterest first.")
        raise typer.Exit(1)
    table = Table(title="Your Pinterest boards")
    table.add_column("board id")
    table.add_column("name")
    table.add_column("privacy")
    for board in boards:
        table.add_row(str(board.get("id")), _hide(board.get("name", "")), board.get("privacy", ""))
    console.print(table)


@pinterest_app.command("queue")
def pinterest_queue(
    listing_ids: list[int] = typer.Argument(..., help="Active listing ids to pin."),
    board: str = typer.Option(..., "--board", "-b", help="Board name or id (see `pinterest boards`)."),
    images: Optional[str] = typer.Option(
        None, "--images", help="Which image ranks to pin, e.g. 1-6 or 1,3,5. Default: all."
    ),
    per_day: int = typer.Option(2, "--per-day", help="Pins per day across the whole queue."),
    start: Optional[str] = typer.Option(None, "--start", help="First day, YYYY-MM-DD. Default: today."),
    ai_modified: bool = typer.Option(
        False, "--ai-modified",
        help="Declare the images AI-created or AI-modified, as Pinterest asks.",
    ),
    description: Optional[str] = typer.Option(
        None, "--description", help="Pin description. Default: built from the title and tags."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the schedule without saving it."),
) -> None:
    """Queue Pins for published listings, spread over days. Nothing is posted here."""
    ranks = pinterest_mod.parse_ranks(images)
    try:
        first_day = date.fromisoformat(start) if start else date.today()
    except ValueError as exc:
        raise typer.BadParameter("--start takes YYYY-MM-DD") from exc

    with _pinterest() as pin_client:
        target = pinterest_mod.resolve_board(pin_client.boards(), board)
    pins: list[dict[str, Any]] = []
    with _client() as etsy:
        for listing_id in listing_ids:
            listing = etsy.listing(listing_id)
            listing_images = (etsy.get(f"/listings/{listing_id}/images") or {}).get("results") or []
            pins += pinterest_mod.pins_for_listing(
                listing, listing_images, str(target["id"]),
                ranks=ranks, ai_modified=ai_modified, description=description,
            )

    queue = pinterest_mod.Queue.load()
    added = queue.add(pins, start=first_day, per_day=per_day)
    table = Table(title=f"{len(added)} Pin(s) for board {_hide(target.get('name', ''))!s}")
    table.add_column("due")
    table.add_column("listing")
    table.add_column("image")
    table.add_column("title")
    for entry in added:
        table.add_row(entry["due"], _hide(entry["listing_id"], "id"), str(entry["rank"]),
                      _hide(entry["payload"]["title"][:60]))
    console.print(table)
    skipped = len(pins) - len(added)
    if skipped:
        _warn(f"{skipped} Pin(s) were already queued for that board and were skipped.")
    if dry_run:
        _ok("Dry run: nothing was saved.")
        return
    queue.save()
    _ok(f"Queued {len(added)} Pin(s) in {queue.path}")
    console.print("Post the due ones with: [cyan]etsyotomasyon pinterest post[/] — run it once a day.")


@pinterest_app.command("post")
def pinterest_post(
    limit: Optional[int] = typer.Option(None, "--max", help="Post at most this many now."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what is due without posting."),
) -> None:
    """Post the Pins that are due today. Meant to run once a day from a scheduler."""
    queue = pinterest_mod.Queue.load()
    if not queue.due(date.today()):
        _ok("Nothing is due today.")
        return

    def report(entry: dict[str, Any]) -> None:
        mark = {"posted": f"[green]{TICK}[/]", "failed": f"[red]{CROSS}[/]",
                "uncertain": "[yellow]?[/]"}.get(entry["status"], "·")
        note = f" — {entry['message']}" if entry.get("message") else ""
        console.print(f"{mark} {entry['due']} listing {_hide(entry['listing_id'], 'id')} "
                      f"image {entry['rank']}{note}")

    if dry_run:
        done = pinterest_mod.post_due(None, queue, today=date.today(), limit=limit,  # type: ignore[arg-type]
                                      dry_run=True, on_progress=report)
        _ok(f"Dry run: {len(done)} Pin(s) are due. Nothing was posted.")
        return
    with _pinterest() as client:
        done = pinterest_mod.post_due(client, queue, today=date.today(), limit=limit, on_progress=report)
    posted = sum(1 for e in done if e["status"] == "posted")
    _ok(f"Posted {posted} of {len(done)} due Pin(s).")
    uncertain = [e for e in done if e["status"] == "uncertain"]
    if uncertain:
        _warn(
            f"{len(uncertain)} Pin(s) may or may not have been created — check the board. "
            "They are not retried automatically, so a duplicate is never posted."
        )


@pinterest_app.command("retry")
def pinterest_retry(
    listing_id: int = typer.Argument(..., help="The listing whose Pins to put back in the queue."),
    image: Optional[int] = typer.Option(None, "--image", help="Only this image rank."),
) -> None:
    """Re-queue failed or uncertain Pins for today — only after checking the board.

    An uncertain Pin may already exist on Pinterest. Look at the board first; retrying
    one that did land posts it twice.
    """
    queue = pinterest_mod.Queue.load()
    today = date.today().isoformat()
    touched = 0
    for entry in queue.entries:
        if entry["listing_id"] != listing_id or entry["status"] not in {"failed", "uncertain", "sending"}:
            continue
        if image is not None and entry["rank"] != image:
            continue
        entry.update(status="pending", due=today, message="")
        touched += 1
    if not touched:
        _warn("Nothing to retry for that listing.")
        raise typer.Exit(1)
    queue.save()
    _ok(f"{touched} Pin(s) are due again today. Post them with: etsyotomasyon pinterest post")


@pinterest_app.command("list")
def pinterest_list(
    show: int = typer.Option(20, "--show", help="How many upcoming Pins to list."),
) -> None:
    """Show the queue: what is posted, waiting, or needs a look."""
    queue = pinterest_mod.Queue.load()
    if not queue.entries:
        _ok("The Pin queue is empty.")
        return
    console.print(", ".join(f"{n} {s}" for s, n in sorted(queue.counts().items())))
    upcoming = [e for e in queue.entries if e["status"] in {"pending", "uncertain", "sending", "failed"}]
    table = Table()
    for col in ("due", "status", "listing", "image", "note"):
        table.add_column(col)
    for entry in sorted(upcoming, key=lambda e: e["due"])[:show]:
        table.add_row(entry["due"], entry["status"], _hide(entry["listing_id"], "id"),
                      str(entry["rank"]), (entry.get("message") or "")[:60])
    console.print(table)


# --- shops ---------------------------------------------------------------------


@shops_app.command("list")
def shops_list() -> None:
    """Show every shop set up on this computer, and which one commands use."""
    active = shops_mod.current().id
    table = Table()
    for col in ("", "id", "shop", "connected", "keys and token in"):
        table.add_column(col)
    for shop in shops_mod.all_shops():
        row = shops_mod.describe(shop)
        table.add_row(
            "→" if shop.id == active else "",
            row["id"],
            _hide(row["name"], "shop") if shop.name else row["name"],
            row["connected"],
            row["home"],
        )
    console.print(table)
    console.print("[dim]Use one with --shop <id>, e.g.[/] [cyan]etsyotomasyon --shop shop-2 shop info[/]")


@shops_app.command("add")
def shops_add() -> None:
    """Make room for one more shop, with its own keys and its own sign-in."""
    shop = shops_mod.add()
    _ok(f"Added shop {shop.id} at {shop.home}")
    console.print(
        "Give it keys and connect it:\n"
        f"  [cyan]etsyotomasyon --shop {shop.id} init --path \"{shop.home / '.env'}\"[/]\n"
        f"  [cyan]etsyotomasyon --shop {shop.id} auth login[/]"
    )


@shops_app.command("remove")
def shops_remove(
    shop_id: str = typer.Argument(..., help="The id `etsyotomasyon shops list` shows."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
) -> None:
    """Forget one extra shop on this computer. The shop on Etsy is not touched."""
    if not yes and not typer.confirm(
        f"Delete the keys, token and queue stored for {shop_id} on this computer?"
    ):
        _warn("Cancelled.")
        raise typer.Exit(1)
    shops_mod.remove(shop_id)
    _ok(f"Removed {shop_id}. Its listings and orders on Etsy are unchanged.")


def main() -> None:
    """Entry point. Turns library errors into clean CLI output."""
    try:
        app()
    except EtsyotomasyonError as exc:
        err_console.print(f"[bold red]Error:[/] {exc}")
        hint = getattr(exc, "hint", None)
        if callable(hint) and hint():
            err_console.print(f"[yellow]→[/] {hint()}")
        sys.exit(1)
    except KeyboardInterrupt:
        err_console.print("\n[yellow]Cancelled.[/]")
        sys.exit(130)


if __name__ == "__main__":
    main()
