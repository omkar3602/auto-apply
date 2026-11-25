from datetime import datetime

from db import db
from job_boards import job_board_name

ACTIVE_STATUSES = ("new", "queued", "applying", "apply_failed")
DECIDED_STATUSES = ("passed", "applied")

STATUS_LABELS = {
    "new": "New",
    "queued": "Queued",
    "applying": "Applying…",
    "apply_failed": "Apply Failed",
    "needs_review": "Needs you",
    "passed": "Passed",
    "applied": "Applied",
}


class Job(db.Model):
    __tablename__ = "jobs"
    __table_args__ = (db.UniqueConstraint("source", "external_id", name="uq_source_external_id"),)

    id = db.Column(db.Integer, primary_key=True)

    source = db.Column(db.String(50), nullable=False, index=True)
    external_id = db.Column(db.String(255), nullable=False)

    title = db.Column(db.String(500), nullable=False)
    company = db.Column(db.String(255))
    location = db.Column(db.String(255))
    work_mode = db.Column(db.String(50))
    posted_label = db.Column(db.String(100))
    tags = db.Column(db.Text)

    job_url = db.Column(db.Text, nullable=False)
    description = db.Column(db.Text)

    status = db.Column(db.String(20), nullable=False, default="new", index=True)
    status_message = db.Column(db.Text)

    # Set when a job is queued, "auto" (automation/auto_apply_schedule.py)
    # or "manual" (routes/jobs.py's apply_job - a swipe-right/Apply click).
    # None for a job that's never been queued at all. Exists so the
    # auto-apply history page (routes/history.py) can show only what the
    # scheduler actually submitted, not every manual apply too.
    applied_via = db.Column(db.String(20), index=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    status_changed_at = db.Column(db.DateTime)

    raw_json = db.Column(db.Text)

    # Filled in by the scheduler (automation/auto_apply_run.py). Nullable and
    # added to an existing database by db_upgrade.py, so old rows stay valid.
    posted_at = db.Column(db.DateTime)               # exact post time (naive UTC) when the source gives one
    years_min = db.Column(db.Integer)
    sponsorship = db.Column(db.String(20))
    match_score = db.Column(db.Integer)              # Tsenta's matchScore, feed jobs only
    tsenta_job_id = db.Column(db.String(64), index=True)
    tsenta_application_id = db.Column(db.String(64), index=True)

    def tag_list(self):
        return [t.strip() for t in (self.tags or "").split(",") if t.strip()]

    @property
    def job_board(self):
        return job_board_name(self.job_url)

    @property
    def status_label(self):
        return STATUS_LABELS.get(self.status) or self.status.replace("_", " ").title()

    def to_dict(self):
        return {
            "id": self.id,
            "source": self.source,
            "title": self.title,
            "company": self.company,
            "location": self.location,
            "work_mode": self.work_mode,
            "posted_label": self.posted_label,
            "tags": self.tag_list(),
            "job_url": self.job_url,
            "job_board": self.job_board,
            "status": self.status,
            "status_message": self.status_message,
        }


class PushSubscription(db.Model):
    """One row per browser/device the user enabled reminders on (a PWA
    reinstall or a second device just adds another row - endpoint is unique
    per subscription, so re-subscribing the same one updates in place).
    """

    __tablename__ = "push_subscriptions"

    id = db.Column(db.Integer, primary_key=True)
    endpoint = db.Column(db.Text, unique=True, nullable=False)
    p256dh = db.Column(db.Text, nullable=False)
    auth = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AutoApplyRun(db.Model):
    """One pass of the scheduler (or a manual pass): what it found and did."""

    __tablename__ = "auto_apply_runs"

    id = db.Column(db.Integer, primary_key=True)
    started_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    finished_at = db.Column(db.DateTime)
    trigger = db.Column(db.String(20), default="schedule")   # schedule | manual
    dry_run = db.Column(db.Boolean, default=True, nullable=False)
    # running | done | needs_login | no_balance | error
    status = db.Column(db.String(20), default="running", nullable=False)
    found = db.Column(db.Integer, default=0)
    passed = db.Column(db.Integer, default=0)
    applied = db.Column(db.Integer, default=0)       # live: accepted by Tsenta; dry run: always 0
    would_apply = db.Column(db.Integer, default=0)   # dry run only
    failed = db.Column(db.Integer, default=0)
    needs_you = db.Column(db.Integer, default=0)
    skipped_json = db.Column(db.Text, default="{}")  # {category: count}
    balance_before = db.Column(db.Integer)
    balance_after = db.Column(db.Integer)
    message = db.Column(db.Text)

    decisions = db.relationship("AutoApplyDecision", backref="run", lazy="dynamic", cascade="all, delete-orphan")

    @property
    def skipped(self):
        import json
        try:
            return json.loads(self.skipped_json or "{}")
        except ValueError:
            return {}


class AutoApplyDecision(db.Model):
    """One candidate in one run: Pass or Skip, why, and what happened to it."""

    __tablename__ = "auto_apply_decisions"

    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(db.Integer, db.ForeignKey("auto_apply_runs.id"), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    source = db.Column(db.String(30))
    title = db.Column(db.String(500))
    company = db.Column(db.String(255))
    location = db.Column(db.String(255))
    url = db.Column(db.Text)
    match_score = db.Column(db.Integer)
    job_id = db.Column(db.Integer)                   # our Job row, when there is one
    tsenta_job_id = db.Column(db.String(64))
    verdict = db.Column(db.String(8), nullable=False)        # pass | skip
    category = db.Column(db.String(30))
    reason = db.Column(db.Text)
    # would_apply | capped | applied | pending | needs_you | failed | rejected | already_applied | none
    action = db.Column(db.String(20), default="none", nullable=False)
    message = db.Column(db.Text)
