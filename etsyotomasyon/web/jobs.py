"""Long work runs as a Job on a worker thread, strictly one at a time per lane.

    def work(job: Job) -> dict:
        items = load_items()
        for n, item in enumerate(items, 1):
            job.check_cancel()                      # raises JobCancelled when asked to stop
            do(item)
            job.progress(n, len(items), label=item.name)
            job.emit("item-done", name=item.name)   # -> SSE `job-event`
            job.set_state(last=item.name)           # snapshot a page can re-read later
        return {"done": len(items)}                 # -> job.result

    job = ctx.jobs.start("designs", "designs:job.title", work, params={"n": 3})
    return job.summary()

Anything `work` raises becomes the job's `error` ({code, message, params}, mapped
like an API error). A job holds the shop read lock while it runs, so the open
shop cannot change underneath it, and the shop switch is refused while any job is
queued or running, in any lane.

Lanes: "write" (the default) is for anything that changes the shop or the workspace:
those jobs run strictly one after another. "read" has its own worker for work that
only reads (the Kâr-Zarar numbers), so it does not wait behind a long upload:

    ctx.jobs.start("profit", "profit:job.title", work, lane="read")
"""

from __future__ import annotations

import collections
import logging
import threading
import time
import uuid
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # pragma: no cover
    from .context import AppContext

log = logging.getLogger("etsyotomasyon.web")

QUEUED, RUNNING, DONE, ERROR, CANCELLED = "queued", "running", "done", "error", "cancelled"
ACTIVE = (QUEUED, RUNNING)
WRITE, READ = "write", "read"
LANES = (WRITE, READ)
KEEP_JOBS = 50
KEEP_LOG_LINES = 500
# At most five `job` progress events per second per job; the latest always arrives.
PROGRESS_INTERVAL = 0.2
# Errors that mean the shop's connection changed: re-check the status afterwards.
_STATUS_CODES = {"reconnect", "offline", "bad_keys", "setup_needed"}


class JobCancelled(Exception):
    """Raised inside a job by check_cancel() once someone asked it to stop."""


def _now() -> float:
    return round(time.time(), 3)


