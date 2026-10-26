"""Start the app: one server per computer, the browser pointed at it, and a quiet exit.

Double-clicking etsyotomasyon a second time does not start a second server: the first
one is found through `~/.etsyotomasyon/web.json` and a ping, and the browser is simply
pointed at it again. Two launches at the same moment (a double double-click) take
turns through a lock file (`~/.etsyotomasyon/web.lock`), so the second one finds the
first. After an update, a still running older version is asked to quit first (not
while it runs a task). The server stops by itself once no tab has been open (and no
task running) for a while, so closing the browser is all it takes to quit.
"""

from __future__ import annotations

import errno
import logging
import os
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import date
from pathlib import Path
from typing import Any

from .. import __version__
from ..config import base_home, read_json, write_json_private
from ..errors import ConfigError

log = logging.getLogger("etsyotomasyon.web")

# 3003 is the Etsy sign-in listener and 8085 the Pinterest one: never ours.
PORTS = (3000, 3001, 3002, 3004, 3005, 3006, 3007, 3008, 3009, 3010, 0)
RESERVED_PORTS = frozenset({3003, 8085})
STATE_FILE = "web.json"
LOCK_FILE = "web.lock"
# How long a launch waits for another one that is starting at the same moment.
LOCK_WAIT = 30.0
# How long an older version gets to stop after it was asked to quit.
QUIT_WAIT = 15.0
PING_TIMEOUT = 1.5
IDLE_TIMEOUT = 90.0
START_GRACE = 180.0
WATCH_INTERVAL = 5.0


def state_path() -> Path:
    return base_home() / STATE_FILE


def browser_url(port: int, token: str | None = None, path: str = "/") -> str:
    """The address the browser opens. Always `localhost`, as the video shows it."""
    path = path if path.startswith("/") else f"/{path}"
    query = f"?k={token}" if token else ""
    return f"http://localhost:{port}{path}{query}"


def instance_of(token: str) -> str:
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def probe(port: int, token: str, timeout: float = PING_TIMEOUT) -> dict[str, Any] | None:
    """/api/ping's answer when our app, started with `token`, answers on `port`."""
    import httpx

    try:
        # Plain http to this machine: no TLS context to build (verify would cost 0.3 s).
        with httpx.Client(trust_env=False, verify=False, timeout=timeout) as http:
            resp = http.get(f"http://127.0.0.1:{port}/api/ping")
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    if (
        resp.status_code == 200
        and isinstance(data, dict)
        and data.get("app") == "etsyotomasyon"
        and data.get("instance") == instance_of(token)
    ):
        return data
    return None


def ping(port: int, token: str, timeout: float = PING_TIMEOUT) -> bool:
    """True when our app, started with `token`, answers on `port`."""
    return probe(port, token, timeout) is not None


def find_running() -> dict[str, Any] | None:
    """The web.json of a server that is up and answering, or None.

    Its "version" is the one the server itself reports.
    """
    try:
        data = read_json(state_path())
    except ConfigError:
        return None
    if not isinstance(data, dict):
        return None
    port, token = data.get("port"), data.get("token")
    if not isinstance(port, int) or not isinstance(token, str) or not token:
        return None
    answer = probe(port, token)
    if answer is None:
        return None
    return {**data, "version": answer.get("version")}


def ask_to_quit(running: dict[str, Any], *, wait: float = QUIT_WAIT) -> str:
    """Ask a running server (an older version) to stop, as its Quit button would.

    "stopped" once it has let go of web.json, "busy" when it is running a task (it is
    left alone), "failed" when it did not answer or did not stop in `wait` seconds.
    """
    import httpx

    from .server import COOKIE

    port, token = running["port"], running["token"]
    try:
        with httpx.Client(trust_env=False, verify=False, timeout=5.0) as http:
            resp = http.post(
                f"http://127.0.0.1:{port}/api/quit",
                json={},
                headers={"X-Etsyotomasyon": "1", "Cookie": f"{COOKIE}={token}"},
            )
    except httpx.HTTPError:
        return "failed"
    if resp.status_code == 409:
        return "busy"
    if resp.status_code != 200:
        return "failed"
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            data = read_json(state_path())
        except ConfigError:
            data = None
        gone = not (isinstance(data, dict) and data.get("token") == token)
        if gone and not ping(port, token, timeout=0.5):
            return "stopped"
        time.sleep(0.1)
    return "failed"


