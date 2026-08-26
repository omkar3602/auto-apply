from flask import Blueprint, render_template, request

from models import DECIDED_STATUSES, Job
from scrapers import SOURCES

history_bp = Blueprint("history", __name__, url_prefix="/history")


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
