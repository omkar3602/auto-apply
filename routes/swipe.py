from flask import Blueprint, render_template

from automation import user_settings
from models import Job

swipe_bp = Blueprint("swipe", __name__, url_prefix="/swipe")

BATCH_SIZE = 50


@swipe_bp.get("")
def swipe():
    settings = user_settings.load()

    query = Job.query.filter(Job.status == "new")
    selected_sources = settings["swipe_sources"]
    if selected_sources is not None:
        query = query.filter(Job.source.in_(selected_sources))

    jobs = query.order_by(Job.created_at.asc()).limit(BATCH_SIZE).all()
    return render_template(
        "swipe.html",
        jobs=jobs,
        keyboard_mode=settings["keyboard_mode"],
    )
