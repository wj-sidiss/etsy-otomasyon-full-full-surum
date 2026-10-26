"""etsyotomasyon in the browser: a local web server the app opens at http://localhost:3000.

`launch()` is what double-clicking the app runs; `self_test()` is what the release
workflow runs on the packaged app to prove it is complete.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
import traceback
import urllib.parse
from pathlib import Path
from typing import Any

logging.getLogger("etsyotomasyon.web").addHandler(logging.NullHandler())

SELF_TEST_TIMEOUT = 60.0


def launch(*, port: int | None = None, open_browser: bool = True) -> int:
    """Start the server (or find the one already running) and open the browser."""
    from .launcher import launch as _launch

    return _launch(port=port, open_browser=open_browser)


# Everything a shop's .env may set, cleared for the self-test so the machine's own
# configuration cannot change its answers.
_ISOLATE = (
    "ETSYOTOMASYON_HOME",
    "ETSYOTOMASYON_SHOP",
    "ETSYOTOMASYON_IGNORE_CWD_ENV",
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


def self_test(*, timeout: float = SELF_TEST_TIMEOUT) -> int:
    """Prove a build is complete: modules import, every static file is served with
    the right type, the session and status work offline, the string files match.

    Never opens a browser, never touches the real ~/.etsyotomasyon, and gives up after
    `timeout` seconds instead of hanging a release job. Prints the outcome; returns
    0 when everything passed.
    """
    from .. import __version__

    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["problems"] = _self_test_steps()
        except BaseException:  # noqa: BLE001 — reported, never raised into the bootloader
            outcome["problems"] = ["crashed:\n" + traceback.format_exc()]

    saved = {name: os.environ.get(name) for name in _ISOLATE}
    home = tempfile.mkdtemp(prefix="etsyotomasyon-self-test-")
    for name in _ISOLATE:
        os.environ.pop(name, None)
    os.environ["ETSYOTOMASYON_HOME"] = home
    os.environ["ETSYOTOMASYON_IGNORE_CWD_ENV"] = "1"
    try:
        worker = threading.Thread(target=run, name="etsyotomasyon-self-test", daemon=True)
        worker.start()
        worker.join(timeout)
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(home, ignore_errors=True)
    if worker.is_alive():
        print(f"self-test: did not finish within {timeout:.0f} s", flush=True)
        return 1
    problems = outcome.get("problems") or []
    for problem in problems:
        print(f"self-test: {problem}", flush=True)
    if problems:
        return 1
    print(f"etsyotomasyon {__version__} self-test passed", flush=True)
    return 0


def _self_test_steps() -> list[str]:
    import httpx

    from .. import cli  # noqa: F401 — importing every command is part of the test
    from ..desktop import icon
    from . import api, i18n  # noqa: F401
    from .context import AppContext
    from .files import content_type_for
    from .server import COOKIE, STATIC_DIR, WebServer

    problems: list[str] = []
    icon.render(64)

    # The default products folder is on the real Desktop; keep the test off it.
    from ..config import base_home
    from ..desktop import settings

    settings.save_shop_prefs({"workspace": str(base_home() / "Etsy Studio")})
    token = "self-test-" + os.urandom(8).hex()
    ctx = AppContext(token=token)
    server = WebServer(ctx, 0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2},
                              daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.port}"
    try:
        with httpx.Client(base_url=base, trust_env=False, verify=False, timeout=10.0) as http:
            resp = http.get("/")
            if resp.status_code != 200 or not resp.headers.get("content-type", "").startswith(
                "text/html"
            ):
                problems.append(f"/ answered {resp.status_code} {resp.headers.get('content-type')}")

            files = sorted(p for p in Path(STATIC_DIR).rglob("*") if p.is_file())
            if not files:
                problems.append(f"no static files in {STATIC_DIR}")
            for path in files:
                rel = path.relative_to(STATIC_DIR).as_posix()
                resp = http.get("/" + urllib.parse.quote(rel))
                expected = content_type_for(path)
                if resp.status_code != 200:
                    problems.append(f"/{rel} answered {resp.status_code}")
                elif resp.headers.get("content-type") != expected:
                    problems.append(
                        f"/{rel} came as {resp.headers.get('content-type')}, not {expected}"
                    )

            resp = http.get("/api/ping")
            data = resp.json() if resp.status_code == 200 else {}
            if data.get("app") != "etsyotomasyon" or data.get("instance") != ctx.instance:
                problems.append(f"/api/ping answered {resp.status_code} {data}")

            if http.get("/api/session").status_code != 401:
                problems.append("/api/session answered without a session")
            resp = http.get(f"/?k={token}")
            if resp.status_code != 302 or COOKIE not in http.cookies:
                problems.append(f"the session link answered {resp.status_code} without a cookie")
            resp = http.get("/api/session")
            if resp.status_code != 200:
                problems.append(f"/api/session answered {resp.status_code} with a session")
            resp = http.get("/api/status")
            state = resp.json().get("state") if resp.status_code == 200 else None
            if state != "keys":
                problems.append(f"/api/status gave {resp.status_code} state={state!r}, not 'keys'")

        problems.extend(i18n.check_files(Path(STATIC_DIR) / "i18n"))
    finally:
        server.stop()
        thread.join(5.0)
        server.server_close()
        ctx.close()
    return problems