class Job:
    def __init__(
        self,
        runner: JobRunner,
        kind: str,
        title_key: str,
        fn: Callable[[Job], Any],
        *,
        params: dict[str, Any] | None = None,
        cancellable: bool = True,
        refresh_status: bool = False,
        lane: str = WRITE,
    ) -> None:
        self.runner = runner
        self.lane = lane
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.title_key = title_key
        self.fn = fn
        self.params = dict(params or {})
        self.cancellable = cancellable
        self.refresh_status = refresh_status
        self.status = QUEUED
        self.done_count = 0
        self.total: int | None = None
        self.label: str | None = None
        self.created_at = _now()
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.error: dict[str, Any] | None = None
        self.result: Any = None
        self.state: dict[str, Any] = {}
        self.logs: collections.deque[dict[str, Any]] = collections.deque(maxlen=KEEP_LOG_LINES)
        self._cancel = threading.Event()
        self._finished = threading.Event()
        self._lock = threading.Lock()
        self._last_progress = 0.0
        self._pending_timer: threading.Timer | None = None

    # --- called from inside the job ---------------------------------------------

    def progress(self, done: int, total: int | None = None, label: str | None = None) -> None:
        """Report progress: `done` of `total` (None when unknown), with an optional label."""
        with self._lock:
            self.done_count = int(done)
            self.total = None if total is None else int(total)
            if label is not None:
                self.label = str(label)
            now = time.monotonic()
            due = now - self._last_progress >= PROGRESS_INTERVAL or (
                self.total is not None and self.done_count >= self.total
            )
            if due:
                self._last_progress = now
            elif self._pending_timer is None:
                # Throttled: publish the latest numbers once the interval has passed.
                wait = PROGRESS_INTERVAL - (now - self._last_progress)
                self._pending_timer = threading.Timer(max(0.01, wait), self._flush_progress)
                self._pending_timer.daemon = True
                self._pending_timer.start()
        if due:
            self.runner.publish(self)

    def _flush_progress(self) -> None:
        with self._lock:
            self._pending_timer = None
            self._last_progress = time.monotonic()
        if self.status == RUNNING:
            self.runner.publish(self)

    def emit(self, type: str, **data: Any) -> None:  # noqa: A002 — the event's type
        """A fine-grained event for the page: SSE `job-event` {job_id, kind, type, data}."""
        self.runner.events_publish(
            "job-event", {"job_id": self.id, "kind": self.kind, "type": type, "data": data}
        )

    def set_state(self, **fields: Any) -> None:
        """Merge JSON-able fields into `job.state`, which GET /api/jobs/{id} returns.

        Not pushed by itself: it is what a page renders after navigating back. Use
        emit() for live updates.
        """
        with self._lock:
            self.state.update(fields)

    def log(self, text: str, level: str = "info") -> None:
        """A line for the job's log (kept, and sent as job-event type "log")."""
        entry = {"at": _now(), "level": level, "text": str(text)}
        with self._lock:
            self.logs.append(entry)
        self.emit("log", **entry)

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def check_cancel(self) -> None:
        if self._cancel.is_set():
            raise JobCancelled()

    # --- from outside -------------------------------------------------------------

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the job has finished; False on timeout."""
        return self._finished.wait(timeout)

    @property
    def active(self) -> bool:
        return self.status in ACTIVE

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "kind": self.kind,
                "title_key": self.title_key,
                "params": self.params,
                "status": self.status,
                "lane": self.lane,
                "cancellable": self.cancellable,
                "progress": {"done": self.done_count, "total": self.total, "label": self.label},
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "error": self.error,
                "result": self.result,
            }

    def to_dict(self) -> dict[str, Any]:
        """The full job: the summary plus `state` and the last log lines."""
        data = self.summary()
        with self._lock:
            data["state"] = dict(self.state)
            data["log"] = list(self.logs)
        return data


class JobRunner:
    """Runs jobs one at a time per lane, in the order they were started."""

    def __init__(self, ctx: AppContext | None = None, *, keep: int = KEEP_JOBS) -> None:
        self.ctx = ctx
        self.keep = keep
        self._jobs: collections.OrderedDict[str, Job] = collections.OrderedDict()
        self._queues: dict[str, collections.deque[Job]] = {
            lane: collections.deque() for lane in LANES
        }
        self._cond = threading.Condition()
        self._stopping = False
        self._threads = [
            threading.Thread(
                target=self._run, args=(lane,), name=f"etsyotomasyon-jobs-{lane}", daemon=True
            )
            for lane in LANES
        ]
        for thread in self._threads:
            thread.start()

    # --- publishing ---------------------------------------------------------------

    def events_publish(self, topic: str, data: Any) -> None:
        if self.ctx is not None:
            self.ctx.events.publish(topic, data)

    def publish(self, job: Job) -> None:
        try:
            self.events_publish("job", job.summary())
        except Exception:  # noqa: BLE001 — a bad value must not take the worker down
            log.exception("could not publish job %s", job.id)

    # --- API ------------------------------------------------------------------------

    def start(
        self,
        kind: str,
        title_key: str,
        fn: Callable[[Job], Any],
        *,
        params: dict[str, Any] | None = None,
        cancellable: bool = True,
        refresh_status: bool = False,
        lane: str = WRITE,
    ) -> Job:
        """Queue `fn(job)`; returns the Job at once. refresh_status re-checks the shop
        status when it ends (use it for work that changes the shop or the setup).

        lane="write" (default): strictly one after another with every other write job.
        lane="read": work that only reads, on its own worker, beside a write job.
        """
        if lane not in LANES:
            raise ValueError(f"unknown job lane {lane!r}")
        job = Job(
            self, kind, title_key, fn,
            params=params, cancellable=cancellable, refresh_status=refresh_status, lane=lane,
        )
        with self._cond:
            if self._stopping:
                raise RuntimeError("the job runner has stopped")
            self._jobs[job.id] = job
            self._queues[lane].append(job)
            self._trim()
            self._cond.notify_all()
        self.publish(job)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._cond:
            return self._jobs.get(job_id)

    def list(
        self, kind: str | None = None, active: bool = False, lane: str | None = None
    ) -> list[Job]:
        """Newest first."""
        with self._cond:
            jobs = list(self._jobs.values())
        jobs.reverse()
        return [
            j for j in jobs
            if (kind is None or j.kind == kind)
            and (not active or j.active)
            and (lane is None or j.lane == lane)
        ]

    def active(self, lane: str | None = None) -> list[Job]:
        return self.list(active=True, lane=lane)

    def busy(self, lane: str | None = None) -> bool:
        """True while a job is queued or running: in `lane`, or in any lane (None)."""
        return bool(self.active(lane))

    def cancel(self, job_id: str) -> Job | None:
        """Ask a job to stop. A queued job is cancelled at once; a running one when it
        next calls check_cancel(). Returns None if there is no such job; raises
        ValueError for a running job that is not cancellable."""
        with self._cond:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job.status == QUEUED:
                self._queues[job.lane].remove(job)
                job._cancel.set()
                job.status = CANCELLED
                job.finished_at = _now()
                job._finished.set()
                self._cond.notify_all()
            elif job.status == RUNNING:
                if not job.cancellable:
                    raise ValueError("not cancellable")
                job._cancel.set()
            else:
                return job
        self.publish(job)
        return job

    def stop(self, timeout: float = 5.0) -> None:
        """Cancel everything and let the workers end (server shutdown)."""
        with self._cond:
            self._stopping = True
            for queue in self._queues.values():
                queued = list(queue)
                queue.clear()
                for job in queued:
                    job._cancel.set()
                    job.status = CANCELLED
                    job.finished_at = _now()
                    job._finished.set()
            for job in self._jobs.values():
                job._cancel.set()
            self._cond.notify_all()
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))

    # --- the workers ------------------------------------------------------------------

    def _trim(self) -> None:
        """Keep the last `keep` jobs; active ones are never dropped."""
        excess = len(self._jobs) - self.keep
        if excess <= 0:
            return
        for job_id in [jid for jid, j in self._jobs.items() if not j.active][:excess]:
            del self._jobs[job_id]

    def _run(self, lane: str) -> None:
        queue = self._queues[lane]
        while True:
            with self._cond:
                while not queue and not self._stopping:
                    self._cond.wait()
                if self._stopping and not queue:
                    return
                job = queue.popleft()
                job.status = RUNNING
                job.started_at = _now()
            self.publish(job)
            try:
                self._execute(job)
            except BaseException:  # noqa: BLE001 — the worker must survive anything
                log.exception("job %s broke the runner", job.id)
                job.status = ERROR
                job.finished_at = job.finished_at or _now()
                job._finished.set()

    def _execute(self, job: Job) -> None:
        from .errors import describe
        from .router import dumps

        result: Any = None
        error: dict[str, Any] | None = None
        status = DONE
        try:
            if self.ctx is not None:
                with self.ctx.shop_lock.read():
                    result = job.fn(job)
            else:
                result = job.fn(job)
            dumps(result)  # the result is sent as JSON: find out now, not in a response
        except JobCancelled:
            status = CANCELLED
        except BaseException as exc:  # noqa: BLE001 — becomes the job's error, never escapes
            status = ERROR
            error = describe(exc)
        with job._lock:
            if job._pending_timer is not None:
                job._pending_timer.cancel()
                job._pending_timer = None
            job.result = result if status == DONE else None
            job.error = error
            job.status = status
            job.finished_at = _now()
        job._finished.set()
        with self._cond:
            self._trim()
        self.publish(job)
        if self.ctx is not None and (
            job.refresh_status or (error is not None and error["code"] in _STATUS_CODES)
        ):
            try:
                self.ctx.set_status_soon()
            except Exception:  # noqa: BLE001
                log.exception("status refresh after a job failed")
