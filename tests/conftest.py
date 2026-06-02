import pytest

from app import create_app
from app.config import Config
from app.extensions import db as _db


class TestConfig(Config):
    TESTING = True
    SECRET_KEY = "test-secret"
    AUTH_REQUIRED = True
    WTF_CSRF_ENABLED = False
    OPENROUTER_API_KEY = ""
    CELERY_TASK_ALWAYS_EAGER = True


@pytest.fixture
def app(tmp_path):
    db_file = tmp_path / "test.db"
    TestConfig.SQLALCHEMY_DATABASE_URI = f"sqlite:///{db_file}"
    application = create_app(TestConfig)
    yield application
    with application.app_context():
        _db.session.remove()
        _db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def db(app):
    with app.app_context():
        yield _db


def register(client, email="a@example.com", password="password123"):
    return client.post(
        "/auth/signup",
        data={"email": email, "password": password},
        follow_redirects=True,
    )


def login(client, email="a@example.com", password="password123"):
    return client.post(
        "/auth/login",
        data={"email": email, "password": password},
        follow_redirects=True,
    )


def logout(client):
    return client.post("/auth/logout", follow_redirects=True)
