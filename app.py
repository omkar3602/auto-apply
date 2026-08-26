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
    app.run(debug=True, use_reloader=False, port=8000)
