"""The setup checklist: work out exactly what is missing, and say what to do about it.

Most of the friction in a tool like this is not the code — it is the eight things
that must all be true before a single request can succeed. Some can be detected, some
can only be asked, and the difference matters: a check that quietly assumes is worse
than one that asks.

So each step reports one of three states. `ok` means it was verified. `missing` means
it was verified as absent, with the exact command to fix it. `unknown` means only you
can tell us — you get asked, and if you are not there to answer nothing is assumed.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import auth
from .client import EtsyClient
from .config import Config, token_path
from .drop import workspace as workspace_mod
from .errors import EtsyotomasyonError

OK = "ok"
MISSING = "missing"
UNKNOWN = "unknown"
WARN = "warn"

MIN_PYTHON = (3, 9)


@dataclass
class StepResult:
    state: str = UNKNOWN
    detail: str = ""
    fix: list[str] = field(default_factory=list)


@dataclass
class Step:
    number: int
    title: str
    check: Callable[[], StepResult]
    question: str = ""
    """Asked only when `check` returns UNKNOWN — things no code can verify."""
    required: bool = True
    """A non-required step is useful but not blocking, e.g. the drop workspace."""


# --- the individual checks ------------------------------------------------------


def check_python() -> StepResult:
    version = ".".join(str(p) for p in sys.version_info[:3])
    if sys.version_info[:2] < MIN_PYTHON:
        return StepResult(
            MISSING,
            f"Python {version} is too old",
            [f"etsyotomasyon needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer.",
             "Install a newer Python from https://www.python.org/downloads/"],
        )
    return StepResult(OK, f"Python {version}")


def check_installed() -> StepResult:
    from . import __version__

    return StepResult(OK, f"etsyotomasyon {__version__}")


def check_shop() -> StepResult:
    """Only answerable by the person, until a token exists."""
    token = auth.load_token()
    if token is None:
        return StepResult(UNKNOWN)
    return StepResult(OK, "a shop is connected, so one exists")


def check_app() -> StepResult:
    """A keystring in the environment is proof an app was created."""
    try:
        config = Config.load()
    except EtsyotomasyonError:
        return StepResult(UNKNOWN)
    return StepResult(OK, f"keystring {config.keystring[:6]}… is configured")


def check_credentials() -> StepResult:
    try:
        config = Config.load()
    except EtsyotomasyonError as exc:
        first = str(exc).splitlines()[0]
        return StepResult(
            MISSING,
            first,
            ["Run: etsyotomasyon init",
             "It asks for your keystring and shared secret, types the secret hidden,",
             "and writes a .env you never commit.",
             "Both halves are required — Etsy returns 403 on every call with only one."],
        )
    return StepResult(
        OK, f"keystring {config.keystring[:6]}…, shared secret {len(config.shared_secret)} chars"
    )


def check_redirect() -> StepResult:
    try:
        config = Config.load(require_key=False)
        auth.validate_redirect_uri(config.redirect_uri)
    except EtsyotomasyonError as exc:
        return StepResult(
            MISSING,
            str(exc).splitlines()[0],
            ["Set ETSY_REDIRECT_URI in your .env, for example:",
             "  ETSY_REDIRECT_URI=http://localhost:3003/oauth/redirect",
             "http and https are both fine. The host must be a domain name —",
             "'localhost' works, '127.0.0.1' is rejected by Etsy."],
        )
    return StepResult(OK, config.redirect_uri)


def check_callback_registered() -> StepResult:
    """Nothing here can see the other side of Etsy's settings screen."""
    if auth.load_token() is not None:
        return StepResult(OK, "a login has already succeeded, so it matches")
    return StepResult(UNKNOWN)


def check_api_reachable() -> StepResult:
    try:
        config = Config.load()
    except EtsyotomasyonError:
        return StepResult(MISSING, "no credentials to test with", ["Finish step 4 first."])
    try:
        with EtsyClient(config, require_auth=False) as client:
            client.ping()
    except EtsyotomasyonError as exc:
        return StepResult(
            MISSING,
            str(exc).splitlines()[0],
            ["Etsy refused the credential. Check BOTH halves on your app page at",
             "https://www.etsy.com/developers/your-apps — the keystring and the",
             "shared secret are different values and both must be exact.",
             "Then re-run: etsyotomasyon init --force"],
        )
    return StepResult(OK, "Etsy accepted your keystring and shared secret")


