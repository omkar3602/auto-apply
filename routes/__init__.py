from routes.jobs import jobs_bp
from routes.history import history_bp
from routes.swipe import swipe_bp
from routes.push import push_bp
from routes.settings import settings_bp


def register_blueprints(app):
    app.register_blueprint(jobs_bp)
    app.register_blueprint(history_bp)
    app.register_blueprint(swipe_bp)
    app.register_blueprint(push_bp)
    app.register_blueprint(settings_bp)
