"""Agentic card-generation pipeline.

    map -> plan -> figures -> write -> critique -> reconcile -> coverage -> finish

See `orchestrator.py` for the run, `planner.py` for the agent that decides how the
material is carved up, and `strategies.py` for the card grammars workers write with.
"""

from .orchestrator import format_generation_error, generate_deck, progress_for  # noqa: F401
