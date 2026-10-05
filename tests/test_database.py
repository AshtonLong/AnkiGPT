import multiprocessing
import os
import sqlite3
import time

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

from app import create_app
from app.extensions import db as _db

from conftest import TestConfig


def _config(uri):
    return type("DatabaseConfig", (TestConfig,), {"SQLALCHEMY_DATABASE_URI": uri})


@pytest.mark.parametrize("uri", [
    "postgresql://user:password@db.example.com/ankigpt?sslmode=require",
    "postgres://user:password@localhost/ankigpt",
    "mysql://user:password@localhost/ankigpt",
    "not a database url",
])
def test_only_sqlite_databases_are_accepted(uri):
    with pytest.raises(RuntimeError, match="DATABASE_URL must be a SQLite URL") as error:
        create_app(_config(uri))
    # The rejected URL may carry credentials; it must not be echoed into logs.
    assert "password" not in str(error.value)


def test_relative_database_path_lives_in_the_instance_folder():
    app = create_app(_config("sqlite:///instance/_pytest_relative.db"))
    path = os.path.join(app.instance_path, "_pytest_relative.db")
    try:
        with app.app_context():
            assert os.path.normcase(_db.engine.url.database) == os.path.normcase(path)
            assert os.path.exists(path)
    finally:
        with app.app_context():
            _db.session.remove()
            _db.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)


def _boot(uri, barrier, results):
    stalled = []

    # Pause each worker between "which tables exist?" and its first CREATE TABLE. Without
    # the startup lock every worker would by then have decided to create the same tables.
    @event.listens_for(Engine, "before_cursor_execute")
    def stall(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith("CREATE TABLE") and not stalled:
            stalled.append(True)
            time.sleep(0.4)

    barrier.wait()
    try:
        create_app(_config(uri))
        results.put(None)
    except Exception as exc:  # reported to the parent, which fails the test
        results.put(repr(exc))


def test_workers_booting_together_do_not_race_to_create_the_schema(tmp_path):
    """Gunicorn starts its workers at the same moment against one empty database."""
    database = tmp_path / "boot.db"
    uri = f"sqlite:///{database.as_posix()}"
    context = multiprocessing.get_context("spawn")
    workers = 3
    barrier, results = context.Barrier(workers), context.Queue()
    processes = [context.Process(target=_boot, args=(uri, barrier, results)) for _ in range(workers)]
    for process in processes:
        process.start()
    outcomes = [results.get(timeout=60) for _ in processes]
    for process in processes:
        process.join(timeout=30)
    assert outcomes == [None] * workers
    with sqlite3.connect(database) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"user", "deck", "card", "generation_cache"} <= tables
