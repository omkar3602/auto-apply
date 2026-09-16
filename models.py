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

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    status_changed_at = db.Column(db.DateTime)

    raw_json = db.Column(db.Text)

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
