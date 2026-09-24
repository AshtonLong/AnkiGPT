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
Copy-Item example.env .env # set a random SECRET_KEY and OPENROUTER_API_KEY
python run.py
```

Open `http://127.0.0.1:5000` and sign in or create an account. Every workspace route
requires authentication; decks and their cards, sources, images, progress, and exports
are accessible only to their owner. The landing-page sample remains public.

## Quick start (Docker)

```bash
cp example.env .env        # then set SECRET_KEY and OPENROUTER_API_KEY in .env
docker compose up --build
```

Open `http://localhost:5000`. Compose runs a single `web` container; the SQLite
database and uploads live in the `ankigpt-data` volume. Run the tests in the
container with:

```bash
docker compose exec web sh -c "pip install -q -r requirements-dev.txt && python -m pytest tests/ -q"
```

## Configuration

### Neon PostgreSQL

Install `requirements.txt`, then set `DATABASE_URL` in `.env` to the pooled URL
from the Neon project's **Connect** dialog. Standard `postgresql://` URLs are
supported; Neon connections use TLS and check pooled connections before reuse
so the app can reconnect after the database scales to zero. Credentials stay in
the ignored `.env` file. Connection normalization is implemented in `app/database.py`.

For an existing SQLite database, stop app writers and take a backup using
SQLite's backup API (include the WAL; do not copy just a live `.db` file). The
Docker app's database is `/app/instance/ankigpt.db` inside the named volume and
may differ from the Windows `instance/ankigpt.db` file.
Set `DATABASE_URL_UNPOOLED` in `.env` to Neon's direct connection URL and run:

```powershell
python -m scripts.migrate_to_neon --source instance/backup.db
```

The migration requires an empty destination, copies all eight model tables,
preserves IDs and task relationships, compares every copied value, and advances
PostgreSQL ID sequences. Failure rolls back schema and row changes. It never
modifies the source file. Keep the backup and the app stopped until verification
passes, then set `DATABASE_URL` to the pooled Neon URL and recreate the app:

```powershell
docker compose up -d --build --force-recreate web
```

Use the same `DATABASE_URL` for `python run.py` to access the migrated database
locally. The old SQLite database remains available for recovery. Neon hosts the
database; the Flask app continues running wherever you deploy its container.

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | `dev-secret` | Session/CSRF signing secret. Use a strong random value; the default only triggers a warning. |
| `SESSION_COOKIE_SECURE` | `false` | Set `true` when serving over HTTPS. |
| `DATABASE_URL` | `sqlite:///instance/ankigpt.db` | SQLAlchemy URL. |
| `OPENROUTER_API_KEY` | | Required for AI operations. |
| `OPENROUTER_MODEL` | `openai/gpt-6-luna` | Default model for every role. |
| `OPENROUTER_MODEL_{MAPPER,PLANNER,WORKER,CRITIC,RECONCILE,VISION}` | | Per-role overrides (e.g. a stronger planner). |
| `OPENROUTER_EMBEDDING_MODEL` | `openai/text-embedding-3-small` | Used for duplicate clustering. |
| `OPENROUTER_REASONING_{PLANNER,MAPPER,WORKER,CRITIC,RECONCILE,VISION}` | `medium`/`low` | Reasoning effort per role; empty omits the parameter. |
| `OPENROUTER_TEMPERATURE` | | Unset by default — reasoning models reject it. |
| `OPENROUTER_SITE_URL` / `OPENROUTER_APP_NAME` | empty / `AnkiSpark` | Optional provider attribution headers. |
| `OPENROUTER_MAX_TOKENS` | `16000` | Output cap per call (billing is per token used). |
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
| `MAX_SOURCE_CHARS` | `400000` | Longer sources are truncated with a warning (`0` disables). With billing on, the plan's per-deck size applies below this. |
| `BILLING_ENABLED` | `false` | Enforce monthly page allowances and turn on Stripe subscriptions. |
| `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET` | | Stripe API key and webhook signing secret. |
| `STRIPE_PORTAL_CONFIGURATION` | | Customer Portal configuration from `scripts/stripe_setup.py` (empty uses the account default). |
| `STRIPE_AUTOMATIC_TAX` | `false` | Collect sales tax/VAT with Stripe Tax at checkout. |
| `PROXY_FIX_HOPS` | `0` | Trust this many reverse-proxy hops of `X-Forwarded-*` headers (production compose sets `1`). |
| `MAIL_SMTP_HOST` / `_PORT` / `_USERNAME` / `_PASSWORD`, `MAIL_FROM` | / `465` | SMTP for password-reset email. Unset, links are logged instead. |
| `LEGAL_NAME` / `SUPPORT_EMAIL` / `LEGAL_JURISDICTION` | `AnkiSpark` / / `Canada` | Shown on the legal pages and footer. |
| `UPLOAD_MAX_MB` / `UPLOAD_FOLDER` | `50` / `instance/uploads` | Uploads. |
| `GENERATION_IN_THREAD` | `true` | Run generation on a background thread (tests set `false` to run inline). |


### Billing (Stripe)

Billing is off by default, so a self-hosted install is unmetered. To sell plans:

1. Put a **test-mode** secret key in `.env` as `STRIPE_SECRET_KEY`, then run
   `python -m scripts.stripe_setup`. It creates the Pro and Max products, monthly and
   yearly prices (found later by lookup keys such as `ankigpt_pro_monthly`), and a
   Customer Portal configuration that allows plan switches and cancel-at-period-end.
   It is safe to rerun.
2. Forward webhooks while developing: `stripe listen --forward-to localhost:5000/billing/webhook`,
   and copy the printed `whsec_...` into `STRIPE_WEBHOOK_SECRET`. In production, pass
   `--webhook-url https://your.domain/billing/webhook` to the setup script instead.
3. Set `BILLING_ENABLED=true` and restart. Test with card `4242 4242 4242 4242`.
4. Repeat with the live key when you launch.

Plans, prices and allowances live in `app/services/billing.py` (`PLANS`). Usage is
metered in pages of 4,000 extracted characters when a deck's generation starts; a
deck's next two reruns are free, and deleting a deck does not refund its pages.
Allowances reset on the 1st of each month (UTC). Changing a price in code needs
`python -m scripts.stripe_setup --reprice`.

## Deployment and data

`python run.py` starts Flask with debug mode enabled for local development. The Docker
image uses Gunicorn with two workers on port 8000; Compose exposes port 5000.
Generation lives in the web process and can be interrupted by a restart or deployment.
Closing the browser does not stop the server, but stopping the server stops its work.

Set a strong `SECRET_KEY`, serve through HTTPS, and enable `SESSION_COOKIE_SECURE`
when deploying. The app warns about its default secret; it does not refuse startup.
The API key is configured for the server, shared by its users. There is no API-key
field on the profile page.

Workspace routes require sign-in and enforce deck ownership, including cards, images,
progress, and downloads. Generation sends source text, prompts, and selected figure
images through OpenRouter for AI processing. Sources, cards, figure bytes, traces,
review stats, and cached results remain in the configured database. Uploaded PDF files
are deleted after extraction on a best-effort basis; extracted content remains.
Deleting a deck cascades through its related records; the shared cache is separate.

The page loads fonts and HTMX from external CDNs. Python dependencies must also be
installed before an offline test run.