class LaunchLock:
    """An exclusive lock on `~/.etsyotomasyon/web.lock`, held while a launch looks for a
    running server and, finding none, starts its own and writes web.json.

    The operating system drops it when the process ends, however it ends, so a
    crashed launch never blocks the next one. The file itself stays.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or base_home() / LOCK_FILE
        self._fd: int | None = None

    def acquire(self, timeout: float = LOCK_WAIT) -> bool:
        """True once held; False after `timeout` seconds (or if locking is impossible)."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            log.warning("could not open %s", self.path)
            return False
        deadline = time.monotonic() + timeout
        while True:
            if _try_lock(fd):
                self._fd = fd
                return True
            if time.monotonic() >= deadline:
                os.close(fd)
                return False
            time.sleep(0.05)

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock(fd)
        finally:
            os.close(fd)

    @property
    def held(self) -> bool:
        return self._fd is not None


def _try_lock(fd: int) -> bool:
    try:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def write_state(port: int, token: str) -> None:
    write_json_private(
        state_path(), {"pid": os.getpid(), "port": port, "token": token, "version": __version__}
    )


def clear_state(token: str) -> None:
    """Remove web.json, but only if it still describes this server."""
    try:
        data = read_json(state_path())
    except ConfigError:
        data = None
    if isinstance(data, dict) and data.get("token") == token:
        try:
            state_path().unlink()
        except OSError:
            pass


def candidate_ports(port: int | None = None) -> list[int]:
    ports = list(PORTS)
    if port:
        if port in RESERVED_PORTS:
            raise ValueError(f"Port {port} is reserved for signing in to Etsy or Pinterest.")
        ports = [port] + [p for p in ports if p != port]
    return ports


# The address or the whole address family does not exist: no IPv6 on this machine.
_NO_IPV6 = {errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT, 10047, 10049}


def bind(
    ctx: Any, port: int | None = None, *, ports: list[int] | None = None, ipv6: bool = True
) -> Any:
    """A WebServer on the first free port of `ports` (default: candidate_ports(port)).

    With ipv6, the same port is also taken on ::1 (`server.companion`): "localhost"
    resolves to ::1 first, so without it a browser waits for that attempt to fail
    on every new connection — and if some other program holds ::1 on a port, the
    browser would reach that program instead of us, so such a port counts as busy.
    Machines without IPv6 get the 127.0.0.1 listener only.
    """
    import socket

    from .server import WebServer

    last: OSError | None = None
    for candidate in ports if ports is not None else candidate_ports(port):
        for _attempt in range(5 if candidate == 0 else 1):
            try:
                server = WebServer(ctx, candidate)
            except OSError as exc:  # in use (EADDRINUSE / WSAEADDRINUSE) or refused: next
                last = exc
                break
            if not ipv6 or not socket.has_ipv6:
                return server
            try:
                server.companion = WebServer(
                    ctx, server.port, family=socket.AF_INET6,
                    router=server.router, static_dir=server.static_dir,
                )
                return server
            except OSError as exc:
                if exc.errno in _NO_IPV6 or getattr(exc, "winerror", None) in _NO_IPV6:
                    return server
                server.server_close()
                last = exc
    raise OSError(f"No free port for etsyotomasyon ({last})")


