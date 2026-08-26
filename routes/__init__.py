from routes.jobs import jobs_bp
from routes.history import history_bp


def register_blueprints(app):
    app.register_blueprint(jobs_bp)
    app.register_blueprint(history_bp)
