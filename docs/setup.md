# Setup and configuration

[README](../README.md) · [User guide](user-guide.md) · [Setup](setup.md) · [Architecture](architecture.md)

Use Python 3.12 (the Docker image uses 3.12), or Docker with Compose.
Run commands from the repository root. Do not overwrite an existing `.env`; merge
needed settings into it. Generate a session secret with:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

## Quick start (local)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item example.env .env # set a random SECRET_KEY
python run.py
```

Open `http://127.0.0.1:5000`, sign in or create an account, and save your OpenRouter
API key under **My profile** (see [OpenRouter API keys](#openrouter-api-keys)). Every workspace route
requires authentication; decks and their cards, sources, images, progress, and exports
are accessible only to their owner. The landing-page sample remains public.

## Quick start (Docker)

```bash
cp example.env .env        # then set SECRET_KEY in .env
docker compose up --build
```

Open `http://localhost:5000`. Compose runs a single `web` container; the SQLite
database and uploads live in the `ankigpt-data` volume. Run the tests in the
container with:

```bash
docker compose exec web sh -c "pip install -q -r requirements-dev.txt && python -m pytest tests/ -q"
```

## Configuration

### OpenRouter API keys

AnkiSpark is free and has no billing. Each user pays OpenRouter directly for the AI
calls their own decks make:

1. Create a key at [openrouter.ai/keys](https://openrouter.ai/keys) and add credit to
   the OpenRouter account. A credit limit on the key caps what it can spend.
2. In AnkiSpark, open **My profile → OpenRouter API key**, paste the key and save.

The key is encrypted with a key derived from `SECRET_KEY` before it is written to the
database, and only its last four characters are ever shown again. Every generation
run, AI improve, regenerate and Coach call for a deck uses the deck owner's key.
Without a key, **Plan & generate** and the editor's AI actions are refused with a link
to the profile page.

Keep `SECRET_KEY` stable. If it changes, saved keys can no longer be decrypted: the
app treats them as missing and asks each user to paste theirs again.

`OPENROUTER_API_KEY` in `.env` is optional. When set, it is used for accounts that have
not saved their own key. That suits a private, single-user install; on a public server
leave it empty, or every such account spends the server's credits with no limit.

### Database

All data lives in one SQLite file: `instance/ankigpt.db` by default, or the path in
`DATABASE_URL` (a relative path is placed in the instance folder). The app refuses to
start with any other kind of database URL. It runs SQLite in WAL mode, so the
generation thread and web requests can share the file; keep the file on a local disk,
not a network share.

To back it up while the app runs, use SQLite's backup API so the WAL is included
(copying just the live `.db` file can miss recent writes):

```bash
docker compose exec web python -c "import sqlite3; s = sqlite3.connect('/app/instance/ankigpt.db'); d = sqlite3.connect('/app/instance/backup.db'); s.backup(d); d.close(); s.close()"
docker compose cp web:/app/instance/backup.db ./ankigpt-backup.db
```

The Docker app's database is `/app/instance/ankigpt.db` inside the `ankigpt-data`
volume, which is a different file from `instance/ankigpt.db` in a local checkout.

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | `dev-secret` | Session/CSRF signing secret, and the secret that saved OpenRouter keys are encrypted with. Use a strong random value and keep it stable; the default only triggers a warning. |
| `SESSION_COOKIE_SECURE` | `false` | Set `true` when serving over HTTPS. |
| `DATABASE_URL` | `sqlite:///instance/ankigpt.db` | SQLite URL; nothing else is accepted. |
| `OPENROUTER_API_KEY` | | Optional fallback for accounts without their own key. Users normally save a key under **My profile**. |
| `OPENROUTER_MODEL` | `openai/gpt-6-luna` | Default model for every role. |
| `OPENROUTER_MODEL_{MAPPER,CHEATSHEET,PLANNER,WORKER,CRITIC,RECONCILE,VISION}` | | Per-role overrides (e.g. a stronger planner). |
| `OPENROUTER_EMBEDDING_MODEL` | `openai/text-embedding-3-small` | Used for duplicate clustering. |
| `OPENROUTER_REASONING_{PLANNER,CHEATSHEET,MAPPER,WORKER,CRITIC,RECONCILE,VISION}` | `medium`/`low` | Reasoning effort per role (planner and cheat sheet default to `medium`); empty omits the parameter. |
| `OPENROUTER_TEMPERATURE` | | Unset by default — reasoning models reject it. |
| `OPENROUTER_SITE_URL` / `OPENROUTER_APP_NAME` | empty / `AnkiSpark` | Optional provider attribution headers. |
| `OPENROUTER_MAX_TOKENS` | `16000` | Output cap per call (OpenRouter charges per token used). |
| `OPENROUTER_TIMEOUT_SECONDS` / `_MAX_RETRIES` / `_RETRY_BACKOFF_SECONDS` | `180` / `2` / `1.5` | HTTP behaviour. |
| `PIPELINE_MAX_WORKERS` | `6` | Concurrent model calls. |
| `PIPELINE_PLANNER_MAX_TURNS` | `14` | Planner agent loop cap. |
| `PIPELINE_UNIT_MAX_CHARS` | `14000` | Larger units are split deterministically. |
| `PIPELINE_CRITIC_ENABLED` | `true` | Critic phase. |
| `PIPELINE_COVERAGE_ENABLED` | `true` | Coverage audit + gap fill. |
| `PIPELINE_EMBED_DEDUPE_ENABLED` | `true` | Embedding-based duplicate clustering. |
| `PIPELINE_DEDUPE_THRESHOLD` | `0.90` | Cosine threshold for a duplicate cluster. |
| `PIPELINE_CACHE_ENABLED` | `true` | Content-addressed result cache. |
| `PIPELINE_FIGURES_ENABLED` / `PIPELINE_MAX_FIGURES` | `true` / `24` | Figure extraction from PDFs. |
| `MAX_SOURCE_CHARS` | `400000` | Longer sources are truncated with a warning (`0` disables). |
| `PROXY_FIX_HOPS` | `0` | Trust this many reverse-proxy hops of `X-Forwarded-*` headers (production compose sets `1`). |
| `MAIL_SMTP_HOST` / `_PORT` / `_USERNAME` / `_PASSWORD`, `MAIL_FROM` | / `465` | SMTP for password-reset email. Unset, links are logged instead. |
| `LEGAL_NAME` / `SUPPORT_EMAIL` / `LEGAL_JURISDICTION` | `AnkiSpark` / / `Canada` | Shown on the legal pages and footer. |
| `UPLOAD_MAX_MB` / `UPLOAD_FOLDER` | `50` / `instance/uploads` | Uploads. |
| `GENERATION_IN_THREAD` | `true` | Run generation on a background thread (tests set `false` to run inline). |


## Deployment and data

`python run.py` starts Flask with debug mode enabled for local development. The Docker
image uses Gunicorn with two workers on port 8000; Compose exposes port 5000.
Generation lives in the web process and can be interrupted by a restart or deployment.
Closing the browser does not stop the server, but stopping the server stops its work.

Set a strong `SECRET_KEY`, serve through HTTPS, and enable `SESSION_COOKIE_SECURE`
when deploying. The app warns about its default secret; it does not refuse startup.
Users bring their own OpenRouter keys; see [OpenRouter API keys](#openrouter-api-keys).

Workspace routes require sign-in and enforce deck ownership, including cards, images,
progress, and downloads. Generation sends source text, prompts, and selected figure
images through OpenRouter for AI processing. Sources, cards, figure bytes, traces,
review stats, and cached results remain in the SQLite database. Uploaded PDF files
are deleted after extraction on a best-effort basis; extracted content remains.
Deleting a deck cascades through its related records; the shared cache is separate.

The page loads fonts and HTMX from external CDNs. Python dependencies must also be
installed before an offline test run.