class IdleWatchdog(threading.Thread):
    """Stops the server once nobody is using it.

    Idle means: no event stream open and no job queued or running. The limit is
    `idle_timeout` after the last tab closed, or `grace` after start if no tab
    ever connected (a browser that failed to open should not leave a server behind).
    """

    def __init__(
        self,
        ctx: Any,
        on_idle: Any,
        *,
        idle_timeout: float = IDLE_TIMEOUT,
        grace: float = START_GRACE,
        interval: float = WATCH_INTERVAL,
    ) -> None:
        super().__init__(name="etsyotomasyon-idle", daemon=True)
        self.ctx = ctx
        self.on_idle = on_idle
        self.idle_timeout = idle_timeout
        self.grace = grace
        self.interval = interval
        self.started = time.monotonic()
        self.last_busy = 0.0
        self._stop_event = threading.Event()

    def cancel(self) -> None:
        self._stop_event.set()

    def check(self, now: float | None = None) -> bool:
        """One look: True when the server has been idle long enough to stop."""
        now = time.monotonic() if now is None else now
        hub = self.ctx.events
        if hub.client_count:
            return False  # closing the last tab sets hub.idle_since
        if self.ctx.jobs.busy():
            self.last_busy = now  # a job that just ended must not count as idle time
            return False
        if hub.ever_connected:
            since, limit = max(hub.idle_since, self.last_busy), self.idle_timeout
        else:
            since, limit = max(self.started, self.last_busy), self.grace
        return now - since >= limit

    def run(self) -> None:
        while not self._stop_event.wait(self.interval):
            if self.check():
                log.info("no tab open and nothing running: stopping")
                self.on_idle()
                return


def _ensure_streams() -> None:
    """A windowed build has no stdout/stderr: send them to the log file instead."""
    if sys.stdout is None or sys.stderr is None:
        from ..desktop import _log_to_file

        _log_to_file()


def _setup_logging() -> logging.Handler | None:
    """Log to ~/.etsyotomasyon/logs/web-<date>.log; returns the handler to remove later."""
    logger = logging.getLogger("etsyotomasyon.web")
    try:
        folder = base_home() / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(folder / f"web-{date.today().isoformat()}.log",
                                      encoding="utf-8")
    except OSError:
        return None
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return handler


def _stop_logging(handler: logging.Handler | None) -> None:
    if handler is not None:
        logging.getLogger("etsyotomasyon.web").removeHandler(handler)
        handler.close()


def _open_browser(url: str, *, wait: bool = False) -> None:
    """Point the browser at `url`. The full address (with its ?k= key) is printed as
    well: it is the way in when no browser opens (none registered, --no-browser)."""

    def run() -> None:
        try:
            opened = webbrowser.open(url)
        except Exception:  # noqa: BLE001 — the printed address is the fallback
            opened = False
        if not opened:
            log.warning("could not open the browser")
            print("No browser opened by itself: open the address above in one.", flush=True)

    if wait:
        run()
    else:
        threading.Thread(target=run, name="etsyotomasyon-browser", daemon=True).start()


def launch(
    *,
    port: int | None = None,
    open_browser: bool = True,
    idle_timeout: float = IDLE_TIMEOUT,
    grace: float = START_GRACE,
    watch_interval: float = WATCH_INTERVAL,
    ports: list[int] | None = None,
    ready: Any = None,
) -> int:
    """Run the app until it is quit or left idle. Returns an exit code.

    The last four parameters exist for tests: shorter idle limits, which ports to
    try, and `ready(server)`, called on its own thread as the server starts serving.
    """
    os.environ["ETSYOTOMASYON_IGNORE_CWD_ENV"] = "1"
    _ensure_streams()
    handler = _setup_logging()
    try:
        return _run(port=port, open_browser=open_browser, idle_timeout=idle_timeout,
                    grace=grace, watch_interval=watch_interval, ports=ports, ready=ready)
    finally:
        _stop_logging(handler)


def _run(
    *,
    port: int | None,
    open_browser: bool,
    idle_timeout: float,
    grace: float,
    watch_interval: float,
    ports: list[int] | None,
    ready: Any,
) -> int:
    lock = LaunchLock()
    if not lock.acquire():
        log.warning("another launch kept %s for too long; starting anyway", lock.path)
    try:
        running = find_running()
        if running is not None and running.get("version") != __version__:
            running = _replace_older(running, open_browser=open_browser)
            if running is not None:
                return 0  # it stays: it is running a task (the person was told)
        if running is not None:
            url = browser_url(running["port"], running["token"])
            print(f"etsyotomasyon is already running at {url}", flush=True)
            if open_browser:
                _open_browser(url, wait=True)
            return 0
        started = _start(port, ports)
    finally:
        lock.release()  # web.json names the new server (or there is none): next, please
    if started is None:
        return 1
    ctx, server, token = started
    return _serve(ctx, server, token, open_browser=open_browser, idle_timeout=idle_timeout,
                  grace=grace, watch_interval=watch_interval, ready=ready)


