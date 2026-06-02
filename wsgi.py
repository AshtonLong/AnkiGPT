from dotenv import load_dotenv

# Load .env BEFORE create_app, since Config reads env vars at import time. Without
# this, gunicorn/uwsgi runs with dev defaults (dev secret, empty API key).
load_dotenv()

from app import create_app  # noqa: E402

app = create_app()
