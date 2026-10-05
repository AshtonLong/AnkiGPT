"""Everything desktop mode adds to the Flask app.

AnkiGPT Desktop runs this same app on the user's own computer behind an Electron
window: one local user, no sign-in, all data in one folder. `create_app` calls into
this module only when the config sets DESKTOP_MODE, which `DesktopConfig` alone does,
so the web app never runs any of it. The full design is in desktop-app/SPEC.md.
"""

import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import sys
import threading
from contextlib import closing
from datetime import datetime

from dotenv import dotenv_values
from flask import abort, current_app, has_app_context, redirect, request, url_for

from .config import Config, DesktopConfig
from .extensions import db, login_manager
from .models import Deck, PipelineTask, User, utcnow

logger = logging.getLogger(__name__)

# `.invalid` is reserved and can never receive mail, so this can't collide with a real account.
LOCAL_USER_EMAIL = "local@ankigpt.invalid"
INTERRUPTED_MESSAGE = "AnkiGPT was closed while this deck was generating. Retry to run it again."
TOKEN_HEADER = "X-AnkiGPT-Token"
BACKUPS_KEPT = 3

# The only keys settings.env may set. Anything else in the file is ignored and logged.
SETTINGS_KEYS = (
    "OPENROUTER_MODEL", "OPENROUTER_EMBEDDING_MODEL", "OPENROUTER_TEMPERATURE",
    "OPENROUTER_TIMEOUT_SECONDS", "OPENROUTER_MAX_TOKENS", "MAX_SOURCE_CHARS",
)
SETTINGS_PREFIXES = ("OPENROUTER_MODEL_", "OPENROUTER_REASONING_", "PIPELINE_")

# Account pages have nothing to do when there is no account.
_REDIRECTED_ENDPOINTS = frozenset({
    "main.index", "auth.signup", "auth.login", "auth.forgot_password", "auth.reset_password", "auth.logout",
})
_REMOVED_ENDPOINTS = frozenset({"legal.terms", "legal.privacy"})

_GUARD = "ankigpt_desktop_guard"
_LOCAL_USER_ID = "ankigpt_local_user_id"
_stdout_lock = threading.Lock()
# The stream `emit` writes to once `reserve_stdout` has claimed it. Until then, stdout.
_shell = None


def is_desktop():
    return has_app_context() and bool(current_app.config.get("DESKTOP_MODE"))


def settings_page_name():
    """What the page that holds the OpenRouter key is called in the running app.

    Every message that sends the user there takes the name from here, so desktop says
    "Settings" and the web app still says "My profile".
    """
    return "Settings" if is_desktop() else "My profile"


def template_context():
    return {"desktop_mode": is_desktop(), "settings_page": settings_page_name()}


# ------------------------------------------------------------- request guard
class RequestGuard:
    """WSGI middleware that turns away every request not made by this launch's own window.

    A local server can be reached by any web page in the user's browser and any program
    on the computer, and desktop mode has no login. So before Flask sees a request,
    including one for a static file, it must carry the launch token (which only the
    Electron shell knows) and name the exact loopback host and port it was sent to
    (which defeats DNS rebinding).
    """

    def __init__(self, wsgi_app, token, check_token=True):
        self.wsgi_app = wsgi_app
        self.token = (token or "").encode("latin-1", "replace")
        self.check_token = check_token
        # Set by `set_port` once the server has bound. Until then nothing is allowed.
        self.host = None

    def __call__(self, environ, start_response):
        if self.allows(environ):
            return self.wsgi_app(environ, start_response)
        body = b"Forbidden"
        start_response("403 FORBIDDEN", [
            ("Content-Type", "text/plain; charset=utf-8"), ("Content-Length", str(len(body))),
        ])
        return [body]

    def allows(self, environ):
        if self.host is None or environ.get("HTTP_HOST") != self.host:
            return False
        if not self.check_token:
            return True
        sent = environ.get("HTTP_X_ANKIGPT_TOKEN", "").encode("latin-1", "replace")
        return bool(self.token) and hmac.compare_digest(sent, self.token)


