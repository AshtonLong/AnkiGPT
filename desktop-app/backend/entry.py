"""Entry point of the AnkiGPT Desktop backend (frozen as ankigpt-backend.exe).

The Electron shell starts this hidden, passes it everything it needs in environment
variables, reads one JSON line per message from its stdout, and closes its stdin to
stop it. See desktop-app/SPEC.md sections 2 and 5.10.

    ankigpt-backend.exe                 started by the shell
    ankigpt-backend.exe --self-check    prove the frozen bundle is complete, then exit
    python entry.py --dev               development only: no launch token needed, so the
                                        app can be opened in an ordinary browser
"""

import argparse
import io
import json
import logging
import multiprocessing
import os
import sqlite3
import sys
import tempfile
import threading
import zipfile
from logging.handlers import RotatingFileHandler

FROZEN = bool(getattr(sys, "frozen", False))
HERE = os.path.dirname(os.path.abspath(__file__))
if not FROZEN:
    # Run from a checkout: make the `app` package importable.
    sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..")))

REQUIRED_ENV = ("ANKIGPT_DATA_DIR", "ANKIGPT_LOG_DIR", "ANKIGPT_TOKEN", "SECRET_KEY", "ANKIGPT_VERSION")
LOG_FILE_BYTES = 2 * 1024 * 1024
LOG_FILES = 5
SERVER_THREADS = 8

logger = logging.getLogger("ankigpt.backend")


def bundled(*parts):
    """Path of a file shipped beside this script, or inside the frozen bundle."""
    return os.path.join(getattr(sys, "_MEIPASS", HERE), *parts)


