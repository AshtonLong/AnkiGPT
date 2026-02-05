import os
from flask import Flask
from .config import Config
from .extensions import db, login_manager, migrate
from .models import User


def create_app():
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(Config)
    os.makedirs(app.instance_path, exist_ok=True)
    db_url = app.config["SQLALCHEMY_DATABASE_URI"]
    if db_url.startswith("sqlite:///") and not db_url.startswith("sqlite:////"):
        rel_path = db_url.replace("sqlite:///", "", 1)
        if rel_path.startswith("instance/"):
            rel_path = rel_path.replace("instance/", "", 1)
        abs_path = os.path.join(app.instance_path, rel_path)
        app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{abs_path}"
    for key in ("UPLOAD_FOLDER", "EXPORT_FOLDER"):
        path = app.config.get(key, "")
        if path and not os.path.isabs(path):
            if path.startswith("instance/"):
                path = path.replace("instance/", "", 1)
            path = os.path.join(app.instance_path, path)
        app.config[key] = path
        if path:
            os.makedirs(path, exist_ok=True)

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"

    from .routes.main import bp as main_bp
    from .routes.auth import bp as auth_bp
    from .tasks import init_celery

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)

    init_celery(app)

    with app.app_context():
        db.create_all()

    return app


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))
