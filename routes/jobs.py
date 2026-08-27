from datetime import datetime

from flask import Blueprint, current_app, jsonify, redirect, render_template, url_for

from db import db
from models import ACTIVE_STATUSES, Job
from scrapers import SOURCES
from automation.tasks import get_sync_status, run_sync_async

jobs_bp = Blueprint("jobs", __name__, url_prefix="/jobs")


@jobs_bp.get("/<source>")
def tab_view(source):
    if source not in SOURCES:
        return f"Unknown source: {source}", 404
    jobs = (
        Job.query.filter(Job.source == source, Job.status.in_(ACTIVE_STATUSES))
        .order_by(Job.created_at.desc())
        .all()
    )
    return render_template(
        "tab.html",
        source=source,
        jobs=jobs,
        sync_status=get_sync_status(source),
    )


@jobs_bp.post("/<source>/sync")
def sync(source):
    if source not in SOURCES:
        return jsonify({"error": "unknown source"}), 404
    run_sync_async(current_app._get_current_object(), source)
    return jsonify({"ok": True})


@jobs_bp.get("/<source>/sync-status")
def sync_status(source):
    return jsonify(get_sync_status(source))


@jobs_bp.get("/<source>/<int:job_id>")
def detail(source, job_id):
    job = Job.query.filter_by(source=source, id=job_id).first_or_404()
    return render_template("job_detail.html", job=job)


@jobs_bp.post("/<int:job_id>/pass")
def pass_job(job_id):
    job = db.session.get(Job, job_id)
    if job is None:
        return jsonify({"error": "not found"}), 404
    job.status = "passed"
    job.status_changed_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"ok": True})


@jobs_bp.post("/<int:job_id>/undo-pass")
def undo_pass(job_id):
    # Only reverses a pass. Apply has a real external side effect (queued or
    # already submitted to tsenta) that can't be safely un-submitted, so
    # there's no undo path for it.
    job = db.session.get(Job, job_id)
    if job is None:
        return jsonify({"error": "not found"}), 404
    if job.status != "passed":
        return jsonify({"error": "not passed"}), 409
    job.status = "new"
    job.status_changed_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"ok": True})


@jobs_bp.post("/<int:job_id>/apply")
def apply_job(job_id):
    job = db.session.get(Job, job_id)
    if job is None:
        return jsonify({"error": "not found"}), 404
    # Queues it for the background apply worker rather than driving tsenta
    # inline - see automation/tasks.start_apply_worker.
    job.status = "queued"
    job.status_changed_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"ok": True, "status": "queued"})


@jobs_bp.get("/<int:job_id>/apply-status")
def apply_status(job_id):
    job = db.session.get(Job, job_id)
    if job is None:
        return jsonify({"error": "not found"}), 404
    return jsonify({"status": job.status, "status_message": job.status_message})
