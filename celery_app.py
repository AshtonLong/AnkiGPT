from app import create_app
from app.tasks import celery, init_celery

app = create_app()
celery = init_celery(app)
