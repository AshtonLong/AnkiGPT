# AnkiGPT MVP

Flask web app that turns text or PDFs into Anki decks using OpenRouter.

## Quick start
1. Create a virtualenv and install deps:
   - `python -m venv .venv`
   - `source .venv/bin/activate`
   - `pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and set `OPENROUTER_API_KEY`.
3. Run the app:
   - `python run.py`
4. Visit `http://localhost:5000`

## Optional: background worker
By default tasks run eagerly in-process (no Redis required).
If you want real async processing, set `CELERY_ALWAYS_EAGER=false`, add Redis URLs, and run:
- `celery -A celery_app.celery worker -l info`
