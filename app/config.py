import os


def _env_bool(name, default):
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# Sentinel default secret. The app refuses to start in production if this is still in use.
DEV_SECRET_KEY = "dev-secret"


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", DEV_SECRET_KEY)
    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///instance/ankigpt.db")
    SQLALCHEMY_DATABASE_URI = DATABASE_URL
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
    # Default to a current, *stable* model. Avoid shipping a "-preview" slug as the
    # default since preview IDs can be deprecated/removed without notice. Override
    # with OPENROUTER_MODEL to use a newer/cheaper model.
    OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-3.5-flash")
    OPENROUTER_SITE_URL = os.getenv("OPENROUTER_SITE_URL", "")
    OPENROUTER_APP_NAME = os.getenv("OPENROUTER_APP_NAME", "AnkiGPT")
    OPENROUTER_TIMEOUT_SECONDS = float(os.getenv("OPENROUTER_TIMEOUT_SECONDS", "120"))
    OPENROUTER_MAX_RETRIES = int(os.getenv("OPENROUTER_MAX_RETRIES", "2"))
    OPENROUTER_RETRY_BACKOFF_SECONDS = float(os.getenv("OPENROUTER_RETRY_BACKOFF_SECONDS", "1.5"))
    OPENROUTER_MAX_TOKENS = int(os.getenv("OPENROUTER_MAX_TOKENS", "4000"))

    AUTH_REQUIRED = _env_bool("AUTH_REQUIRED", True)

    # Uploads. 50 MB is plenty for study PDFs; a multi-hundred-MB cap is a trivial
    # disk-exhaustion DoS vector since files are written before validation.
    UPLOAD_MAX_MB = int(os.getenv("UPLOAD_MAX_MB", "50"))
    MAX_CONTENT_LENGTH = UPLOAD_MAX_MB * 1024 * 1024
    UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", "instance/uploads")
    EXPORT_FOLDER = os.getenv("EXPORT_FOLDER", "instance/exports")
    ALLOWED_UPLOAD_EXTENSIONS = {"pdf"}

    # Guard against pasting an entire book: bounds LLM cost/time. 0 disables the cap.
    MAX_SOURCE_CHARS = int(os.getenv("MAX_SOURCE_CHARS", "200000"))

    CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "")
    CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "")
    CELERY_ALWAYS_EAGER = _env_bool("CELERY_ALWAYS_EAGER", True)
    CELERY_TASK_ALWAYS_EAGER = CELERY_ALWAYS_EAGER or not bool(CELERY_BROKER_URL)
    CELERY_TASK_EAGER_PROPAGATES = True

    # Session/cookie hardening. SECURE is opt-in so local HTTP development still works;
    # set SESSION_COOKIE_SECURE=true behind HTTPS in production.
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE", False)
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"
    WTF_CSRF_TIME_LIMIT = None

    DEFAULT_CARD_STYLE = "basic"
    DEFAULT_DIFFICULTY = "intermediate"
    DEFAULT_TARGET_CARDS = 30
