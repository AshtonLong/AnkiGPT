import logging
import os
import sqlite3

from flask import Flask, request
from sqlalchemy import event, inspect, text
from sqlalchemy.engine import Engine

from .config import Config, DEV_SECRET_KEY
from .database import database_url, engine_options
from .extensions import csrf, db, login_manager, migrate
from .models import User


@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    """SQLite tuning: enforce ON DELETE CASCADE (off by default), and use WAL with a busy
    timeout so the generation thread and web requests can share the file."""
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=8000")
            cursor.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.DatabaseError:
            pass
        cursor.close()


def _under_instance(instance_path, path):
    """Resolve a config path relative to the instance dir, tolerating an 'instance/' prefix."""
    if os.path.isabs(path):
        return path
    if path.startswith("instance/"):
        path = path.replace("instance/", "", 1)
    return os.path.join(instance_path, path)


def _ensure_columns(app):
    """Add columns that exist on the models but not in an older database.

    `create_all()` only creates missing *tables*. Rather than force a migration step
    on every schema change in a single-user app, add missing columns in place
    (nullable, so the statement is valid on SQLite and Postgres alike).
    """
    engine = db.engine
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    added = []
    with engine.begin() as conn:
        for table in db.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            present = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                col_type = column.type.compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'))
                added.append(f"{table.name}.{column.name}")
    if added:
        app.logger.info("Added missing columns: %s", ", ".join(added))


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

    app.config["SQLALCHEMY_DATABASE_URI"] = database_url(app.config["SQLALCHEMY_DATABASE_URI"])
    options = engine_options(app.config["SQLALCHEMY_DATABASE_URI"])
    options.update(app.config.get("SQLALCHEMY_ENGINE_OPTIONS", {}))
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = options
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    csrf.init_app(app)

    from .routes.main import bp as main_bp
    from .routes.auth import bp as auth_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)

    @app.after_request
    def private_responses(response):
        # Account-specific HTML, images, downloads, and errors must be reauthorized
        # after logout or an account switch, never reused from an HTTP cache.
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "private, no-store"
            response.vary.add("Cookie")
        return response

    with app.app_context():
        db.create_all()
        _ensure_columns(app)

    return app


@login_manager.user_loader
def load_user(user_id):
    try:
        return db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None
