"""Tiny background job runner.

Pipelines (analyse -> merge -> configure -> deploy) take seconds to minutes, so
they run off the request thread. State lives in Postgres, so a restart loses at
most the in-flight step (the change request shows the last recorded status and
can be retried). For multi-replica deployments swap this for a real queue
(e.g. arq/Celery/RQ) — the job functions only need a change request id.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from ..settings import get_settings

log = logging.getLogger(__name__)


class JobRunner:
    def __init__(self, workers: int = 2):
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="devops-job")

    def submit(self, fn: Callable[..., Any], *args: Any) -> Future[Any] | None:
        if get_settings().sync_jobs:
            fn(*args)
            return None

        def wrapped() -> Any:
            try:
                return fn(*args)
            except Exception:  # noqa: BLE001
                log.exception("background job %s failed", getattr(fn, "__name__", fn))
                raise

        return self._pool.submit(wrapped)


jobs = JobRunner()
