"""Runs translation jobs in the background, at most `max_concurrent` at a time, and keeps their state on disk.

The manager knows nothing about translation: it is given a `runner(job, report, cache_dir) -> result` and records
what happens. A job moves Queued -> Running -> Done / Failed / Cancelled. State is written to
<state_dir>/jobs/<job id>.json after every change, so a restart does not lose finished jobs; jobs that were
Queued or Running when the service stopped are marked Failed (the backend submits them again)."""
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("extraction.jobs")

QUEUED, RUNNING, DONE, FAILED, CANCELLED = "Queued", "Running", "Done", "Failed", "Cancelled"
FINISHED = {DONE, FAILED, CANCELLED}
RESTARTED = "The extraction service restarted while this job was waiting or running; submit it again."


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Job:
    job_id: str
    input_path: str
    output_dir: str
    options: dict
    status: str = QUEUED
    progress: int = 0
    stage: Optional[str] = None
    critical: int = 0
    moderate: int = 0
    document_score: Optional[float] = None
    review_required: bool = False
    error: Optional[str] = None
    files: dict = field(default_factory=dict)
    created_at: str = field(default_factory=_now)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    cancel_requested: bool = False


Runner = Callable[[Job, Callable[[str, int], None], str], dict]


class JobManager:
    def __init__(self, state_dir, cache_dir, max_concurrent: int, runner: Runner):
        self._dir = Path(state_dir) / "jobs"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._cache_dir = str(cache_dir)
        self._runner = runner
        self._max = max_concurrent
        self._jobs: dict = {}
        self._futures: dict = {}
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(max_workers=max_concurrent, thread_name_prefix="job")

    @property
    def max_concurrent(self) -> int:
        return self._max

    # ------------------------------------------------------------------ state on disk
    def _save(self, job: Job) -> None:
        """Best effort: on Windows replacing a file can fail for a moment (antivirus, indexer), and a failed save must
        never stop a job, so it is retried and then only logged."""
        path = self._dir / f"{job.job_id}.json"
        tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
        text = json.dumps(asdict(job), ensure_ascii=False)
        for attempt in range(6):
            try:
                tmp.write_text(text, encoding="utf-8")
                os.replace(tmp, path)
                return
            except OSError:
                time.sleep(0.02 * (attempt + 1))
        log.warning("Could not save the state of job %s", job.job_id)

    def recover(self) -> int:
        """Load the jobs of earlier runs; those that were unfinished become Failed. -> number of jobs loaded."""
        known = {f.name for f in fields(Job)}
        loaded = 0
        for path in sorted(self._dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                job = Job(**{k: v for k, v in data.items() if k in known})
            except (OSError, ValueError, TypeError):
                log.warning("Ignoring unreadable job state %s", path)
                continue
            if job.status not in FINISHED:
                job.status, job.error, job.finished_at = FAILED, RESTARTED, _now()
                self._save(job)
            with self._lock:
                self._jobs[job.job_id] = job
            loaded += 1
        return loaded

    # ------------------------------------------------------------------ commands
    def submit(self, job_id: str, input_path: str, output_dir: str, options: dict):
        """-> (job, created). Submitting an id that exists returns the existing job, so a retry never starts it twice."""
        with self._lock:
            existing = self._jobs.get(job_id)
            if existing:
                return existing, False
            job = Job(job_id=job_id, input_path=input_path, output_dir=output_dir, options=options)
            self._jobs[job_id] = job
            self._save(job)
            self._futures[job_id] = self._pool.submit(self._run, job)
            return job, True

    def cancel(self, job_id: str):
        """-> (job, accepted). A queued job is cancelled at once; a running one stops at its next progress report."""
        with self._lock:
            job = self._jobs[job_id]
            if job.status in FINISHED:
                return job, False
            job.cancel_requested = True
            future = self._futures.get(job_id)
            if job.status == QUEUED and future is not None and future.cancel():
                self._finish(job, CANCELLED)
            else:
                job.stage = "Cancelling"
                self._save(job)
            return job, True

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------ queries
    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self, status: Optional[str] = None) -> list:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
            return [j for j in jobs if status is None or j.status == status]

    def counts(self) -> dict:
        with self._lock:
            return {"running": sum(j.status == RUNNING for j in self._jobs.values()),
                    "queued": sum(j.status == QUEUED for j in self._jobs.values())}

    # ------------------------------------------------------------------ the worker
    def _finish(self, job: Job, status: str, error: Optional[str] = None) -> None:
        job.status, job.error, job.finished_at = status, error, _now()
        self._save(job)

    def _report(self, job: Job, stage: str, percent: int) -> None:
        with self._lock:
            if job.status != RUNNING or job.cancel_requested:
                return
            changed = stage != job.stage or percent > job.progress
            job.stage, job.progress = stage, max(job.progress, min(int(percent), 99))
            if changed:
                self._save(job)

    def _run(self, job: Job) -> None:
        try:
            self._execute(job)
        except Exception as error:      # the manager's own bookkeeping failed: the job must still end
            log.exception("Job %s could not be run", job.job_id)
            with self._lock:
                if job.status not in FINISHED:
                    self._finish(job, FAILED, f"Internal error: {error}"[:500])

    def _execute(self, job: Job) -> None:
        with self._lock:
            if job.cancel_requested:
                self._finish(job, CANCELLED)
                return
            job.status, job.started_at, job.stage = RUNNING, _now(), "Starting"
            self._save(job)
        try:
            result = self._runner(job, lambda stage, percent: self._report(job, stage, percent), self._cache_dir)
        except Exception as error:      # a failed job must never take the worker down
            with self._lock:
                if job.cancel_requested:
                    self._finish(job, CANCELLED)
                else:
                    log.exception("Job %s failed", job.job_id)
                    self._finish(job, FAILED, (str(error) or type(error).__name__)[:500])
            return
        with self._lock:
            job.critical, job.moderate = result.get("critical", 0), result.get("moderate", 0)
            job.document_score = result.get("documentScore")
            job.review_required = bool(result.get("reviewRequired"))
            job.files = {k: v for k, v in (result.get("files") or {}).items() if v}
            job.progress, job.stage = 100, "Done"
            self._finish(job, DONE)