def _replace_older(running: dict[str, Any], *, open_browser: bool) -> dict[str, Any] | None:
    """An older (or newer) version is running: ask it to quit so this one can start.

    Returns None once it has stopped; otherwise `running`, which then stays open (it
    is running a task, or did not stop) and the browser is pointed at it with a note
    that this version is waiting.
    """
    old = running.get("version") or "?"
    outcome = ask_to_quit(running)
    if outcome == "stopped":
        log.info("stopped the running etsyotomasyon %s to start %s", old, __version__)
        print(f"Stopped etsyotomasyon {old} to start {__version__}.", flush=True)
        return None
    url = browser_url(running["port"], running["token"])
    if outcome == "busy":
        log.warning("etsyotomasyon %s is running a task: %s not started", old, __version__)
        print(f"etsyotomasyon {old} is still running a task, so {__version__} did not start. "
              "Start etsyotomasyon again once the task has finished.", flush=True)
    else:
        log.warning("etsyotomasyon %s did not stop: %s not started", old, __version__)
        print(f"etsyotomasyon {old} is still running and did not stop, so {__version__} did not "
              "start. Quit it (shop menu > Quit), then start etsyotomasyon again.", flush=True)
    print(f"etsyotomasyon {old} is at {url}", flush=True)
    if open_browser:
        # The page it opens tells the person why (app.js reads ?newer=).
        waiting = f"{url}&newer={urllib.parse.quote(__version__)}"
        _open_browser(waiting, wait=True)
    return running


def _start(port: int | None, ports: list[int] | None) -> tuple[Any, Any, str] | None:
    """A new context and server, bound, with web.json written; None if no port."""
    from .context import AppContext

    token = secrets.token_urlsafe(32)
    ctx = AppContext(token=token)
    try:
        server = bind(ctx, port, ports=ports)
    except (OSError, ValueError) as exc:
        ctx.close()
        print(f"etsyotomasyon could not start its local server: {exc}", flush=True)
        return None
    try:
        write_state(server.port, token)
    except (OSError, ConfigError) as exc:
        log.warning("could not write %s: %s", STATE_FILE, exc)
    return ctx, server, token


def _serve(
    ctx: Any,
    server: Any,
    token: str,
    *,
    open_browser: bool,
    idle_timeout: float,
    grace: float,
    watch_interval: float,
    ready: Any,
) -> int:
    ctx.on_quit = server.stop
    watchdog = IdleWatchdog(
        ctx, server.stop, idle_timeout=idle_timeout, grace=grace, interval=watch_interval
    )
    try:
        url = browser_url(server.port, token)
        # The whole address, key included: without the key the page cannot be used,
        # and this terminal is the person's own.
        print(f"etsyotomasyon is running at {url}", flush=True)
        print("Close the browser tab to stop it, or press Ctrl+C here.", flush=True)
        watchdog.start()
        ctx.updates.start()  # a look for a newer release ~10 s from now (unless one
        # answered under an hour ago), then daily
        if open_browser:
            _open_browser(url)
        if server.companion is not None:
            threading.Thread(target=server.companion.serve_forever, kwargs={"poll_interval": 0.5},
                             name="etsyotomasyon-ipv6", daemon=True).start()
        if ready is not None:
            threading.Thread(target=ready, args=(server,), daemon=True).start()
        try:
            server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            pass
    finally:
        watchdog.cancel()
        if server.companion is not None:
            server.companion.stop_serving()
        server.stopping.set()
        ctx.close()
        server.server_close()
        clear_state(token)
    return 0
