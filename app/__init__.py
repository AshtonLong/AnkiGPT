import logging
import os
import sqlite3

from flask import Flask, request
from sqlalchemy import event, inspect, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import ArgumentError
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import Config, DEV_SECRET_KEY
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


def _ensure_columns(app, conn):
    """Add columns that exist on the models but not in an older database.

    `create_all()` only creates missing *tables*. Rather than force a migration step
    on every schema change in a single-user app, add missing columns in place
    (nullable, which is the only kind SQLite can add to a table that has rows).
    """
    inspector = inspect(conn)
    existing_tables = set(inspector.get_table_names())
    added = []
    for table in db.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        present = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            col_type = column.type.compile(dialect=conn.dialect)
            conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'))
            added.append(f"{table.name}.{column.name}")
    if added:
        app.logger.info("Added missing columns: %s", ", ".join(added))


def _sqlite_url(app, value):
    """The database URL as an absolute SQLite path, or a clear error for anything else."""
    try:
        url = make_url(value)
    except ArgumentError:
        url = None
    if url is None or url.get_backend_name() != "sqlite":
        raise RuntimeError(
            "DATABASE_URL must be a SQLite URL such as sqlite:///instance/ankigpt.db. "
            "AnkiGPT stores its data in one SQLite file; other databases are not supported."
        )
    if url.database and url.database != ":memory:" and not os.path.isabs(url.database):
        url = url.set(database=_under_instance(app.instance_path, url.database))
    return url


def _ensure_schema(app):
    """Create missing tables and columns in one transaction.

    Gunicorn boots its workers at the same moment. BEGIN IMMEDIATE takes SQLite's write
    lock before anything is inspected, so the workers take turns and two of them never
    race to create the same new table.
    """
    with db.engine.connect() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        db.metadata.create_all(bind=conn)
        _ensure_columns(app, conn)
        conn.commit()


def create_app(config_object=Config):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_object)
    hops = app.config.get("PROXY_FIX_HOPS") or 0
    if hops:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=hops, x_proto=hops, x_host=hops)
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
    app.config["SQLALCHEMY_DATABASE_URI"] = _sqlite_url(app, app.config["SQLALCHEMY_DATABASE_URI"])
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
    from .routes.legal import bp as legal_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(legal_bp)

    @app.after_request
    def private_responses(response):
        # Account-specific HTML, images, downloads, and errors must be reauthorized
        # after logout or an account switch, never reused from an HTTP cache.
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "private, no-store"
            response.vary.add("Cookie")
        return response

    with app.app_context():
        _ensure_schema(app)

    return app


@login_manager.user_loader
def load_user(user_id):
    try:
        return db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None
