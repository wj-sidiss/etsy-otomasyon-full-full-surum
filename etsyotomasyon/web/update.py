"""Is there a newer etsyotomasyon? A quiet look at GitHub's latest release, at every start
and once a day while the app runs.

    ctx.updates.start()            # the launcher: a look ~10 s after start, then daily
    ctx.updates.state()            # what the page shows (GET /api/update)
    ctx.updates.check(force=True)  # "Şimdi kontrol et" (POST /api/update/check)
    ctx.updates.dismiss("0.3.1")   # the pill's ×, for that version only
    ctx.updates.set_auto(False)    # the Ayarlar toggle

One GET to api.github.com/repos/<repo>/releases/latest, which already skips drafts and
pre-releases, with nothing but a User-Agent. The answer is kept in
`~/.etsyotomasyon/update.json` with the time it came. Every start looks again, unless that
answer is under an hour old (START_FRESH): a quick restart reuses it. While the app
stays open, the next look comes a day after the last answer. Any failure (offline, rate
limit, a strange answer) is a log line and nothing else; the next look comes an hour
later, a restart included, so there is never more than one failed look an hour. A newer
version is announced once through the bell (the version is remembered), and the pill in
the top bar shows it until it is dismissed for that version.

Off switches: the Ayarlar toggle (app pref `update_check`, on by default) stops the
automatic looks; `ETSYOTOMASYON_NO_UPDATE_CHECK=1` stops every look, the manual one too.
"""

from __future__ import annotations

import copy
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .. import __version__
from ..config import base_home, read_json, write_json_private
from ..errors import ConfigError

if TYPE_CHECKING:  # pragma: no cover
    from .context import AppContext

log = logging.getLogger("etsyotomasyon.web")

REPO = "wj-sidiss/etsy-otomasyon-full-full-surum"
# GitHub answers a renamed repository with a redirect, which is followed.
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
CACHE_FILE = "update.json"
ENV_OFF = "ETSYOTOMASYON_NO_UPDATE_CHECK"
PREF = "update_check"
FIRST_DELAY = 10.0
INTERVAL = 24 * 60 * 60.0  # while the app runs: a day after the last answer
START_FRESH = 60 * 60.0  # at a start: an answer younger than this is reused
RETRY_AFTER = 60 * 60.0  # after a failed look
MIN_WAIT = 60.0
TIMEOUT = 5.0
MAX_NOTES = 20000
MAX_NAME = 200
ERRORS = ("offline", "rate_limited", "bad_response")

_TAG = re.compile(r"[vV]?(\d{1,6})\.(\d{1,6})\.(\d{1,6})")
_WHEN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})")
_SAFE_URL = re.compile(r"https://github\.com/[A-Za-z0-9._~/%+-]+")

Version = tuple[int, int, int]


def parse_version(text: Any) -> Version | None:
    """(0, 3, 10) for "v0.3.10" or "0.3.10"; None for anything else ("nightly", "v1.2")."""
    if not isinstance(text, str):
        return None
    match = _TAG.fullmatch(text.strip())
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def current_version(text: str = __version__) -> Version | None:
    """The running version: its leading X.Y.Z, so "0.3.0.dev1" counts as 0.3.0."""
    match = _TAG.match(text.strip()) if isinstance(text, str) else None
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def is_newer(candidate: Any, than: Any) -> bool:
    """True when version text `candidate` is above `than` (numbers, not text: 0.3.10 > 0.3.9)."""
    new = parse_version(candidate)
    old = current_version(than) if isinstance(than, str) else None
    return new is not None and old is not None and new > old


def _clean_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = value.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    if len(text) > limit:
        text = text[:limit].rstrip() + "\n…"
    return text.strip()


def _clean_url(value: Any) -> str:
    """The release page on github.com; anything else falls back to the releases page."""
    if isinstance(value, str) and len(value) <= 500 and _SAFE_URL.fullmatch(value):
        return value
    return RELEASES_PAGE


def parse_release(data: Any) -> dict[str, Any] | None:
    """The fields the app shows, from GitHub's release JSON (or from our own cache).

    None when there is no usable release in it: not an object, a draft or pre-release,
    or a tag that is not vX.Y.Z. Every field is checked, since the answer is untrusted:
    the notes are plain text for textContent, the link is always on github.com.
    """
    if not isinstance(data, dict) or data.get("draft") is True or data.get("prerelease") is True:
        return None
    tag = data.get("tag_name", data.get("tag"))
    version = parse_version(tag)
    if version is None:
        return None
    published = data.get("published_at")
    return {
        "version": "{}.{}.{}".format(*version),
        "tag": str(tag).strip(),
        "url": _clean_url(data.get("html_url", data.get("url"))),
        "name": _clean_text(data.get("name"), MAX_NAME),
        "notes": _clean_text(data.get("body", data.get("notes")), MAX_NOTES),
        "published_at": published if isinstance(published, str) and _WHEN.fullmatch(published)
        else None,
    }


