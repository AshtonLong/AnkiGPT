from celery import Celery

celery = Celery(__name__)


def init_celery(app):
    celery.conf.update(
        broker_url=app.config["CELERY_BROKER_URL"],
        result_backend=app.config["CELERY_RESULT_BACKEND"],
        task_always_eager=app.config["CELERY_TASK_ALWAYS_EAGER"],
        task_eager_propagates=app.config["CELERY_TASK_EAGER_PROPAGATES"],
    )

    TaskBase = celery.Task

    class ContextTask(TaskBase):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return TaskBase.__call__(self, *args, **kwargs)

    celery.Task = ContextTask
    return celery


@celery.task
def generate_deck_task(deck_id):
    from .services.deckgen import generate_deck

    return generate_deck(deck_id)


@celery.task
def export_deck_task(deck_id):
    from .services.export import export_deck

    return export_deck(deck_id)
