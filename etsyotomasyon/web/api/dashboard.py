"""Panel (dashboard) endpoints.

    GET /api/dashboard        setup steps, recent activity, etsyotomasyon's own work (the four
                              KPI cards) and the last stats (no Etsy call)
    GET /api/dashboard/stats  the Etsy numbers (cached; ?refresh=1 re-reads)

The stats are read in parallel through the shared client (its rate limiter still
holds Etsy's QPS) and each one may fail on its own: a stat that cannot be read comes
back with `value: null` and an error code, the others are still shown.

The KPI cards count etsyotomasyon's own work, from a local log of finished Tasarım Yükle
runs: 3-DRAFTS/runs.json beside the designs page's last-run.json. A run is added to it
when it ends (the dashboard follows the running job) and, should that ever be missed,
from last-run.json the next time the Panel is read. Test runs (dry_run) are not work.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ..errors import describe
from ..router import ApiError, Request
from .profit import receipt_money

if TYPE_CHECKING:  # pragma: no cover
    from ..context import AppContext
    from ..router import Router

COUNT_TTL = 60.0  # listing and order counts
SAMPLE_TTL = 300.0  # the SEO sample and the month's revenue
SEO_SAMPLE = 100  # "a cheap sample": the first page of active listings
SEO_LOW = 60  # a listing under this score needs attention (the score ring turns red)
REVENUE_MAX_RECEIPTS = 1000
RECENT_LIMIT = 8
STAT_NAMES = ("active", "draft", "to_ship", "seo", "revenue", "quota")
# A dashboard read must stay quick: two tries per request, not the batch default.
ATTEMPTS = 2

log = logging.getLogger(__name__)

RUNS_FILE = "runs.json"  # in 3-DRAFTS, beside the designs page's last-run.json
LAST_RUN_FILE = "last-run.json"
DRAFTS_DIR = "3-DRAFTS"
RUNS_KEEP = 1000  # the newest runs kept in runs.json
DESIGNS_KIND = "designs"  # the Tasarım Yükle job
# What one product takes by hand: mockups, a title and 13 tags (the video's "20 dk").
MANUAL_MINUTES = 20
SPEED_RUNS = 20  # "Ortalama hazırlanma süresi": over the newest runs that made drafts
SEO_RECENT = 50  # "Son taslakların ortalaması": the newest drafts' scores
BAR_DAYS = 7  # the drafts card's small bars: one per day, today last
_runs_lock = threading.Lock()


class StatsCache:
    """Per-shop cache of computed stats: {(shop_id, name): (computed_at, value)}."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}

    def get(self, shop_id: str, name: str, ttl: float) -> dict[str, Any] | None:
        with self._lock:
            hit = self._items.get((shop_id, name))
        if hit is None or time.time() - hit[0] > ttl:
            return None
        return dict(hit[1], cached_at=round(hit[0], 3))

    def put(self, shop_id: str, name: str, value: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            self._items[(shop_id, name)] = (now, value)
        return dict(value, cached_at=round(now, 3))

    def forget(self, shop_id: str, names: tuple[str, ...]) -> None:
        """Drop some of a shop's stats (the data behind them changed)."""
        with self._lock:
            for name in names:
                self._items.pop((shop_id, name), None)

    def last(self, shop_id: str) -> dict[str, dict[str, Any]]:
        """Whatever is cached for the shop, however old (for the fast endpoint)."""
        with self._lock:
            items = {name: (at, value) for (sid, name), (at, value) in self._items.items()
                     if sid == shop_id}
        return {name: dict(value, cached_at=round(at, 3)) for name, (at, value) in items.items()}


def register(r: Router, ctx: AppContext) -> None:
    cache = StatsCache()
    runs = RunWatcher()

    def listings_changed(c: AppContext) -> None:
        cache.forget(c.shop_id, ("active", "draft", "seo"))
        runs.follow(c)  # Tasarım Yükle made drafts: log its run once it ends

    ctx.on_change("listings", listings_changed, name="dashboard")
    ctx.on_change("orders", lambda c: cache.forget(c.shop_id, ("to_ship", "revenue")),
                  name="dashboard")
    r.get("/api/dashboard", lambda req: overview(req, cache))
    r.get("/api/dashboard/stats", lambda req: stats(req, cache))


# --- the fast part ------------------------------------------------------------------


def setup_steps(status: dict[str, Any]) -> dict[str, Any]:
    """The four "Nasıl çalışıyor?" tiles, from the status object.

    Hesap: Etsy keys saved and accepted. Mağaza: signed in. Mockuplar: at least one
    mockup. Tasarım yükle: a template listing chosen, the last thing an upload needs.
    The first step not done is the current one.
    """
    state = status.get("state")
    setup = status.get("setup") or {}
    keys_ok = bool(setup.get("keys")) and state not in ("keys", "bad_keys")
    connected = bool(setup.get("connected")) and state != "reconnect"
    mockups = int(setup.get("mockups") or 0)
    template = bool(setup.get("template"))
    done = {
        "account": keys_ok,
        "shop": keys_ok and connected,
        "mockups": mockups > 0,
        "designs": template,
    }
    steps = []
    current: str | None = None
    for step_id in ("account", "shop", "mockups", "designs"):
        if done[step_id]:
            step_state = "done"
        elif current is None:
            step_state = "current"
            current = step_id
        else:
            step_state = "todo"
        steps.append({"id": step_id, "state": step_state})
    return {
        "steps": steps,
        "current": current,
        "complete": current is None,
        "checking": state == "checking",
        "problem": state if state in ("bad_keys", "reconnect") else None,
        "mockups": mockups,
        "template_title": setup.get("template_title"),
        "designs_pending": int(setup.get("designs_pending") or 0),
    }


def recent_activity(ctx: AppContext, limit: int = RECENT_LIMIT) -> list[dict[str, Any]]:
    """Running and queued jobs first, then the newest notifications."""
    items: list[dict[str, Any]] = []
    for job in ctx.jobs.list(active=True):
        summary = job.summary()
        items.append({
            "type": "job",
            "id": summary["id"],
            "kind": summary["kind"],
            "title_key": summary["title_key"],
            "params": summary["params"],
            "status": summary["status"],
            "progress": summary["progress"],
            "at": summary["started_at"] or summary["created_at"],
        })
    for note in ctx.notifications():
        if len(items) >= limit:
            break
        items.append({"type": "notification", **note})
    return items[:limit]


def _pinterest_summary() -> dict[str, Any] | None:
    """Pins waiting and due today, from the local queue file (no network)."""
    from datetime import date

    from ... import pinterest

    try:
        queue = pinterest.Queue.load()
    except Exception:  # noqa: BLE001 — a broken queue file is the Pinterest page's business
        return None
    counts = queue.counts()
    return {
        "pending": counts.get("pending", 0),
        "due": len(queue.due(date.today())),
        "attention": sum(counts.get(s, 0) for s in ("failed", "uncertain", "sending")),
    }


def overview(req: Request, cache: StatsCache) -> dict[str, Any]:
    ctx = req.ctx
    assert ctx is not None
    status = ctx.status
    return {
        "status": status,
        "setup": setup_steps(status),
        "recent": recent_activity(ctx),
        "stats": cache.last(ctx.shop_id) or None,
        "pinterest": _pinterest_summary(),
        "work": work_summary(ctx),
    }


# --- etsyotomasyon's own work: the runs log and the four KPI cards --------------------------------


def _shop_key(ctx: AppContext) -> str | None:
    """The open shop's Etsy shop id (the upload history's key), when known."""
    shop = (ctx.status.get("shop") or {}).get("etsy_shop_id")
    return str(shop) if shop else None


def load_runs(drafts: Path) -> list[dict[str, Any]]:
    """The runs log, oldest first; [] when there is none or it cannot be read."""
    from ...drop import automation

    try:
        text = automation.read_text(drafts / RUNS_FILE)
        data = json.loads(text) if text is not None else None
    except (OSError, ValueError):
        return []
    runs = data.get("runs") if isinstance(data, dict) else None
    return [run for run in runs if isinstance(run, dict)] if isinstance(runs, list) else []


def save_runs(drafts: Path, runs: list[dict[str, Any]]) -> None:
    """Write the runs log atomically (retried while a reader has it open)."""
    from ...drop import automation

    path = drafts / RUNS_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        text = json.dumps({"version": 1, "runs": runs[-RUNS_KEEP:]}, ensure_ascii=False, indent=1)
        tmp.write_text(text, encoding="utf-8")
        automation.replace_file(tmp, path)
    except OSError:
        log.warning("could not save %s", path)


def _template_facts(root: Path) -> dict[str, Any]:
    """What every draft takes from the template listing, for the SEO score."""
    from ...drop.template import Template
    from ...drop.workspace import Workspace

    try:
        template = Template.from_dict(Workspace(root).read_template())
    except Exception:  # noqa: BLE001 — no template, or one that no longer reads: title+tags only
        return {}
    return {
        "description": template.description,
        "materials": list(template.materials),
        "should_auto_renew": template.fields.get("should_auto_renew"),
    }


def _number(value: Any) -> float:
    """A non-negative finite number, else 0 (the log is a file anyone can edit)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if 0 <= number < float("inf") else 0.0


def run_entry(data: dict[str, Any], shop: str | None, root: Path) -> dict[str, Any] | None:
    """One finished run as the log keeps it, from a last-run.json record; None for a
    test run (dry_run) or a record that is not a finished run."""
    from ... import seo

    summary = data.get("summary")
    if not isinstance(summary, dict) or summary.get("dry_run") is not False:
        return None
    finished = _number(summary.get("finished_at"))
    if not finished:
        return None
    facts: dict[str, Any] | None = None
    scores: list[int] = []
    for item in data.get("items") or []:
        if not (isinstance(item, dict) and item.get("status") in ("ok", "partial")
                and item.get("listing_id")):
            continue
        if facts is None:
            facts = _template_facts(root)
        tags = [tag for tag in item.get("tags") or [] if isinstance(tag, str)]
        listing = dict(facts, title=str(item.get("title") or ""), tags=tags)
        scores.append(int(seo.audit_listing(listing).score))
    return {
        "key": f"{data.get('job_id') or ''}:{summary.get('batch') or ''}:{round(finished, 3)}",
        "shop": shop,
        "batch": summary.get("batch"),
        "started_at": _number(summary.get("started_at")) or finished,
        "finished_at": finished,
        "duration": _number(summary.get("duration")),
        "total": int(_number(summary.get("total"))),
        "created": int(_number(summary.get("created"))),
        "errors": int(_number(summary.get("errors"))),
        "seo": scores,
    }


def record_last_run(root: Path, shop: str | None) -> bool:
    """Add the designs page's last-run.json to the runs log when it is not there yet.

    True when a run was added. Never raises: the log is a nicety, the run happened.
    """
    from ...drop import automation

    drafts = root / DRAFTS_DIR
    try:
        text = automation.read_text(drafts / LAST_RUN_FILE)
        data = json.loads(text) if text is not None else None
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    try:
        entry = run_entry(data, shop, root)
    except Exception:  # noqa: BLE001 — a record this code does not understand is skipped
        log.exception("could not read the last run for the Panel")
        return False
    if entry is None:
        return False
    with _runs_lock:
        runs = load_runs(drafts)
        if any(run.get("key") == entry["key"] for run in runs):
            return False
        runs.append(entry)
        runs.sort(key=lambda run: _number(run.get("finished_at")))
        save_runs(drafts, runs)
    return True


class RunWatcher:
    """Logs a Tasarım Yükle run once its job ends.

    The designs job reports "listings changed" while it runs (drafts were made); the
    watcher then waits for that job on a thread of its own and adds its last-run.json
    to the log, for the workspace and shop it ran in (neither changes while a job runs).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._watching: set[str] = set()

    def follow(self, ctx: AppContext) -> None:
        jobs = ctx.jobs.list(kind=DESIGNS_KIND, active=True)
        if not jobs:
            return
        root = ctx.workspace_root()
        shop = _shop_key(ctx)
        for job in jobs:
            with self._lock:
                if job.id in self._watching:
                    continue
                self._watching.add(job.id)
            threading.Thread(target=self._wait, args=(job, root, shop),
                             name="etsyotomasyon-dashboard-runs", daemon=True).start()

    def _wait(self, job: Any, root: Path, shop: str | None) -> None:
        try:
            job.wait()
            record_last_run(root, shop)
        finally:
            with self._lock:
                self._watching.discard(job.id)


def _day_start(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def work_summary(ctx: AppContext, now: datetime | None = None) -> dict[str, Any]:
    """The four KPI cards: drafts made this month (today, and per day for the small
    bars), the average seconds a product took, the time saved against MANUAL_MINUTES a
    product by hand, and the newest drafts' SEO average. Local files only."""
    root = ctx.workspace_root()
    shop = _shop_key(ctx)
    drafts = root / DRAFTS_DIR
    if drafts.is_dir():
        record_last_run(root, shop)  # in case its job's end was missed (a restart)
    runs = [run for run in load_runs(drafts) if run.get("shop") in (shop, None)]
    now = now or datetime.now()
    today = _day_start(now).date()
    month = month_start(now)
    first_bar = today - timedelta(days=BAR_DAYS - 1)

    daily = [0] * BAR_DAYS
    per_day: dict[int, int] = {}
    drafts_month = drafts_today = 0
    for run in runs:
        created = int(_number(run.get("created")))
        finished = _number(run.get("finished_at"))
        if not (created and finished):
            continue
        when = datetime.fromtimestamp(finished)
        index = (when.date() - first_bar).days
        if 0 <= index < BAR_DAYS:
            daily[index] += created
        if month <= when < now + timedelta(days=1):
            drafts_month += created
            per_day[when.day] = per_day.get(when.day, 0) + created
            if when.date() == today:
                drafts_today += created
    cumulative: list[int] = []
    total = 0
    for day in range(1, now.day + 1):
        total += per_day.get(day, 0)
        cumulative.append(total)

    timed = [run for run in runs if int(_number(run.get("created"))) > 0
             and _number(run.get("duration")) > 0][-SPEED_RUNS:]
    made = sum(int(_number(run.get("created"))) for run in timed)
    seconds = round(sum(_number(run.get("duration")) for run in timed) / made, 1) if made else None

    scores: list[int] = []
    for run in reversed(runs):
        scores.extend(int(_number(score)) for score in reversed(run.get("seo") or []))
        if len(scores) >= SEO_RECENT:
            break
    scores = scores[:SEO_RECENT]
    return {
        "month": month.strftime("%Y-%m"),
        "drafts_month": drafts_month,
        "drafts_today": drafts_today,
        "daily": daily,
        "cumulative": cumulative,
        "seconds_per_item": seconds,
        "manual_minutes": MANUAL_MINUTES,
        "minutes_saved": drafts_month * MANUAL_MINUTES,
        "seo_avg": round(sum(scores) / len(scores)) if scores else None,
        "seo_sample": len(scores),
    }


# --- the numbers from Etsy --------------------------------------------------------------


def _money(value: Any) -> tuple[float, str | None]:
    """An Etsy Money object -> (amount, currency); anything else -> (0, None)."""
    if not isinstance(value, dict):
        return 0.0, None
    try:
        amount = float(value.get("amount") or 0) / float(value.get("divisor") or 1)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0, None
    return amount, value.get("currency_code") or None


def month_start(now: datetime | None = None) -> datetime:
    """Midnight on the first day of this month, local time (the seller's computer)."""
    now = now or datetime.now()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def receipt_revenue(receipt: dict[str, Any]) -> tuple[float, str | None]:
    """One receipt's revenue and currency, exactly as Kâr-Zarar counts it.

    Items after coupons + shipping + gift wrap - the revenue share of its refunds; tax
    excluded; a fully refunded or cancelled order is 0 (profit.receipt_money).
    """
    if str(receipt.get("status") or "").lower() == "canceled":
        return 0.0, None
    currency = next(
        (_money(receipt.get(name))[1] for name in ("subtotal", "total_shipping_cost", "grandtotal")
         if _money(receipt.get(name))[1]),
        None,
    )
    return round(receipt_money(receipt)["revenue"], 4), currency


def _count_active(client: Any) -> dict[str, Any]:
    return {"value": client.count_listings("active")}


def _count_draft(client: Any) -> dict[str, Any]:
    return {"value": client.count_listings("draft")}


def _count_to_ship(client: Any) -> dict[str, Any]:
    # getShopReceipts filters (OAS): paid, not shipped, not canceled.
    return {"value": client.count_receipts(was_paid=True, was_shipped=False, was_canceled=False)}


def _seo_sample(client: Any) -> dict[str, Any]:
    from ... import seo

    scores = [seo.audit_listing(listing).score
              for listing in client.listings_by_shop("active", max_items=SEO_SAMPLE)]
    if not scores:
        return {"value": None, "sample": 0, "low": 0}
    return {
        "value": round(sum(scores) / len(scores)),
        "sample": len(scores),
        "low": sum(1 for score in scores if score < SEO_LOW),
    }


def _revenue(client: Any, shop_currency: str | None) -> dict[str, Any]:
    start = month_start()
    total = 0.0
    orders = 0
    currency: str | None = None
    # The same receipts Kâr-Zarar reads for this month (paid, not cancelled, created
    # since local midnight on the 1st), one more than the cap to know it was cut.
    receipts = list(client.receipts(
        max_items=REVENUE_MAX_RECEIPTS + 1,
        min_created=int(start.timestamp()),
        was_paid=True,
        was_canceled=False,
    ))
    for receipt in receipts[:REVENUE_MAX_RECEIPTS]:
        # Every paid order that was not cancelled counts, a fully refunded one at 0,
        # as on Kâr-Zarar.
        if not isinstance(receipt, dict) or str(receipt.get("status") or "").lower() == "canceled":
            continue
        amount, cur = receipt_revenue(receipt)
        total += amount
        orders += 1
        currency = currency or cur
    return {
        "value": round(total, 2),
        "currency": currency or shop_currency or "USD",
        "orders": orders,
        "month": start.strftime("%Y-%m"),
        "partial": len(receipts) > REVENUE_MAX_RECEIPTS,
    }


def _error(exc: BaseException) -> dict[str, Any]:
    return {"value": None, "error": describe(exc)}


def stats(req: Request, cache: StatsCache) -> dict[str, Any]:
    ctx = req.ctx
    assert ctx is not None
    refresh = req.bool_query("refresh")
    shop_id = ctx.shop_id
    try:
        client = ctx.client()
    except ApiError as exc:
        error = {"code": exc.code, "message": exc.message, "params": exc.params}
        return {
            "available": False,
            "error": error,
            "stats": {name: {"value": None, "error": error} for name in STAT_NAMES},
        }

    status = ctx.status
    if status.get("state") == "offline" and not refresh:
        # The status check just found Etsy unreachable: say so now instead of letting
        # every number time out in turn. "Yenile" (refresh=1) still tries.
        error = {"code": "offline", "message": status.get("detail") or "Etsy is not reachable.",
                 "params": {}}
        out = {name: cache.get(shop_id, name, float("inf")) or {"value": None, "error": error}
               for name in STAT_NAMES if name != "quota"}
        out["quota"] = {"value": status.get("quota_remaining"), "error": None}
        return {"available": True, "error": error, "stats": {name: out[name] for name in STAT_NAMES}}
    shop_currency = (status.get("shop") or {}).get("currency")
    work: dict[str, tuple[float, Callable[[], dict[str, Any]]]] = {
        "active": (COUNT_TTL, lambda: _count_active(client)),
        "draft": (COUNT_TTL, lambda: _count_draft(client)),
        "to_ship": (COUNT_TTL, lambda: _count_to_ship(client)),
        "seo": (SAMPLE_TTL, lambda: _seo_sample(client)),
        "revenue": (SAMPLE_TTL, lambda: _revenue(client, shop_currency)),
    }
    out: dict[str, dict[str, Any]] = {}
    todo: dict[str, Callable[[], dict[str, Any]]] = {}
    for name, (ttl, fn) in work.items():
        hit = None if refresh else cache.get(shop_id, name, ttl)
        if hit is not None:
            out[name] = hit
        else:
            todo[name] = fn

    def run(fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        with client.attempts(ATTEMPTS):
            return fn()

    if todo:
        # This thread holds the shop read lock until every worker is done, so the shop
        # cannot change while they read it.
        with ThreadPoolExecutor(max_workers=min(4, len(todo)),
                                thread_name_prefix="etsyotomasyon-dashboard") as pool:
            futures = {name: pool.submit(run, fn) for name, fn in todo.items()}
            for name, future in futures.items():
                try:
                    out[name] = cache.put(shop_id, name, dict(future.result(), error=None))
                except Exception as exc:  # noqa: BLE001 — one stat failing must not sink the rest
                    out[name] = _error(exc)

    quota = client.quota_remaining
    if quota is None:
        quota = ctx.status.get("quota_remaining")
    out["quota"] = {"value": quota, "error": None}

    errors = [out[name].get("error") for name in work if out[name].get("error")]
    common = None
    if errors and len(errors) == len(work) and len({e["code"] for e in errors}) == 1:
        common = errors[0]
    return {"available": True, "error": common, "stats": {name: out[name] for name in STAT_NAMES}}
