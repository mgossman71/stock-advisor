"""One background job at a time, with progress the UI can poll.

The first-run setup, the nightly pipeline, the "Run refresh job" button and
"Screen now" all go through ``runner``.  Single-flight on purpose: two
pipelines writing the same SQLite file would only contend for the lock, and
a second click while one is running should report progress, not start a
duplicate.  Jobs run in a daemon thread so a long fetch (or a slow local LLM)
never holds an HTTP request open - a reverse proxy would time it out.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import threading
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("stock-advisor")

#: ``progress(phase, done, total, detail)`` - what jobs call to report.
Progress = Callable[[str, "int | None", "int | None", "str | None"], None]


@dataclasses.dataclass
class JobStatus:
    name: str | None = None
    running: bool = False
    phase: str = ""
    done: int | None = None
    total: int | None = None
    detail: str | None = None
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    error: str | None = None
    result: Any = None


class JobRunner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._status = JobStatus()

    def status(self) -> JobStatus:
        """A consistent copy (the job thread keeps mutating the live one)."""
        with self._lock:
            return dataclasses.replace(self._status)

    def start(self, name: str, job: Callable[[Progress], Any]) -> bool:
        """Run ``job`` in a background thread; ``False`` if one is already running."""
        if not self._claim(name):
            return False
        threading.Thread(target=self._run, args=(job,), daemon=True,
                         name=f"job-{name}").start()
        return True

    def run_blocking(self, name: str, job: Callable[[Progress], Any]) -> bool:
        """Run ``job`` on the calling thread (the scheduler's); ``False`` if busy."""
        if not self._claim(name):
            return False
        self._run(job)
        return True

    def progress(self, phase: str, done: int | None = None, total: int | None = None,
                 detail: str | None = None) -> None:
        with self._lock:
            self._status.phase, self._status.done = phase, done
            self._status.total, self._status.detail = total, detail

    def _claim(self, name: str) -> bool:
        with self._lock:
            if self._status.running:
                return False
            self._status = JobStatus(name=name, running=True, phase="starting",
                                     started_at=dt.datetime.now(dt.timezone.utc))
            return True

    def _run(self, job: Callable[[Progress], Any]) -> None:
        result, error = None, None
        try:
            result = job(self.progress)
        except Exception as exc:  # a job must never take the process down
            logger.exception("background job %s failed", self._status.name)
            error = str(exc) or exc.__class__.__name__
        with self._lock:
            self._status.running = False
            self._status.finished_at = dt.datetime.now(dt.timezone.utc)
            self._status.result, self._status.error = result, error
            self._status.phase = "failed" if error else "done"


runner = JobRunner()
