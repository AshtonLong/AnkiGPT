"""Agentic card-generation pipeline.

    map -> (cheat sheet) -> plan -> figures -> write -> critique -> reconcile -> coverage -> finish

See `orchestrator.py` for the run, `planner.py` for the agent that decides how the
material is carved up, and `strategies.py` for the card grammars workers write with.
`cheatsheet.py` is the opt-in step that condenses the source before any of that.
"""

from .orchestrator import format_generation_error, generate_deck, progress_for  # noqa: F401
