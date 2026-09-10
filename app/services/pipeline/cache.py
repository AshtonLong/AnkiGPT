"""Content-addressed result cache.

Keys are a SHA-256 over (role, model, prompt version, every input that changes the
output). Any task whose inputs are byte-identical to a previous run — same unit text,
same strategy, same notes — is served from the table instead of the model. DB access is
main-thread only; the parallel runner resolves hits before dispatching work.
"""

import hashlib
import json
import logging

from ...extensions import db
from ...models import GenerationCache

logger = logging.getLogger(__name__)


def make_key(*parts):
    h = hashlib.sha256()
    for part in parts:
        if not isinstance(part, str):
            part = json.dumps(part, sort_keys=True, ensure_ascii=False, default=str)
        h.update(part.encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


class DBCache:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.hits = 0
        self.misses = 0

    def get(self, key):
        if not self.enabled or not key:
            return None
        row = GenerationCache.query.filter_by(key=key).first()
        if row is None:
            self.misses += 1
            return None
        self.hits += 1
        try:
            row.hits = (row.hits or 0) + 1
            db.session.commit()
        except Exception:
            db.session.rollback()
        return {"value": row.value_json, "usage": row.usage_json or {}, "model": row.model}

    def put(self, key, role, model, value, usage=None):
        if not self.enabled or not key:
            return
        try:
            existing = GenerationCache.query.filter_by(key=key).first()
            if existing:
                existing.value_json = value
                existing.usage_json = usage or {}
            else:
                db.session.add(
                    GenerationCache(key=key, role=role, model=model, value_json=value, usage_json=usage or {})
                )
            db.session.commit()
        except Exception:
            logger.exception("Cache write failed for %s", key[:12])
            db.session.rollback()


class NullCache(DBCache):
    def __init__(self):
        super().__init__(enabled=False)