def set_port(app, port):
    """Tell the request guard which port the server bound, so it can check `Host`."""
    app.extensions[_GUARD].host = f"127.0.0.1:{int(port)}"


# ---------------------------------------------------------------- local user
@login_manager.request_loader
def _load_local_user(_request):
    # The login manager is shared by every app in the process, so check the mode here.
    if not is_desktop():
        return None
    return db.session.get(User, current_app.extensions.get(_LOCAL_USER_ID))


def ensure_local_user():
    """Find or create the one user every desktop request runs as. Returns it."""
    user = User.query.filter_by(email=LOCAL_USER_EMAIL).first()
    if user is None:
        # Not a valid hash, so no password can ever match it.
        user = User(email=LOCAL_USER_EMAIL, password_hash="!" + secrets.token_hex(32))
        db.session.add(user)
        db.session.commit()
    current_app.extensions[_LOCAL_USER_ID] = user.id
    return user


# ---------------------------------------------------------- interrupted runs
def fail_interrupted_runs():
    """Mark runs that were cut off when the app last closed as failed, so they can be retried.

    Generation lives on a thread of the process that just exited. Without this a deck
    that was mid-run would sit in `processing` forever. Only safe on desktop, where the
    single-instance lock guarantees no other process is generating.
    """
    decks = Deck.query.filter_by(status="processing").all()
    for deck in decks:
        run = dict(deck.run_json or {})
        run["last_error"] = INTERRUPTED_MESSAGE
        deck.run_json = run
        deck.status = "failed"
    tasks = PipelineTask.query.filter(PipelineTask.status.in_(("running", "queued"))).update(
        {"status": "failed", "finished_at": utcnow()}, synchronize_session=False
    )
    db.session.commit()
    if decks or tasks:
        logger.info("Marked %s interrupted deck(s) and %s task(s) as failed", len(decks), tasks)


# ------------------------------------------------------- messages to the shell
def reserve_stdout():
    """Keep stdout for messages to the shell alone. Returns the stream that reaches it.

    Libraries print: PyMuPDF, for one, reports problems in a PDF on stdout. After this
    call only `emit` writes to the pipe the shell reads. Everything else sent to stdout,
    by Python or by native code, lands on stderr instead, which the shell logs.
    """
    global _shell
    sys.stdout.flush()
    try:
        _shell = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", newline="\n")
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    except (AttributeError, OSError, ValueError):
        # No real file descriptors behind the streams. Swapping the Python objects is the best left.
        _shell, sys.stdout = sys.stdout, sys.stderr
    return _shell


def emit(event, **fields):
    """Send one message to the Electron shell: a JSON line on stdout, flushed at once.

    stdout carries nothing else (logging goes to a file), so the shell can parse every line.
    """
    line = json.dumps({"event": event, **fields}, separators=(",", ":"))
    with _stdout_lock:
        try:
            stream = _shell or sys.stdout
            stream.write(line + "\n")
            stream.flush()
        except (OSError, ValueError):
            # The shell is gone. The stdin watcher is about to end this process too.
            pass


def report_activity(active_runs):
    """The shell warns before quitting, and keeps the computer awake, while this is above zero."""
    emit("activity", active_runs=active_runs)


# ------------------------------------------------------------- settings.env
def _is_settings_key(key):
    return (key in SETTINGS_KEYS or key.startswith(SETTINGS_PREFIXES)) and hasattr(Config, key)


def _coerce(value, default):
    """Parse a settings.env string as the type the setting already has."""
    if isinstance(default, bool):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(value)
    if isinstance(default, float):
        return float(value)
    return value