class CheckFailed(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


class UpdateChecker:
    """Looks for a newer release in the background; see the module doc.

    Nothing runs until start(). `transport` (an httpx transport) and `clock` are for
    tests; a test never reaches the network (conftest sets ETSYOTOMASYON_NO_UPDATE_CHECK).
    """

    def __init__(
        self,
        ctx: AppContext,
        *,
        url: str = LATEST_URL,
        transport: Any = None,
        first_delay: float = FIRST_DELAY,
        interval: float = INTERVAL,
        start_fresh: float = START_FRESH,
        retry_after: float = RETRY_AFTER,
        min_wait: float = MIN_WAIT,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.ctx = ctx
        self.url = url
        self.transport = transport
        self.first_delay = first_delay
        self.interval = interval
        self.start_fresh = start_fresh
        self.retry_after = retry_after
        self.min_wait = min_wait
        self.clock = clock
        self.current = __version__
        self._lock = threading.Lock()
        self._check_lock = threading.Lock()
        self._wake = threading.Event()
        self._closed = False
        self._thread: threading.Thread | None = None
        self._data = self._load()

    # --- switches ---------------------------------------------------------------------

    @property
    def blocked(self) -> bool:
        """ETSYOTOMASYON_NO_UPDATE_CHECK is set (1, true, yes, on): no look at all."""
        return os.environ.get(ENV_OFF, "").strip().lower() not in ("", "0", "false", "no", "off")

    @property
    def auto(self) -> bool:
        """The Ayarlar toggle: look by itself, at a start and daily (default on)."""
        value = self.ctx.app_prefs().get(PREF, True)
        return value if isinstance(value, bool) else True

    @property
    def enabled(self) -> bool:
        return self.auto and not self.blocked

    def set_auto(self, on: bool) -> dict[str, Any]:
        prefs = self.ctx.app_prefs()
        prefs[PREF] = bool(on)
        self.ctx.save_app_prefs(prefs)
        self._wake.set()  # the loop looks again at once if a look is due
        return self._publish()

    # --- the cache (base home: shared by every shop) ------------------------------------

    @staticmethod
    def path() -> Path:
        return base_home() / CACHE_FILE

    def _load(self) -> dict[str, Any]:
        try:
            raw = read_json(self.path())
        except ConfigError:
            log.warning("could not read %s; starting afresh", CACHE_FILE)
            raw = None
        raw = raw if isinstance(raw, dict) else {}
        data: dict[str, Any] = {
            "checked_at": _number(raw.get("checked_at")),
            "attempted_at": _number(raw.get("attempted_at")),
            "error": raw.get("error") if raw.get("error") in ERRORS else None,
            "latest": parse_release(raw.get("latest")),
        }
        for key in ("notified", "dismissed"):
            version = parse_version(raw.get(key))
            data[key] = "{}.{}.{}".format(*version) if version else None
        return data

    def _save(self) -> None:
        """Caller holds self._lock."""
        try:
            write_json_private(self.path(), self._data)
        except OSError as exc:
            log.warning("could not save %s: %s", CACHE_FILE, exc)

    # --- what the page gets ---------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        """GET /api/update and the SSE `update` topic.

        `pill`: the top bar shows the new version (a newer release, not dismissed for
        that version, automatic looks on). `error` is set when the last look failed.
        """
        with self._lock:
            data = copy.deepcopy(self._data)
        latest = data["latest"]
        available = bool(latest) and is_newer(latest["version"], self.current)
        dismissed_v = parse_version(data["dismissed"])
        dismissed = available and dismissed_v is not None and (
            dismissed_v >= (parse_version(latest["version"]) or (0, 0, 0))
        )
        auto, blocked = self.auto, self.blocked
        return {
            "current": self.current,
            "auto": auto,
            "blocked": blocked,
            "enabled": auto and not blocked,
            "checked_at": data["checked_at"],
            "error": data["error"],
            "latest": latest,
            "available": available,
            "dismissed": dismissed,
            "pill": available and not dismissed and auto and not blocked,
        }

    def _publish(self) -> dict[str, Any]:
        state = self.state()
        self.ctx.events.publish("update", state)
        return state

    def dismiss(self, version: str) -> dict[str, Any]:
        """Hide the pill for `version` (and anything older); a newer release shows it again."""
        parsed = parse_version(version)
        if parsed is None:
            raise ValueError(f"not a version: {version!r}")
        with self._lock:
            self._data["dismissed"] = "{}.{}.{}".format(*parsed)
            self._save()
        return self._publish()

    # --- looking --------------------------------------------------------------------------

    def due_in(self, now: float | None = None, *, max_age: float | None = None) -> float:
        """Seconds until the next automatic look (<= 0: now).

        `max_age` is how old the last answer may grow before a look is due: a day
        (`interval`, the default) while the app runs, an hour (`start_fresh`) for the
        look at a start. A failed try is followed by the next one an hour later in both
        cases. A time in the future (a clock that was set back) counts as now.
        """
        now = self.clock() if now is None else now
        max_age = self.interval if max_age is None else max_age
        with self._lock:
            checked = self._data["checked_at"]
            attempted = self._data["attempted_at"]
        due = 0.0
        if checked is not None and checked <= now:
            due = checked + max_age
        if attempted is not None and attempted <= now:
            due = max(due, attempted + self.retry_after)
        return due - now

    def check(self, *, force: bool = False, max_age: float | None = None) -> dict[str, Any]:
        """Look now (force) or only when due (see due_in for `max_age`). Returns
        state(); never raises."""
        if self.blocked or self._closed:
            return self.state()
        with self._check_lock:
            if self._closed or (not force and self.due_in(max_age=max_age) > 0):
                return self.state()
            now = self.clock()
            try:
                latest = self._fetch()
            except CheckFailed as exc:
                log.warning("update check failed (%s): %s", exc.code, exc)
                with self._lock:
                    self._data["attempted_at"] = now
                    self._data["error"] = exc.code
                    self._save()
                return self._publish()
            except Exception:  # noqa: BLE001 — a background look must never raise
                log.exception("update check failed")
                with self._lock:
                    self._data["attempted_at"] = now
                    self._data["error"] = "bad_response"
                    self._save()
                return self._publish()
            announce = None
            with self._lock:
                self._data.update(checked_at=now, attempted_at=now, error=None, latest=latest)
                if latest and is_newer(latest["version"], self.current):
                    told = parse_version(self._data["notified"])
                    if told is None or told < (parse_version(latest["version"]) or (0, 0, 0)):
                        self._data["notified"] = announce = latest["version"]
                self._save()
            log.info("update check: latest release %s, running %s",
                     latest["version"] if latest else "none", self.current)
        if announce is not None:
            self._announce(announce)
        return self._publish()

    def _announce(self, version: str) -> None:
        """One bell notification per new version (in the open shop's list)."""
        try:
            with self.ctx.shop_lock.read():
                self.ctx.notify("common", "update.notify", {"version": version},
                                tone="info", link="/ayarlar")
        except Exception:  # noqa: BLE001
            log.exception("could not announce etsyotomasyon %s", version)

    def _fetch(self) -> dict[str, Any] | None:
        """GitHub's latest release, or None when there is none we can use."""
        import httpx

        headers = {"Accept": "application/vnd.github+json", "User-Agent": f"etsyotomasyon/{__version__}"}
        try:
            with httpx.Client(timeout=TIMEOUT, follow_redirects=True,
                              transport=self.transport) as http:
                resp = http.get(self.url, headers=headers)
        except httpx.HTTPError as exc:
            raise CheckFailed("offline", str(exc) or type(exc).__name__) from exc
        if resp.status_code == 404:
            return None  # no release published (yet)
        if resp.status_code in (403, 429):
            raise CheckFailed("rate_limited", f"HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise CheckFailed("bad_response", f"HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise CheckFailed("bad_response", "the answer is not JSON") from exc
        if not isinstance(data, dict):
            raise CheckFailed("bad_response", "the answer is not a JSON object")
        release = parse_release(data)
        if release is None:
            log.info("update check: ignoring the latest release %r (not vX.Y.Z)",
                     str(data.get("tag_name"))[:40])
        return release

    # --- the background loop ----------------------------------------------------------------

    def start(self) -> None:
        """Look ~10 s from now (unless the last answer is under an hour old), then
        whenever due, on a daemon thread. Idempotent."""
        with self._lock:
            if self._thread is not None or self._closed:
                return
            self._thread = threading.Thread(target=self._loop, name="etsyotomasyon-update",
                                            daemon=True)
        self._thread.start()

    def fresh(self, now: float | None = None) -> bool:
        """The last answer is under `start_fresh` old: a start reuses it."""
        now = self.clock() if now is None else now
        with self._lock:
            checked = self._data["checked_at"]
        return checked is not None and checked <= now and now - checked < self.start_fresh

    def step(self, starting: bool) -> tuple[bool, float]:
        """One pass of the background loop: look when due. Returns (still starting,
        seconds until the next pass).

        `starting` holds until the app has an answer under `start_fresh` old, reused
        from the cache or just asked: until then an older answer is due (the look at a
        start), so a start look put off by a failed try an hour ago, or failing itself,
        comes an hour later, not a day. Then a day after each answer. With the checks
        off nothing is looked at, and the first pass after they are switched on is
        still the start's.
        """
        if not self.enabled:
            return starting, self.interval
        if starting and self.fresh():
            starting = False
        max_age = self.start_fresh if starting else None
        if self.due_in(max_age=max_age) <= 0:
            self.check(max_age=max_age)
            if starting and self.fresh():
                starting = False
        max_age = self.start_fresh if starting else None
        return starting, max(self.min_wait, self.due_in(max_age=max_age))

    def _loop(self) -> None:
        wait = self.first_delay
        starting = True
        while True:
            self._wake.wait(max(0.0, wait))
            self._wake.clear()
            if self._closed:
                return
            starting, wait = self.step(starting)

    def close(self) -> None:
        self._closed = True
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(0.5)  # it may be inside a request (5 s at most); it is a daemon