def check_connected() -> StepResult:
    token = auth.load_token()
    if token is None:
        return StepResult(
            MISSING,
            "no shop connected yet",
            ["Run: etsyotomasyon auth login",
             "A browser opens, you approve, and the token is stored in",
             f"{token_path()} with 0600 permissions."],
        )
    if token.expired:
        return StepResult(WARN, "token expired — it refreshes itself on the next call")
    missing = token.missing_scopes(Config.load(require_key=False).scopes)
    if missing:
        return StepResult(
            WARN,
            f"connected, but Etsy granted fewer scopes: missing {' '.join(missing)}",
            ["Commands needing those will fail with 403.",
             "Run `etsyotomasyon auth login` again and approve everything."],
        )
    return StepResult(OK, f"connected, token valid for {token.seconds_left // 60} min")


def check_workspace(root: Path | None = None) -> StepResult:
    root = root or workspace_mod.default_root()
    ws = workspace_mod.Workspace(root)
    if not root.is_dir():
        return StepResult(
            MISSING,
            "no drop workspace",
            ["Only needed for `etsyotomasyon drop`. Bulk CSV and SEO work without it.",
             "Run: etsyotomasyon drop init"],
        )
    mockups = len(ws.mockup_files())
    designs = len(ws.product_files())
    has_template = ws.template_path.exists()

    if not has_template:
        return StepResult(
            MISSING,
            f"{root} exists, but no product template",
            ["Build ONE listing properly in Etsy by hand, then copy its settings:",
             "  etsyotomasyon drop template --from-listing <listing_id>",
             "Category, shipping profile, price and processing times cannot be",
             "guessed from a picture — that is why one real listing is required."],
        )
    if not mockups:
        return StepResult(
            WARN,
            f"template ready, but no mockups in {ws.mockups.name}",
            ["Transparent artwork needs a mockup to sit on.",
             "Finished product photos do not — those are used as they are."],
        )
    return StepResult(OK, f"{mockups} mockup(s), {designs} design(s) waiting, template ready")


# --- the checklist --------------------------------------------------------------


def build_steps(workspace: Path | None = None) -> list[Step]:
    return [
        Step(1, "Python 3.9 or newer", check_python),
        Step(2, "etsyotomasyon installed", check_installed),
        Step(
            3,
            "An Etsy shop that is open",
            check_shop,
            question=(
                "Do you already have an Etsy shop open? etsyotomasyon manages a shop, it "
                "cannot create one"
            ),
        ),
        Step(
            4,
            "An Etsy API app",
            check_app,
            question=(
                "Have you created an app at https://www.etsy.com/developers/register-seller-app ? "
                "It is free, and personal use is usually approved quickly"
            ),
        ),
        Step(5, "Keystring AND shared secret in .env", check_credentials),
        Step(6, "A callback URL set", check_redirect),
        Step(
            7,
            "That same callback registered on your Etsy app",
            check_callback_registered,
            question=(
                "Have you added that exact URL to your app's callback list on Etsy? "
                "It must match byte for byte"
            ),
        ),
        Step(8, "Etsy accepts the credential", check_api_reachable),
        Step(9, "Your shop connected", check_connected),
        Step(10, "Drop workspace (only for `etsyotomasyon drop`)",
             lambda: check_workspace(workspace), required=False),
    ]


ANSWER_FIXES = {
    3: [
        "Open a shop at https://www.etsy.com/sell first.",
        "etsyotomasyon works on an existing shop — it does not create one.",
    ],
    4: [
        "Create one at https://www.etsy.com/developers/register-seller-app",
        "App name without 'Etsy'; say it is your own tool for your own shop, run on your computer.",
        "You will be given a Keystring and a Shared secret. You need BOTH.",
    ],
    7: [
        "Open the Dashboard at https://www.etsy.com/developers/",
        "In your app's ⋮ menu choose 'Edit callback URLs' and add the exact string from step 6.",
        "Etsy's own rules: http:// or https://, the host must be a domain name,",
        "and IP addresses are rejected — use 'localhost', never '127.0.0.1'.",
    ],
}


def next_command(results: list[tuple[Step, StepResult]]) -> str:
    """The single next thing to run, so nobody has to work it out.

    Prefers a required step, but falls through to an optional one when everything
    required is already done.
    """
    missing = [(s, r) for s, r in results if r.state == MISSING]
    required = [(s, r) for s, r in missing if s.required]

    # While something required is missing, an optional step's command is the wrong
    # advice — "run drop init" is actively misleading when the real answer is
    # "open an Etsy shop first". Some required steps have no command at all, and in
    # that case saying nothing beats sending someone down the wrong path.
    for _step, result in required or missing:
        for line in result.fix:
            if line.startswith("Run: "):
                return line[len("Run: "):]
    return ""
