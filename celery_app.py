from dotenv import load_dotenv

# Load .env BEFORE create_app so the Celery worker process uses the same config
# (broker URL, API key, secret) as the web process.
load_dotenv()

from app import create_app  # noqa: E402
from app.tasks import celery, init_celery  # noqa: E402

app = create_app()
celery = init_celery(app)