def setup_logging(log_dir):
    os.makedirs(log_dir, exist_ok=True)
    handler = RotatingFileHandler(
        os.path.join(log_dir, "backend.log"), maxBytes=LOG_FILE_BYTES, backupCount=LOG_FILES - 1, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def watch_stdin(server):
    """Exit when stdin reaches end-of-file: the shell closed it to stop us, or died.

    A backend that outlived its shell would keep the database open and block the next launch.
    """
    def watch():
        try:
            while sys.stdin.buffer.read(4096):
                pass
        except (OSError, ValueError):
            pass
        logger.info("stdin closed; stopping")
        try:
            server.close()
        except Exception:
            logger.exception("Could not close the server cleanly")
        # The PDF layout library can start worker processes. Don't leave them behind.
        for child in multiprocessing.active_children():
            child.terminate()
        logging.shutdown()
        # Generation threads are daemons mid-request to OpenRouter; there is nothing to wait for.
        os._exit(0)

    threading.Thread(target=watch, name="ankigpt-stdin", daemon=True).start()


def serve(dev):
    required = [name for name in REQUIRED_ENV if not (dev and name == "ANKIGPT_TOKEN")]
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        print(
            "ankigpt-backend: missing environment variable(s): " + ", ".join(missing) + ".\n"
            "The AnkiGPT app sets these when it starts the backend; it is not meant to be run by hand.",
            file=sys.stderr,
        )
        return 2

    setup_logging(os.environ["ANKIGPT_LOG_DIR"])
    try:
        from waitress.server import create_server

        from app.desktop import create_desktop_app, emit, reserve_stdout, set_port

        reserve_stdout()
        version = os.environ["ANKIGPT_VERSION"]
        logger.info("Starting AnkiGPT backend %s (%s)", version, "development" if dev else "desktop")
        app = create_desktop_app(
            os.environ["ANKIGPT_DATA_DIR"], os.environ["SECRET_KEY"], token=os.environ.get("ANKIGPT_TOKEN", ""),
            version=version, check_token=not dev,
        )
        # Port 0: Windows picks a free port, so two programs can never fight over one.
        server = create_server(app, host="127.0.0.1", port=0, threads=SERVER_THREADS)
        port = int(server.effective_port)  # waitress reports it as a string
        set_port(app, port)
        if dev:
            print(f"AnkiGPT backend (development): http://127.0.0.1:{port}/decks", file=sys.stderr)
        else:
            watch_stdin(server)
        logger.info("Listening on 127.0.0.1:%s", port)
        emit("ready", port=port)
        server.run()
    except Exception:
        logger.exception("The backend could not start")
        raise
    return 0


# ----------------------------------------------------------------- self-check
SAMPLE_PHRASE = "Mitochondria"
SELF_CHECK_HOST = "127.0.0.1:1"
SELF_CHECK_TOKEN = "self-check"


def _check_imports():
    import onnxruntime
    import pymupdf
    import pymupdf.layout
    import pymupdf4llm

    return f"pymupdf {pymupdf.__version__}, pymupdf4llm {pymupdf4llm.__version__}, onnxruntime {onnxruntime.__version__}"


def _check_pdf_text():
    """The sample PDF must be read through the layout model, not the plain-text fallback.

    `extract_pdf_text` falls back silently when the layout library or one of its model
    files is missing, and the only symptom is worse text from every PDF. So watch which
    path it takes.
    """
    import pymupdf
    import pymupdf.layout  # noqa: F401  (activates the layout model)

    from app.services import pdf

    layout = pymupdf._get_layout
    if not callable(layout):
        raise RuntimeError("pymupdf.layout did not activate its model")
    seen = {"layout_calls": 0, "pages": None}

    def counting_layout(*args, **kwargs):
        seen["layout_calls"] += 1
        return layout(*args, **kwargs)

    read_pages = pdf._pages_with_pymupdf4llm

    def recording_pages(*args, **kwargs):
        seen["pages"] = read_pages(*args, **kwargs)
        return seen["pages"]

    pymupdf._get_layout = counting_layout
    pdf._pages_with_pymupdf4llm = recording_pages
    try:
        text, total, offsets = pdf.extract_pdf_text(bundled("selfcheck", "sample.pdf"))
    finally:
        pymupdf._get_layout = layout
        pdf._pages_with_pymupdf4llm = read_pages
    if not seen["pages"] or not any(seen["pages"]):
        raise RuntimeError("pymupdf4llm returned no pages, so extraction fell back to plain text")
    if not seen["layout_calls"]:
        raise RuntimeError("the layout model was never run")
    if SAMPLE_PHRASE not in text:
        raise RuntimeError(f"expected {SAMPLE_PHRASE!r} in the extracted text, got {text[:120]!r}")
    return f"{len(text)} characters from {total} page(s), layout model run {seen['layout_calls']} time(s)"


def _check_figures():
    from app.services.pipeline.figures import extract_figures

    figures = extract_figures(bundled("selfcheck", "sample.pdf"))
    if not figures or not figures[0]["image"].startswith(b"\x89PNG"):
        raise RuntimeError("no figure image was extracted from the sample PDF")
    return f"{len(figures)} figure(s), first is {figures[0]['width']}x{figures[0]['height']}"


def _check_export(app):
    from app.desktop import LOCAL_USER_EMAIL
    from app.extensions import db
    from app.models import Card, Deck, User
    from app.services.export import export_deck

    with app.app_context():
        user = User.query.filter_by(email=LOCAL_USER_EMAIL).one()
        deck = Deck(user_id=user.id, title="Self-check", card_style="mixed", status="ready", source_type="text",
                    source_text="x", settings_json={}, run_json={})
        db.session.add(deck)
        db.session.flush()
        db.session.add(Card(deck_id=deck.id, type="basic", front="Front?", back="Back.", status="ok"))
        db.session.add(Card(deck_id=deck.id, type="cloze", cloze_text="A {{c1::cloze}}.", extra="", status="ok"))
        db.session.commit()
        package, filename = export_deck(deck.id)
    with zipfile.ZipFile(io.BytesIO(package.getvalue())) as archive, tempfile.TemporaryDirectory() as folder:
        collection = archive.extract("collection.anki2", folder)
        connection = sqlite3.connect(collection)
        try:
            notes = connection.execute("SELECT count(*) FROM notes").fetchone()[0]
        finally:
            connection.close()
    if notes != 2:
        raise RuntimeError(f"expected 2 notes in the exported package, found {notes}")
    return f"{filename}: {notes} notes"


def _check_key_round_trip(app):
    from app.models import User
    from app.services.credentials import set_user_key, user_key

    key = "sk-or-v1-self-check"
    with app.app_context():
        user = User(email="self-check@ankigpt.invalid")
        set_user_key(user, key)
        if key in user.openrouter_key_encrypted:
            raise RuntimeError("the key was stored unencrypted")
        if user_key(user) != key:
            raise RuntimeError("the key did not survive encryption and decryption")
    return "encrypted and decrypted"


def _check_assets(app):
    for template in ("base.html", "decks.html", "settings.html", "partials/card_row.html"):
        app.jinja_env.get_template(template)
    client = app.test_client()
    paths = ("/decks", "/auth/profile", "/static/style.css", "/static/fonts.css", "/static/app.js",
             "/static/brand.svg", "/static/vendor/htmx.min.js", "/static/fonts/figtree-latin.woff2",
             "/static/fonts/fragment-mono-latin.woff2")
    for path in paths:
        response = client.get(path, base_url=f"http://{SELF_CHECK_HOST}", headers={"X-AnkiGPT-Token": SELF_CHECK_TOKEN})
        if response.status_code != 200:
            raise RuntimeError(f"{path} returned {response.status_code}")
    return f"{len(paths)} pages and static files served"


def self_check():
    """Exercise everything a bad bundle would break quietly. Prints a JSON report."""
    from app.desktop import create_desktop_app, reserve_stdout, set_port

    report_stream = reserve_stdout()
    checks = {}

    def run(name, check, *args):
        try:
            checks[name] = {"ok": True, "detail": check(*args)}
        except Exception as exc:  # every check reports, so one failure doesn't hide the next
            checks[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    run("imports", _check_imports)
    run("pdf_text_through_layout", _check_pdf_text)
    run("figure_extraction", _check_figures)
    with tempfile.TemporaryDirectory(prefix="ankigpt-self-check-") as folder:
        app = None
        try:
            app = create_desktop_app(os.path.join(folder, "data"), "self-check-secret", token=SELF_CHECK_TOKEN,
                                     version="self-check")
            set_port(app, SELF_CHECK_HOST.rsplit(":", 1)[1])
        except Exception as exc:
            checks["app"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if app is not None:
            run("apkg_export", _check_export, app)
            run("key_round_trip", _check_key_round_trip, app)
            run("templates_and_static", _check_assets, app)
            from app.extensions import db

            with app.app_context():
                db.session.remove()
                db.engine.dispose()

    ok = all(check["ok"] for check in checks.values())
    report = {"ok": ok, "frozen": FROZEN, "python": sys.version.split()[0], "checks": checks}
    report_stream.write(json.dumps(report, indent=2) + "\n")
    report_stream.flush()
    return 0 if ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ankigpt-backend", description="The AnkiGPT Desktop backend.")
    parser.add_argument("--self-check", action="store_true", help="check the bundle is complete, print a report, exit")
    parser.add_argument("--dev", action="store_true",
                        help="development only: skip the launch-token check so a browser can open the app")
    args = parser.parse_args(argv)
    if args.self_check:
        return self_check()
    if args.dev and FROZEN:
        print("ankigpt-backend: --dev is not available in a packaged build.", file=sys.stderr)
        return 2
    return serve(args.dev)


if __name__ == "__main__":
    # First, before anything else runs: in a frozen program a multiprocessing child
    # re-runs this file, and without this each child would try to start another server.
    multiprocessing.freeze_support()
    sys.exit(main())
