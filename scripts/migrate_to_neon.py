"""Copy a SQLite snapshot into an empty PostgreSQL database, then verify all values.

Stop app writers before taking the snapshot and leave them stopped until cutover.
Run: python -m scripts.migrate_to_neon --source instance/backup.db
The destination comes from DATABASE_URL_UNPOOLED or DATABASE_URL in .env.
"""

import argparse
import os
from pathlib import Path
import sqlite3
import tempfile

from dotenv import load_dotenv
from sqlalchemy import create_engine, func, inspect, select, text

from app.database import database_url, engine_options
from app.extensions import db
from app import models  # noqa: F401: register all tables without starting the app


def copy_database(source_path, target_url):
    source_path = Path(source_path).resolve(strict=True)
    target_url = database_url(target_url)
    if target_url.get_backend_name() != "postgresql":
        raise ValueError("Destination must be PostgreSQL.")

    # SQLite's backup API includes WAL contents and produces a consistent snapshot.
    # The original file is opened read-only and is never modified.
    with tempfile.TemporaryDirectory(prefix="ankigpt-migrate-") as temporary:
        snapshot = Path(temporary) / "snapshot.db"
        source = sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)
        backup = sqlite3.connect(snapshot)
        try:
            source.backup(backup)
            if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("SQLite integrity check failed.")
            if backup.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("SQLite has broken foreign-key references.")
        finally:
            backup.close()
            source.close()

        reader = create_engine(database_url("sqlite:///" + snapshot.as_posix()))
        target = create_engine(target_url, **engine_options(target_url))
        counts = {}
        try:
            with reader.connect() as source_conn, target.begin() as dest:
                # All schema changes and data inserts roll back together on failure.
                db.metadata.create_all(dest)
                tables = db.metadata.sorted_tables
                quoted_tables = ", ".join(dest.dialect.identifier_preparer.quote(t.name) for t in tables)
                dest.execute(text(f"LOCK TABLE {quoted_tables} IN ACCESS EXCLUSIVE MODE"))
                for table in tables:
                    if dest.scalar(select(func.count()).select_from(table)):
                        raise ValueError(f"Destination table {table.name} is not empty; refusing to overwrite it.")

                source_tables = set(inspect(source_conn).get_table_names())
                for table in tables:
                    if table.name not in source_tables:
                        raise ValueError(f"Source is missing table {table.name}; upgrade the source schema first.")
                    columns = {c["name"] for c in inspect(source_conn).get_columns(table.name)}
                    if set(table.columns.keys()) - columns:
                        raise ValueError(f"Source table {table.name} is missing columns; upgrade it first.")
                    rows = [dict(row) for row in source_conn.execute(select(table).order_by(table.c.id)).mappings()]
                    # Parent tasks may have larger IDs than their children. Restore
                    # self-references only after every task has been inserted.
                    parents = []
                    for row in rows:
                        for column in table.columns:
                            value = row[column.name]
                            length = getattr(column.type, "length", None)
                            if isinstance(value, str) and length and len(value) > length:
                                raise ValueError(f"{table.name}.{column.name}, row {row['id']}, exceeds PostgreSQL length {length}.")
                        if table.name == "pipeline_task" and row["parent_id"] is not None:
                            parents.append((row["id"], row["parent_id"]))
                            row["parent_id"] = None
                    for offset in range(0, len(rows), 500):
                        dest.execute(table.insert(), rows[offset:offset + 500])
                    for row_id, parent_id in parents:
                        dest.execute(table.update().where(table.c.id == row_id).values(parent_id=parent_id))

                    original = [dict(row) for row in source_conn.execute(select(table).order_by(table.c.id)).mappings()]
                    copied = [dict(row) for row in dest.execute(select(table).order_by(table.c.id)).mappings()]
                    if original != copied:
                        raise ValueError(f"Verification failed for {table.name}; migration rolled back.")
                    counts[table.name] = len(original)

                # Explicit IDs do not advance Postgres sequences. Advance them so
                # the first new deck/card/user cannot collide with a migrated row.
                for table in tables:
                    quoted = dest.dialect.identifier_preparer.quote(table.name)
                    sequence = dest.scalar(text("SELECT pg_get_serial_sequence(:table_name, 'id')"), {"table_name": quoted})
                    if sequence:
                        highest = dest.scalar(select(func.max(table.c.id)))
                        dest.execute(text("SELECT setval(CAST(:sequence AS regclass), :value, :called)"),
                                     {"sequence": sequence, "value": highest if highest is not None else 1,
                                      "called": highest is not None})
            return counts
        finally:
            reader.dispose()
            target.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="SQLite file or backup to copy")
    args = parser.parse_args()
    load_dotenv()
    target = os.getenv("DATABASE_URL_UNPOOLED") or os.getenv("DATABASE_URL")
    if not target:
        parser.error("Set DATABASE_URL_UNPOOLED or DATABASE_URL in .env.")
    try:
        counts = copy_database(args.source, target)
    except Exception as exc:
        # Driver errors may contain SQL parameters, source text, or credentials.
        message = str(exc) if isinstance(exc, (ValueError, FileNotFoundError)) else type(exc).__name__
        parser.exit(1, f"Migration failed: {message}\n")
    for table, count in counts.items():
        print(f"{table}: {count} rows copied and verified")
    print("Migration committed. The source database has not been changed.")


if __name__ == "__main__":
    main()
