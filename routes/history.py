from flask import Blueprint, abort, render_template, request

from db import db
from models import DECIDED_STATUSES, AutoApplyDecision, AutoApplyRun, Job
from scrapers import SOURCES

history_bp = Blueprint("history", __name__, url_prefix="/history")

# applied/apply_failed, not models.DECIDED_STATUSES (passed/applied) - a
# failed auto-apply attempt is exactly the outcome this page exists to
# surface, not something to hide the way the regular history page hides
# "queued"/"applying" in-flight jobs.
# The main history also lists applies that did not go through, so a failure never hides.
HISTORY_STATUSES = DECIDED_STATUSES + ("apply_failed", "needs_review")
AUTO_APPLY_HISTORY_STATUSES = ("applied", "apply_failed", "needs_review", "applying")


@history_bp.get("")
def history():
    source = request.args.get("source") or None
    status = request.args.get("status") or None

    query = Job.query.filter(Job.status.in_(HISTORY_STATUSES))
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


@history_bp.get("/auto/runs")
def auto_runs():
    """Every scheduler pass, newest first (dry runs included, marked)."""
    runs = AutoApplyRun.query.order_by(AutoApplyRun.started_at.desc()).limit(200).all()
    return render_template("auto_runs.html", runs=runs)


@history_bp.get("/auto/runs/<int:run_id>")
def auto_run(run_id):
    """One pass: every candidate with Pass/Skip, the reason, and what was done."""
    run = db.session.get(AutoApplyRun, run_id)
    if run is None:
        abort(404)
    verdict = request.args.get("verdict") or None
    query = run.decisions
    if verdict in ("pass", "skip"):
        query = query.filter(AutoApplyDecision.verdict == verdict)
    decisions = query.order_by(AutoApplyDecision.verdict.desc(), AutoApplyDecision.match_score.desc(), AutoApplyDecision.id).all()
    return render_template("auto_run.html", run=run, decisions=decisions, selected_verdict=verdict)
