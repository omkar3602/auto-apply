import os

from flask import Flask, redirect, url_for

from config import Config
from db import db
from routes import register_blueprints
from scrapers import SOURCES

DEFAULT_SOURCE = next(iter(SOURCES))


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)
    with app.app_context():
        db.create_all()

    register_blueprints(app)

    @app.route("/")
    def index():
        return redirect(url_for("jobs.tab_view", source=DEFAULT_SOURCE))

    @app.context_processor
    def inject_sources():
        return {"nav_sources": SOURCES, "default_source": DEFAULT_SOURCE}

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
