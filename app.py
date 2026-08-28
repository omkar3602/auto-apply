import logging
import os
from datetime import timezone
from zoneinfo import ZoneInfo

from flask import Flask, redirect, send_from_directory, url_for

from automation.reminders import start_daily_reminder
from automation.tasks import start_apply_worker
from config import Config
from db import db
from routes import register_blueprints
from scrapers import SOURCES

DEFAULT_SOURCE = next(iter(SOURCES))
DISPLAY_TZ = ZoneInfo("America/New_York")


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    # Without this, app.logger.info(...) is silently dropped - Flask's
    # logger has no level of its own, so it inherits Python's root logger
    # default (WARNING), and there's no handler installed below that either.
    # Every background worker's success-path logging (sync results, apply
    # results, the daily reminder) has been invisible because of this; only
    # .exception()/.error() calls ever made it into the log.
    logging.basicConfig(level=logging.INFO)

    db.init_app(app)
    with app.app_context():
        db.create_all()

    register_blueprints(app)
    start_apply_worker(app)
    start_daily_reminder(app)

    @app.route("/")
    def index():
        return redirect(url_for("swipe.swipe"))

    @app.route("/sw.js")
    def service_worker():
        # Served from the root, not /static/sw.js, so its default scope is
        # "/" (the whole site) instead of just "/static/" - a service
        # worker's scope defaults to its own directory, and without this it
        # never controls real pages, so navigator.serviceWorker.ready would
        # never resolve on them (push subscribe/unsubscribe depend on it).
        return send_from_directory(
            os.path.join(app.root_path, "static"), "sw.js", mimetype="application/javascript"
        )

    @app.context_processor
    def inject_sources():
        return {"nav_sources": SOURCES, "default_source": DEFAULT_SOURCE}

    @app.template_filter("local_dt")
    def local_dt(value, fmt="%Y-%m-%d %H:%M"):
        # Columns are stored as naive UTC (datetime.utcnow()); attach that
        # before converting, or astimezone() would assume local time instead.
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc).astimezone(DISPLAY_TZ).strftime(fmt)

    return app


app = create_app()

if __name__ == "__main__":
    # Debug is off by default: once `tailscale serve` puts this in front of
    # your tailnet, Werkzeug's interactive debugger would be reachable from
    # other devices, and it can execute code. Tracebacks still go to the
    # terminal/log either way. Set AUTO_APPLY_DEBUG=true for the in-browser
    # debugger while working locally.
    debug = os.environ.get("AUTO_APPLY_DEBUG", "").lower() == "true"
    port = int(os.environ.get("PORT", "8000"))
    app.run(debug=debug, use_reloader=False, port=port)
