import os


def _env_bool(name, default):
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# Sentinel default secret. The app refuses to start in production if this is still in use.
DEV_SECRET_KEY = "dev-secret"

# GPT-5.6 Luna: 1M context, structured outputs, tools, vision, cheap. Every pipeline
# role defaults to it; override a single role with OPENROUTER_MODEL_<ROLE>.
DEFAULT_MODEL = "openai/gpt-5.6-luna"
DEFAULT_EMBEDDING_MODEL = "openai/text-embedding-3-small"


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", DEV_SECRET_KEY)
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", "sqlite:///instance/ankigpt.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
    OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL)
    # Per-role overrides. Empty -> OPENROUTER_MODEL. Roles: mapper, planner, worker,
    # critic, reconcile, vision. The planner benefits most from a stronger model.
    OPENROUTER_MODEL_MAPPER = os.getenv("OPENROUTER_MODEL_MAPPER", "")
    OPENROUTER_MODEL_PLANNER = os.getenv("OPENROUTER_MODEL_PLANNER", "")
    OPENROUTER_MODEL_WORKER = os.getenv("OPENROUTER_MODEL_WORKER", "")
    OPENROUTER_MODEL_CRITIC = os.getenv("OPENROUTER_MODEL_CRITIC", "")
    OPENROUTER_MODEL_RECONCILE = os.getenv("OPENROUTER_MODEL_RECONCILE", "")
    OPENROUTER_MODEL_VISION = os.getenv("OPENROUTER_MODEL_VISION", "")
    OPENROUTER_EMBEDDING_MODEL = os.getenv("OPENROUTER_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)
    # Reasoning effort per role (sent as OpenRouter's normalized `reasoning.effort`).
    # Empty disables the parameter for models that don't support it.
    OPENROUTER_REASONING_PLANNER = os.getenv("OPENROUTER_REASONING_PLANNER", "medium")
    OPENROUTER_REASONING_MAPPER = os.getenv("OPENROUTER_REASONING_MAPPER", "low")
    OPENROUTER_REASONING_WORKER = os.getenv("OPENROUTER_REASONING_WORKER", "low")
    OPENROUTER_REASONING_CRITIC = os.getenv("OPENROUTER_REASONING_CRITIC", "low")
    OPENROUTER_REASONING_RECONCILE = os.getenv("OPENROUTER_REASONING_RECONCILE", "low")
    OPENROUTER_REASONING_VISION = os.getenv("OPENROUTER_REASONING_VISION", "low")
    # Optional sampling temperature. Left unset by default: reasoning models reject it.
    OPENROUTER_TEMPERATURE = os.getenv("OPENROUTER_TEMPERATURE", "")
    OPENROUTER_SITE_URL = os.getenv("OPENROUTER_SITE_URL", "")
    OPENROUTER_APP_NAME = os.getenv("OPENROUTER_APP_NAME", "AnkiGPT")
    OPENROUTER_TIMEOUT_SECONDS = float(os.getenv("OPENROUTER_TIMEOUT_SECONDS", "180"))
    OPENROUTER_MAX_RETRIES = _env_int("OPENROUTER_MAX_RETRIES", 2)
    OPENROUTER_RETRY_BACKOFF_SECONDS = float(os.getenv("OPENROUTER_RETRY_BACKOFF_SECONDS", "1.5"))
    # Output cap per call. Luna allows 128k completion tokens; a dense unit can need
    # 10k+ tokens of cards, and billing is per token used, so a high cap is free.
    OPENROUTER_MAX_TOKENS = _env_int("OPENROUTER_MAX_TOKENS", 16000)

    # Pipeline knobs.
    PIPELINE_MAX_WORKERS = _env_int("PIPELINE_MAX_WORKERS", 6)  # concurrent LLM calls
    PIPELINE_PLANNER_MAX_TURNS = _env_int("PIPELINE_PLANNER_MAX_TURNS", 14)
    PIPELINE_UNIT_MAX_CHARS = _env_int("PIPELINE_UNIT_MAX_CHARS", 14000)  # split larger units
    PIPELINE_CRITIC_ENABLED = _env_bool("PIPELINE_CRITIC_ENABLED", True)
    PIPELINE_COVERAGE_ENABLED = _env_bool("PIPELINE_COVERAGE_ENABLED", True)
    PIPELINE_EMBED_DEDUPE_ENABLED = _env_bool("PIPELINE_EMBED_DEDUPE_ENABLED", True)
    PIPELINE_CACHE_ENABLED = _env_bool("PIPELINE_CACHE_ENABLED", True)
    PIPELINE_FIGURES_ENABLED = _env_bool("PIPELINE_FIGURES_ENABLED", True)
    PIPELINE_MAX_FIGURES = _env_int("PIPELINE_MAX_FIGURES", 24)
    PIPELINE_DEDUPE_THRESHOLD = float(os.getenv("PIPELINE_DEDUPE_THRESHOLD", "0.90"))

    # Uploads. 50 MB is plenty for study PDFs; a multi-hundred-MB cap is a trivial
    # disk-exhaustion DoS vector since files are written before validation.
    UPLOAD_MAX_MB = _env_int("UPLOAD_MAX_MB", 50)
    MAX_CONTENT_LENGTH = UPLOAD_MAX_MB * 1024 * 1024
    UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", "instance/uploads")
    ALLOWED_UPLOAD_EXTENSIONS = {"pdf"}

    # Guard against pasting an entire book: bounds LLM cost/time. 0 disables the cap.
    MAX_SOURCE_CHARS = _env_int("MAX_SOURCE_CHARS", 400000)

    # Generation runs on a background thread inside the web process so the live trace
    # is visible while it builds. Tests turn this off to run inline.
    GENERATION_IN_THREAD = _env_bool("GENERATION_IN_THREAD", True)

    # Session/cookie hardening. SECURE is opt-in so local HTTP development still works;
    # set SESSION_COOKIE_SECURE=true behind HTTPS in production.
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE", False)
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"
    WTF_CSRF_TIME_LIMIT = None

    DEFAULT_CARD_STYLE = "basic"
