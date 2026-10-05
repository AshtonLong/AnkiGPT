"""Per-user OpenRouter API keys.

AnkiGPT is free: every account brings its own OpenRouter key and pays OpenRouter
directly for what it generates. A key is encrypted with a key derived from SECRET_KEY
before it reaches the database, so a copy of the database alone does not expose it.
Changing SECRET_KEY makes stored keys unreadable, and users then enter theirs again.
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app

KEY_PREFIX = "sk-or-"
KEY_MAX_LENGTH = 200


def _fernet():
    secret = str(current_app.config["SECRET_KEY"]).encode()
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"openrouter-api-key:" + secret).digest()))


def key_problem(key):
    """Why `key` can't be an OpenRouter key, or None when it looks like one."""
    if not key:
        return "Paste your OpenRouter API key."
    if len(key) > KEY_MAX_LENGTH or any(ch.isspace() for ch in key) or not key.startswith(KEY_PREFIX):
        return f"That doesn't look like an OpenRouter key. Keys start with {KEY_PREFIX} and have no spaces."
    return None


def set_user_key(user, key):
    """Store `key` for the user, or clear the stored one when `key` is empty."""
    key = (key or "").strip()
    user.openrouter_key_encrypted = _fernet().encrypt(key.encode()).decode() if key else None
    user.openrouter_key_hint = key[-4:] if key else None


def user_key(user):
    """The user's own key, or "" when none is saved or it can no longer be decrypted."""
    token = getattr(user, "openrouter_key_encrypted", None)
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        # Saved under a different SECRET_KEY. The profile page asks the user to enter it again.
        return ""


def openrouter_key_for(user):
    """The key a user's AI calls are made with: their own, else the server's fallback."""
    return user_key(user) or current_app.config.get("OPENROUTER_API_KEY") or ""
