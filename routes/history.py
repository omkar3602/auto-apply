from flask import Blueprint, render_template, request

from models import DECIDED_STATUSES, Job
from scrapers import SOURCES

history_bp = Blueprint("history", __name__, url_prefix="/history")

# applied/apply_failed, not models.DECIDED_STATUSES (passed/applied) - a
# failed auto-apply attempt is exactly the outcome this page exists to
# surface, not something to hide the way the regular history page hides
# "queued"/"applying" in-flight jobs.
AUTO_APPLY_HISTORY_STATUSES = ("applied", "apply_failed")


@history_bp.get("")
def history():
    source = request.args.get("source") or None
    status = request.args.get("status") or None

    query = Job.query.filter(Job.status.in_(DECIDED_STATUSES))
    if source:
        query = query.filter(Job.source == source)
    if status:
        query = query.filter(Job.status == status)

    jobs = query.order_by(Job.status_changed_at.desc()).all()
    return render_template(
        "history.html",
        jobs=jobs,
        sources=SOURCES,
        selected_source=source,
        selected_status=status,
    )


@history_bp.get("/auto")
def auto_history():
    """Jobs automation/auto_apply_schedule.py queued, as opposed to a
    manual swipe-right/Apply click (Job.applied_via - only set going
    forward from when that column was added, so applies from before then
    won't show up here even if they actually came from the scheduler).
    """
    status = request.args.get("status") or None

    query = Job.query.filter(
        Job.applied_via == "auto", Job.status.in_(AUTO_APPLY_HISTORY_STATUSES)
    )
    if status:
        query = query.filter(Job.status == status)

    jobs = query.order_by(Job.status_changed_at.desc()).all()
    return render_template(
        "auto_history.html",
        jobs=jobs,
        selected_status=status,
    )
