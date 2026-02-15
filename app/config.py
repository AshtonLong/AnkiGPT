import os


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret")
    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///instance/ankigpt.db")
    SQLALCHEMY_DATABASE_URI = DATABASE_URL
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
    OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-3-flash-preview")
    OPENROUTER_SITE_URL = os.getenv("OPENROUTER_SITE_URL", "")
    OPENROUTER_APP_NAME = os.getenv("OPENROUTER_APP_NAME", "AnkiGPT")
    OPENROUTER_TIMEOUT_SECONDS = float(os.getenv("OPENROUTER_TIMEOUT_SECONDS", "120"))
    OPENROUTER_MAX_RETRIES = int(os.getenv("OPENROUTER_MAX_RETRIES", "2"))
    OPENROUTER_RETRY_BACKOFF_SECONDS = float(os.getenv("OPENROUTER_RETRY_BACKOFF_SECONDS", "1.5"))
    AUTH_REQUIRED = os.getenv("AUTH_REQUIRED", "true").lower() == "true"
    UPLOAD_MAX_MB = int(os.getenv("UPLOAD_MAX_MB", "1024"))
    MAX_CONTENT_LENGTH = UPLOAD_MAX_MB * 1024 * 1024
    UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", "instance/uploads")
    EXPORT_FOLDER = os.getenv("EXPORT_FOLDER", "instance/exports")
    CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "")
    CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "")
    CELERY_ALWAYS_EAGER = os.getenv("CELERY_ALWAYS_EAGER", "true").lower() == "true"
    CELERY_TASK_ALWAYS_EAGER = CELERY_ALWAYS_EAGER or not bool(CELERY_BROKER_URL)
    CELERY_TASK_EAGER_PROPAGATES = True
    DEFAULT_CARD_STYLE = "basic"
    DEFAULT_DIFFICULTY = "intermediate"
    DEFAULT_TARGET_CARDS = 30