def read_settings_env(path):
    """Read the hand-edited advanced settings file.

    Returns (settings, ignored): the config overrides to apply, and the names of the
    keys that were skipped because they are not on the allowed list or have a value
    that can't be parsed. Values are never returned for skipped keys, since someone may
    paste an API key into this file by mistake and it must not reach a log.
    """
    settings, ignored = {}, []
    if not os.path.isfile(path):
        return settings, ignored
    for key, value in dotenv_values(path, interpolate=False).items():
        if value is None or not _is_settings_key(key):
            ignored.append(key)
            continue
        try:
            settings[key] = _coerce(value, getattr(Config, key))
        except ValueError:
            ignored.append(key)
    return settings, ignored


# ------------------------------------------------------------------ backups
def backup_before_upgrade(data_dir, version):
    """Copy the database aside when a different app version is about to open it.

    Runs before the schema is touched. Returns the backup's path, or None when there
    was nothing to back up. The newest BACKUPS_KEPT copies are kept.
    """
    database = os.path.join(data_dir, "ankigpt.db")
    version_file = os.path.join(data_dir, "version.txt")
    previous = None
    if os.path.isfile(version_file):
        with open(version_file, encoding="utf-8") as fh:
            previous = fh.read().strip() or None
    if previous == version:
        return None

    backup_path = None
    if os.path.isfile(database):
        backups = os.path.join(data_dir, "backups")
        os.makedirs(backups, exist_ok=True)
        label = re.sub(r"[^A-Za-z0-9._+-]", "_", previous or "unknown")
        backup_path = os.path.join(backups, f"ankigpt-{label}-{datetime.now():%Y%m%d-%H%M%S}.db")
        # SQLite's backup API copies a consistent snapshot, including anything still in the WAL.
        with closing(sqlite3.connect(database)) as source, closing(sqlite3.connect(backup_path)) as target:
            source.backup(target)
        kept = sorted(
            (os.path.join(backups, name) for name in os.listdir(backups)
             if name.startswith("ankigpt-") and name.endswith(".db")),
            key=lambda path: (os.path.getmtime(path), path), reverse=True,
        )
        for stale in kept[BACKUPS_KEPT:]:
            try:
                os.remove(stale)
            except OSError:
                logger.warning("Could not remove old backup %s", stale)
        logger.info("Backed up the database from version %s to %s", previous or "unknown", backup_path)

    with open(version_file, "w", encoding="utf-8") as fh:
        fh.write(version)
    return backup_path


# ------------------------------------------------------------------ app setup
def _desktop_routes():
    if request.endpoint in _REDIRECTED_ENDPOINTS:
        return redirect(url_for("main.decks"))
    if request.endpoint in _REMOVED_ENDPOINTS:
        abort(404)
    return None


def init_app(app):
    """Wire desktop mode into a new app. Called by `create_app` before the extensions."""
    guard = RequestGuard(app.wsgi_app, app.config.get("DESKTOP_TOKEN"), app.config.get("DESKTOP_CHECK_TOKEN", True))
    app.wsgi_app = guard
    app.extensions[_GUARD] = guard
    app.before_request(_desktop_routes)


def prepare_database(app):
    """Start-up work that needs the schema in place. Called by `create_app`."""
    ensure_local_user()
    fail_interrupted_runs()


def create_desktop_app(data_dir, secret_key, token="", version="", check_token=True):
    """Build the app for one launch of AnkiGPT Desktop, with `data_dir` as its instance folder."""
    from . import create_app

    data_dir = os.path.abspath(data_dir)
    os.makedirs(data_dir, exist_ok=True)
    backup_before_upgrade(data_dir, version)
    settings, ignored = read_settings_env(os.path.join(data_dir, "settings.env"))
    if settings:
        logger.info("settings.env applied: %s", ", ".join(sorted(settings)))
    if ignored:
        logger.warning("settings.env keys ignored (not an advanced setting, or not a valid value): %s",
                       ", ".join(sorted(ignored)))
    config = DesktopConfig.for_launch(
        data_dir, secret_key, token=token, version=version, check_token=check_token, settings=settings,
    )
    return create_app(config, instance_path=data_dir)
