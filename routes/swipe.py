from flask import Blueprint, render_template

from models import Job

swipe_bp = Blueprint("swipe", __name__, url_prefix="/swipe")

BATCH_SIZE = 50


@swipe_bp.get("")
def swipe():
    jobs = (
        Job.query.filter(Job.status == "new")
        .order_by(Job.created_at.asc())
        .limit(BATCH_SIZE)
        .all()
    )
    return render_template("swipe.html", jobs=jobs)
