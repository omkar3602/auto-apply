"""Background-thread runners for sync and apply, so requests return
immediately and the UI polls for status.

Applies go through a queue: marking a job "queued" (from the swipe tab or
the per-source Apply button) is just a DB write. A single dedicated worker
thread drains queued jobs oldest-first and feeds them into tsenta one at a
time - that serialization is required anyway, since tsenta automation drives
one persistent browser profile.
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
_apply_worker_started = False


def get_sync_status(source):
    return _sync_status.get(source, {"state": "unknown", "last_synced_at": None, "last_result": None})


def run_sync_async(app, source):
    if source not in SOURCES:
        raise ValueError(f"Unknown source: {source}")
    if _sync_status[source]["state"] == "running":
        return

    def worker():
        lock = _LOCKS.get(source, threading.Lock())
        with lock:
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
            # Jobs left "applying" belong to a previous process that died
            # mid-submission (a restart, a crash) - we can't tell whether
            # tsenta actually got them, so don't silently retry and risk a
            # double submission. Flag them for a manual look instead.
            orphaned = Job.query.filter_by(status="applying").all()
            for job in orphaned:
                job.status = "apply_failed"
                job.status_message = "Interrupted by a service restart - check tsenta and retry if needed."
                job.status_changed_at = datetime.utcnow()
            if orphaned:
                db.session.commit()

            while True:
                job = (
                    Job.query.filter_by(status="queued")
                    .order_by(Job.status_changed_at.asc())
                    .first()
                )
                if job is None:
                    time.sleep(APPLY_WORKER_POLL_INTERVAL_S)
                    continue

                job_id, job_url = job.id, job.job_url
                job.status = "applying"
                job.status_changed_at = datetime.utcnow()
                db.session.commit()

                try:
                    ok, message = apply_via_tsenta(job_url)
                except Exception as e:
                    app.logger.exception("[apply job=%s] failed", job_id)
                    ok, message = False, str(e)

                app.logger.info("[apply job=%s] %s: %s", job_id, "ok" if ok else "failed", message)

                job = db.session.get(Job, job_id)
                if job is not None:
                    job.status = "applied" if ok else "apply_failed"
                    job.status_message = message
                    job.status_changed_at = datetime.utcnow()
                    db.session.commit()

    threading.Thread(target=worker, daemon=True).start()
