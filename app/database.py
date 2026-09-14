"""Database connection settings shared by the app and migration utility."""

from sqlalchemy.engine import make_url


def database_url(value):
    url = make_url(value)
    if url.drivername == "postgres":
        url = url.set(drivername="postgresql")
    if url.get_backend_name() == "postgresql" and (url.host or "").endswith(".neon.tech"):
        # Neon connections must stay encrypted, including URLs pasted without options.
        if url.query.get("sslmode") not in ("require", "verify-ca", "verify-full"):
            url = url.update_query_dict({"sslmode": "require"})
    return url


def engine_options(url):
    if database_url(url).get_backend_name() == "postgresql":
        return {
            "pool_pre_ping": True,
            "pool_recycle": 240,
            "connect_args": {"connect_timeout": 15},
        }
    return {}
