"""Background deck generation.

Generation runs on a daemon thread inside the web process, so the request that starts
it returns immediately and the status page can poll the live trace. All run state lives
in the database, so any web worker can serve progress for a run another one started.
Tests set GENERATION_IN_THREAD=false to run the pipeline inline.
"""

import logging
import threading

from flask import current_app

logger = logging.getLogger(__name__)


def dispatch_generation(deck_id, resume_from_plan=False):
    """Start (or resume) a generation run without blocking the request."""
    app = current_app._get_current_object()
    from .services.pipeline import generate_deck

    if not app.config.get("GENERATION_IN_THREAD", True):
        with app.app_context():
            generate_deck(deck_id, resume_from_plan=resume_from_plan)
        return

    def run():
        with app.app_context():
            try:
                generate_deck(deck_id, resume_from_plan=resume_from_plan)
            except Exception:  # generate_deck already marks the deck failed
                logger.exception("Background generation crashed for deck %s", deck_id)

    threading.Thread(target=run, name=f"ankigpt-gen-{deck_id}", daemon=True).start()
