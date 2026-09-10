"""Fan-out helper.

Jobs are pure functions (LLM HTTP calls, no DB). The main thread resolves cache hits
first, runs the misses in a thread pool, and calls `on_done` for each completed job in
completion order so the trace/DB can be updated as results land.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class Job:
    id: object
    fn: callable
    cache_key: str = None
    meta: dict = field(default_factory=dict)


@dataclass
class JobResult:
    job: Job
    value: object = None
    error: Exception = None
    cached: bool = False
    usage: dict = field(default_factory=dict)

    @property
    def ok(self):
        return self.error is None


def run_jobs(jobs, max_workers=6, cache=None, on_start=None, on_done=None, abort_on=None):
    """Run `jobs` concurrently. Returns JobResults keyed by job.id.

    `cache` (optional) is consulted with job.cache_key before dispatch; a job whose fn
    returns a dict with a `usage` key has that recorded. `abort_on(exc)` may return True
    to stop scheduling further jobs (terminal errors such as auth failures).
    """
    results = {}
    pending = []
    for job in jobs:
        hit = cache.get(job.cache_key) if (cache and job.cache_key) else None
        if hit is not None:
            res = JobResult(job=job, value=hit["value"], cached=True, usage=hit.get("usage") or {})
            results[job.id] = res
            if on_done:
                on_done(res)
        else:
            pending.append(job)

    if not pending:
        return results

    max_workers = max(1, min(int(max_workers or 1), len(pending)))
    aborted = False
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ankigpt-llm") as pool:
        futures = {}
        for job in pending:
            if on_start:
                on_start(job)
            futures[pool.submit(job.fn)] = job
        for future in as_completed(futures):
            job = futures[future]
            try:
                value = future.result()
                res = JobResult(job=job, value=value)
                if isinstance(value, dict) and isinstance(value.get("usage"), dict):
                    res.usage = value["usage"]
                if cache and job.cache_key and value is not None and not aborted:
                    cache.put(job.cache_key, job.meta.get("role"), job.meta.get("model"), value, res.usage)
            except Exception as exc:  # per-job failure is data, not a crash
                res = JobResult(job=job, error=exc)
                if abort_on and abort_on(exc) and not aborted:
                    aborted = True
                    for other in futures:
                        other.cancel()
            results[job.id] = res
            if on_done:
                on_done(res)
    return results
