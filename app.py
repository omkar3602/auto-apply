import os
from datetime import timezone
from zoneinfo import ZoneInfo

from flask import Flask, redirect, url_for

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

    db.init_app(app)
    with app.app_context():
        db.create_all()

    register_blueprints(app)
    start_apply_worker(app)

    @app.route("/")
    def index():
        return redirect(url_for("swipe.swipe"))

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
