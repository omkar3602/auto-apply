"""Background-thread runners for sync and apply, so requests return
immediately and the UI polls for status.

Applies go through a queue: marking a job "queued" (from the swipe tab or
the per-source Apply button) is just a DB write. A single dedicated worker
thread drains queued jobs oldest-first and feeds them into tsenta one at a
time - that serialization is required anyway, since tsenta automation drives
one persistent browser profile.

That single thread is the *only* thing draining the queue, so nothing is
allowed to end it. Every step below is wrapped and backs off rather than
letting an exception escape the thread: an uncaught "database is locked" on
the final status write killed it once, and the queue silently stopped moving
for a day while jobs piled up behind one row stuck in "applying".
"""

import threading
import time
from datetime import datetime

from db import db
from models import Job
from scrapers import SOURCES
from automation.tsenta import apply_via_tsenta

JOBRIGHT_LOCK = threading.Lock()

_LOCKS = {"jobright": JOBRIGHT_LOCK}

_sync_status = {
    source: {"state": "idle", "last_synced_at": None, "last_result": None}
    for source in SOURCES
}

APPLY_WORKER_POLL_INTERVAL_S = 3
APPLY_WORKER_ERROR_BACKOFF_S = 5

# By the time we write the outcome the job has already been handed to tsenta,
# so a lost write is worse than a slow one: it strands the row in "applying"
# forever, and retrying that row later means applying to the same job twice.
# Retry hard through transient lock contention instead.
STATUS_WRITE_ATTEMPTS = 5
STATUS_WRITE_BACKOFF_S = 2

_apply_worker_started = False


def get_sync_status(source):
    return _sync_status.get(source, {"state": "unknown", "last_synced_at": None, "last_result": None})


def _sync_lock(source):
    """The one shared lock for `source`, created on first use.

    It has to be the same lock object for the life of the process - the old
    `_LOCKS.get(source, threading.Lock())` handed every source without an
    entry a brand-new lock on each call, which serializes nothing at all,
    since a fresh lock is never held by anyone else.
    """
    return _LOCKS.setdefault(source, threading.Lock())


def run_sync_async(app, source):
    if source not in SOURCES:
        raise ValueError(f"Unknown source: {source}")
    if _sync_status[source]["state"] == "running":
        return

    def worker():
        with _sync_lock(source):
            _sync_status[source]["state"] = "running"
            with app.app_context():
                try:
                    result = SOURCES[source]["sync"](app)
                    _sync_status[source]["last_result"] = result
                    app.logger.info("[%s] sync finished: %s", source, result)
                except Exception as e:
                    db.session.rollback()
                    app.logger.exception("[%s] sync failed", source)
                    _sync_status[source]["last_result"] = {"error": str(e)}
                finally:
                    _sync_status[source]["state"] = "idle"
                    _sync_status[source]["last_synced_at"] = datetime.utcnow().isoformat()

    threading.Thread(target=worker, daemon=True).start()


def _flag_orphaned_applying(app):
    """Jobs left "applying" belong to a previous process that died
    mid-submission (a restart, a crash) - we can't tell whether tsenta
    actually got them, so don't silently retry and risk a double
    submission. Flag them for a manual look instead.
    """
    orphaned = Job.query.filter_by(status="applying").all()
    for job in orphaned:
        job.status = "apply_failed"
        job.status_message = "Interrupted by a service restart - check tsenta and retry if needed."
        job.status_changed_at = datetime.utcnow()
    if orphaned:
        db.session.commit()
        app.logger.info("[apply] flagged %s interrupted job(s) from a previous run", len(orphaned))


def _claim_next_queued():
    """Moves the oldest queued job to "applying" and returns (id, url), or
    None when the queue is empty. Claiming before the submission runs is what
    stops a restart mid-flight from quietly re-applying the same job.
    """
    job = (
        Job.query.filter_by(status="queued")
        .order_by(Job.status_changed_at.asc())
        .first()
    )
    if job is None:
        return None

    # Read these before the commit - the session expires the instance on
    # commit, and re-loading it just to read the id costs another query.
    job_id, job_url = job.id, job.job_url
    job.status = "applying"
    job.status_changed_at = datetime.utcnow()
    db.session.commit()
    return job_id, job_url


def _record_outcome(app, job_id, ok, message):
    """Writes the final status back, retrying through transient lock
    contention. Returns True if it landed.
    """
    for attempt in range(1, STATUS_WRITE_ATTEMPTS + 1):
        try:
            job = db.session.get(Job, job_id)
            if job is None:
                return True
            job.status = "applied" if ok else "apply_failed"
            job.status_message = message
            job.status_changed_at = datetime.utcnow()
            db.session.commit()
            return True
        except Exception:
            db.session.rollback()
            if attempt == STATUS_WRITE_ATTEMPTS:
                app.logger.exception(
                    "[apply job=%s] gave up recording the outcome after %s attempts; "
                    "the submission itself reported %r, so the row is left in "
                    "'applying' for a manual look rather than retried",
                    job_id, attempt, message,
                )
                return False
            time.sleep(STATUS_WRITE_BACKOFF_S * attempt)
    return False


def _drain_once(app):
    """One pass of the queue: claim a job, submit it, record what happened.
    Returns False when there was nothing to do.
    """
    claimed = _claim_next_queued()
    if claimed is None:
        return False
    job_id, job_url = claimed

    try:
        ok, message = apply_via_tsenta(job_url)
    except Exception as e:
        app.logger.exception("[apply job=%s] failed", job_id)
        ok, message = False, str(e)

    app.logger.info("[apply job=%s] %s: %s", job_id, "ok" if ok else "failed", message)
    _record_outcome(app, job_id, ok, message)
    return True


def start_apply_worker(app):
    """Starts the single background thread that drains queued applies into
    tsenta, oldest first. The queue itself is just Job.status == "queued" -
    durable in the DB, so it survives an app restart. Safe to call more than
    once; only the first call actually starts the thread.
    """
    global _apply_worker_started
    if _apply_worker_started:
        return
    _apply_worker_started = True

    def worker():
        with app.app_context():
            try:
                _flag_orphaned_applying(app)
            except Exception:
                db.session.rollback()
                app.logger.exception("[apply] could not flag interrupted jobs at startup")

            while True:
                try:
                    if not _drain_once(app):
                        time.sleep(APPLY_WORKER_POLL_INTERVAL_S)
                except Exception:
                    # Deliberately broad: this thread dying stops every future
                    # apply, so any error backs off and tries again instead.
                    try:
                        db.session.rollback()
                    except Exception:
                        pass
                    app.logger.exception("[apply] worker loop error - backing off and continuing")
                    time.sleep(APPLY_WORKER_ERROR_BACKOFF_S)

    threading.Thread(target=worker, daemon=True).start()
