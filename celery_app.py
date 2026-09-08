from dotenv import load_dotenv

# Load .env BEFORE create_app so the Celery worker process uses the same config
# (broker URL, API key, secret) as the web process.
load_dotenv()

from app import create_app  # noqa: E402
from app.tasks import celery  # noqa: E402,F401 — worker entrypoint: `celery -A celery_app.celery`

# create_app() calls init_celery(), which configures the `celery` app imported above.
app = create_app()
