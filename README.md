# AnkiGPT

AnkiGPT is a Flask web app that turns study material into editable Anki flashcards using an LLM via OpenRouter.  
You can paste text or upload a PDF, generate cards, review/edit them, and export a ready-to-import `.apkg` deck.

## Features

- Text and PDF input
- Basic and cloze card generation
- Per-deck generation settings (`focus`, `exclude`, `glossary`, `chunk size`)
- Card validation pipeline:
  - cloze syntax checks
  - math normalization checks
  - source-scope checks
  - deduplication
- In-browser card editor with HTMX inline save/improve actions
- Bulk actions (delete, restore, tag, regenerate by source chunk)
- `.apkg` export compatible with Anki
- Optional authentication-free demo mode
- Sync mode by default, optional async Celery worker mode

## Tech Stack

- Backend: Flask, Flask-Login, Flask-SQLAlchemy, Flask-Migrate
- Queue: Celery (optional async mode), Redis (optional broker/backend)
- AI: OpenRouter Chat Completions API
- Validation: Pydantic
- Export: `genanki`
- PDF parsing: `pypdf`
- Database: SQLite by default (configurable via `DATABASE_URL`)

## How It Works

1. Create a deck from text or PDF.
2. Review extracted source text and choose generation settings.
3. App splits source into chunks and calls OpenRouter per chunk.
4. Responses are parsed/validated, invalid cards are dropped, duplicates are marked deleted.
5. You review/edit cards and export an `.apkg` file.

## Project Structure

```text
.
|-- app/
|   |-- routes/
|   |   |-- auth.py
|   |   `-- main.py
|   |-- services/
|   |   |-- chunking.py
|   |   |-- deckgen.py
|   |   |-- export.py
|   |   |-- llm.py
|   |   |-- pdf.py
|   |   |-- schemas.py
|   |   `-- validators.py
|   |-- templates/
|   |-- static/
|   |-- config.py
|   |-- extensions.py
|   |-- models.py
|   |-- tasks.py
|   `-- __init__.py
|-- celery_app.py
|-- run.py
|-- wsgi.py
|-- requirements.txt
`-- instance/          # sqlite db, uploads, exports (created automatically)
```

## Requirements

- Python 3.10+ (3.12 recommended)
- An OpenRouter API key
- Redis only if you want true async background workers

## Quick Start

### 1. Create and activate a virtual environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 2. Install dependencies

```powershell
pip install -r requirements.txt
```

### 3. Configure environment

Create/update `.env` in the project root:

```env
SECRET_KEY=change-me
DATABASE_URL=sqlite:///instance/ankigpt.db
AUTH_REQUIRED=true
UPLOAD_MAX_MB=20
UPLOAD_FOLDER=instance/uploads
EXPORT_FOLDER=instance/exports

OPENROUTER_API_KEY=your_openrouter_api_key
OPENROUTER_MODEL=google/gemini-3-flash-preview
OPENROUTER_SITE_URL=
OPENROUTER_APP_NAME=AnkiGPT
OPENROUTER_TIMEOUT_SECONDS=120
OPENROUTER_MAX_RETRIES=2
OPENROUTER_RETRY_BACKOFF_SECONDS=1.5

CELERY_BROKER_URL=
CELERY_RESULT_BACKEND=
CELERY_ALWAYS_EAGER=true
```

### 4. Run the app

```powershell
python run.py
```

Open `http://127.0.0.1:5000`.

## Async Worker Mode (Optional)

By default, tasks run eagerly in-process (`CELERY_ALWAYS_EAGER=true`).  
To run true background jobs:

1. Set:
   - `CELERY_ALWAYS_EAGER=false`
   - `CELERY_BROKER_URL=redis://localhost:6379/0`
   - `CELERY_RESULT_BACKEND=redis://localhost:6379/0`
2. Start Redis.
3. Run Flask app (`python run.py`).
4. Start Celery worker:

```powershell
celery -A celery_app.celery worker --loglevel=info
```

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | `dev-secret` | Flask session/CSRF secret. Change in real environments. |
| `DATABASE_URL` | `sqlite:///instance/ankigpt.db` | SQLAlchemy connection URL. |
| `AUTH_REQUIRED` | `true` | Require login if true; if false uses a local demo user. |
| `UPLOAD_MAX_MB` | `20` | Max upload size in MB (`MAX_CONTENT_LENGTH`). |
| `UPLOAD_FOLDER` | `instance/uploads` | PDF upload storage directory. |
| `EXPORT_FOLDER` | `instance/exports` | Export directory (app currently streams files directly). |
| `OPENROUTER_API_KEY` | `` | Required for generation/improve calls. |
| `OPENROUTER_MODEL` | `google/gemini-3-flash-preview` | Model sent to OpenRouter. |
| `OPENROUTER_SITE_URL` | `` | Optional `HTTP-Referer` header for OpenRouter. |
| `OPENROUTER_APP_NAME` | `AnkiGPT` | Optional `X-Title` header for OpenRouter. |
| `OPENROUTER_TIMEOUT_SECONDS` | `120` | Request timeout per OpenRouter call. |
| `OPENROUTER_MAX_RETRIES` | `2` | Retries for transient OpenRouter failures (`429`, `5xx`, network). |
| `OPENROUTER_RETRY_BACKOFF_SECONDS` | `1.5` | Base exponential backoff between retries. |
| `CELERY_BROKER_URL` | `` | Broker URL (Redis/Rabbit/etc). |
| `CELERY_RESULT_BACKEND` | `` | Celery result backend URL. |
| `CELERY_ALWAYS_EAGER` | `true` | Run tasks synchronously in request process. |

## Auth Behavior

- If `AUTH_REQUIRED=true`:
  - Signup/login required for deck actions.
  - Standard user records in database.
- If `AUTH_REQUIRED=false`:
  - App auto-creates/uses local user `demo@local`.
  - Useful for local demos and rapid testing.

## Main Routes

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Landing page |
| `GET` | `/decks` | List decks |
| `GET,POST` | `/decks/new` | Create deck from text/PDF |
| `GET,POST` | `/decks/<deck_id>/preview` | Review source + generation settings |
| `GET` | `/decks/<deck_id>/status` | Generation progress/status |
| `GET` | `/decks/<deck_id>` | Card editor |
| `POST` | `/decks/<deck_id>/export` | Export `.apkg` |
| `POST` | `/decks/<deck_id>/delete` | Delete deck |
| `POST` | `/cards/<card_id>` | Save a single card edit |
| `POST` | `/cards/<card_id>/improve` | LLM card rewrite |
| `POST` | `/cards/bulk` | Bulk delete/restore/tag/regenerate |
| `GET,POST` | `/auth/signup` | Signup |
| `GET,POST` | `/auth/login` | Login |
| `POST` | `/auth/logout` | Logout |

## Data Model Summary

- `User`: account credentials and timestamps
- `Deck`: deck metadata, source content, settings, status
- `Source`: generated chunk records (one row per chunk)
- `Card`: generated/editable cards with status and tags
- `LLMRun`: generation/improvement request logs, parsed payloads, errors, usage

## Export Details

- Exports only cards with `status="ok"`.
- Supports:
  - Basic model (`Front`, `Back`)
  - Cloze model (`Text`, `Extra`)
- Tags are sanitized before writing to Anki.
- Output filename format: `<safe_deck_title>_<deck_id>.apkg`

## Troubleshooting

- "Generation failed":
  - Check the exact status-page error message (it now shows the OpenRouter failure reason).
  - For rate limits, wait briefly and retry.
  - Check `OPENROUTER_API_KEY`.
  - Verify model name in `OPENROUTER_MODEL`.
  - Confirm internet access and OpenRouter account limits.
- Stuck in processing with async mode:
  - Confirm Redis is up and URLs are correct.
  - Confirm Celery worker is running with `-A celery_app.celery`.
- "No cards to export":
  - Deck has zero cards in `ok` state; review filters/validation outcomes.
- PDF has little/no extracted text:
  - PDF may be scanned/image-only; OCR is not included.

## Development Notes

- Tables are auto-created on app startup via `db.create_all()` in `app/__init__.py`.
- `Flask-Migrate` is installed, but this project currently relies on auto-create behavior.
- No formal test suite is included yet.

## Security Notes

- Do not use the default `SECRET_KEY` outside local development.
- Do not commit `.env` secrets.
- Uploaded files and generated content may contain sensitive study material; handle storage accordingly.
