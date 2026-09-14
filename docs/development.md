# Development

[README](../README.md) · [User guide](user-guide.md) · [Setup](setup.md) · [Architecture](architecture.md)

## Stack

Flask, Flask-Login, SQLAlchemy, Flask-Migrate, and Flask-WTF serve Jinja templates
with CSS, JavaScript, and HTMX. OpenRouter supplies chat, tool calls, vision, and
embeddings. Pydantic validates outputs; NumPy supports clustering; genanki writes
packages. PDF extraction uses pymupdf4llm with optional layout analysis and a pypdf
fallback; PyMuPDF renders figures. SQLite is the local default; PostgreSQL is supported.

## Testing

```powershell
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

The suite includes a scripted model (`tests/conftest.py::FakeLLM`) that drives the whole
pipeline — planner tool calls, workers, critic verdicts, duplicate resolution, coverage
audit, and the coach — without network access.

## Routes

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Public landing page with illustrative sample |
| `GET,POST` | `/auth/signup` · `/auth/login` | Register / sign in |
| `POST` | `/auth/logout` | Sign out |
| `GET,POST` | `/auth/profile` | Profile, email, and password settings |
| `POST` | `/decks/<id>/delete` | Permanently delete a deck |
| `GET` | `/decks` | Library |
| `GET,POST` | `/decks/new` | Create a deck from text/PDF (figures extracted here) |
| `GET,POST` | `/decks/<id>/preview` | Review source, brief the planner, start the run |
| `GET` | `/decks/<id>/status` | Live run trace |
| `GET` | `/decks/<id>/progress.json` | Trace as JSON (polled by the status page) |
| `GET,POST` | `/decks/<id>/plan` | Review / edit / run the work order; re-plan |
| `GET` | `/decks/<id>` | Card editor with run insights |
| `POST` | `/decks/<id>/reviews` | Import an Anki package with review history |
| `POST` | `/decks/<id>/coach` | Rewrite struggling cards |
| `POST` | `/decks/<id>/export` | Export `.apkg` (with figure media) |
| `POST` | `/cards/<id>` · `/cards/<id>/improve` · `/cards/bulk` | Edit, AI-improve, bulk (delete/restore/tag/regenerate unit/coach) |
| `GET` | `/figures/<id>.png` | Figure image (owner-scoped) |


These are browser-oriented routes, not a versioned public API. State-changing
requests require CSRF tokens and workspace routes require an authenticated session.

## Refresh README screenshots

```powershell
pip install playwright
python -m playwright install chromium
python -m scripts.capture_docs
```

The optional capture tool starts the real Flask app on a temporary loopback port,
uses a disposable SQLite database and synthetic account/source/cards/plan, and saves
three PNGs to `docs/images/`. It does not load `.env` or call an AI provider. It shuts
down its server and removes its temporary database afterward. Playwright is a docs
tool and is not required to run the app. Fonts and HTMX still use the page's CDNs.

Review every screenshot for readable text, complete controls, and current styling
before committing it. These images demonstrate the interface, not a measured AI run.
