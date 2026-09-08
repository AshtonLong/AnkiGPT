import logging
import os
import sqlite3

from flask import Flask
from sqlalchemy import event
from sqlalchemy.engine import Engine

from .config import Config, DEV_SECRET_KEY
from .extensions import csrf, db, login_manager, migrate
from .models import User


@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    """Enforce ON DELETE CASCADE for SQLite, which ignores foreign keys by default."""
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def _under_instance(instance_path, path):
    """Resolve a config path relative to the instance dir, tolerating an 'instance/' prefix."""
    if os.path.isabs(path):
        return path
    if path.startswith("instance/"):
        path = path.replace("instance/", "", 1)
    return os.path.join(instance_path, path)


def create_app(config_object=Config):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_object)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if app.config.get("SECRET_KEY") == DEV_SECRET_KEY and not app.debug:
        app.logger.warning(
            "SECRET_KEY is the insecure development default. Set a strong SECRET_KEY "
            "before exposing this app — sessions can otherwise be forged."
        )

    os.makedirs(app.instance_path, exist_ok=True)
    db_url = app.config["SQLALCHEMY_DATABASE_URI"]
    if db_url.startswith("sqlite:///") and not db_url.startswith("sqlite:////"):
        rel_path = db_url.replace("sqlite:///", "", 1)
        abs_path = _under_instance(app.instance_path, rel_path)
        app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{abs_path}"
    upload_folder = app.config.get("UPLOAD_FOLDER", "")
    if upload_folder:
        upload_folder = _under_instance(app.instance_path, upload_folder)
        os.makedirs(upload_folder, exist_ok=True)
    app.config["UPLOAD_FOLDER"] = upload_folder

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    csrf.init_app(app)

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
    try:
        return db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None
