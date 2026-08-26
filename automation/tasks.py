"""Background-thread runners for sync and apply, so requests return
immediately and the UI polls for status. One lock per external resource
(jobright profile, tsenta profile) so we never run two browser sessions
against the same persistent profile dir at once.
"""

import threading
from datetime import datetime

from db import db
from models import Job
from scrapers import SOURCES
from automation.tsenta import apply_via_tsenta

JOBRIGHT_LOCK = threading.Lock()
TSENTA_LOCK = threading.Lock()

_LOCKS = {"jobright": JOBRIGHT_LOCK}

_sync_status = {
    source: {"state": "idle", "last_synced_at": None, "last_result": None}
    for source in SOURCES
}


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


def run_apply_async(app, job_id):
    def worker():
        with app.app_context():
            job = db.session.get(Job, job_id)
            if job is None:
                return
            job.status = "applying"
            job.status_changed_at = datetime.utcnow()
            db.session.commit()

            with TSENTA_LOCK:
                try:
                    ok, message = apply_via_tsenta(job.job_url)
                except Exception as e:
                    app.logger.exception("[apply job=%s] failed", job_id)
                    ok, message = False, str(e)

            app.logger.info("[apply job=%s] %s: %s", job_id, "ok" if ok else "failed", message)

            job = db.session.get(Job, job_id)
            if job is None:
                return
            job.status = "applied" if ok else "apply_failed"
            job.status_message = message
            job.status_changed_at = datetime.utcnow()
            db.session.commit()

    threading.Thread(target=worker, daemon=True).start()
