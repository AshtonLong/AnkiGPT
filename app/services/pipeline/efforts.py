"""Per-user reasoning effort for each agent of the pipeline.

The server sets a default effort per role (OPENROUTER_REASONING_<ROLE>). Under Advanced
on the profile page (the Settings page on desktop) a user can move any single agent off
that default. Only the agents they moved are stored, so every other agent keeps following
the server's defaults, including ones that change later.
"""

from .routing import AGENT_ROLES, EFFORT_LEVELS

EFFORT_LABELS = {"minimal": "Minimal", "low": "Low", "medium": "Medium", "high": "High", "xhigh": "Extra high"}

# What the Advanced panel shows: (group, note, ((agent, name, what it does), ...)), in the
# order a run reaches them.
AGENT_GROUPS = (
    ("Reading and planning", "Before any card is written", (
        ("mapper", "Outliner", "Splits your source into study units."),
        ("cheatsheet", "Cheat sheet writer", "Boils each unit down when “Make a cheat sheet first” is on."),
        ("planner", "Planner", "Decides what to cover and hands out the writing tasks."),
        ("vision", "Figure reader", "Reads the figures found in a PDF."),
    )),
    ("Writing", "The cards themselves", (
        ("worker", "Card writers", "Write the cards for each task in the plan."),
    )),
    ("Review agents", "Every check a card goes through afterwards", (
        ("cold_reader", "Cold reader", "Answers each card without the source, to catch cards that give their answer away."),
        ("judge", "Judge", "Rules keep, rewrite or drop on each card against the source."),
        ("merger", "Duplicate resolver", "Picks the card to keep when several test the same fact."),
        ("coverage", "Coverage auditor", "Looks for testable facts that no card covers yet."),
        ("improver", "Card improver", "Rewrites a single card when you choose AI improve."),
        ("coach", "Coach", "Diagnoses and rewrites the cards you keep getting wrong in Anki."),
    )),
)


def user_efforts(user):
    """The efforts a user has set, as {agent: level}. Anything unrecognised is dropped."""
    saved = getattr(user, "agent_efforts_json", None)
    if not isinstance(saved, dict):
        return {}
    return {agent: effort for agent, effort in saved.items() if agent in AGENT_ROLES and effort in EFFORT_LEVELS}


def set_user_efforts(user, efforts):
    """Store the user's efforts, or clear them all when `efforts` is empty."""
    user.agent_efforts_json = dict(efforts) or None


def efforts_from_form(form):
    """Read the Advanced form's sliders into {agent: level}.

    Each slider runs from 0 (follow the default, which stores nothing) to one stop per
    effort level. Returns None when a slider holds a value the page never offers.
    """
    efforts = {}
    for agent in AGENT_ROLES:
        try:
            stop = int(form.get(f"effort_{agent}", "0"))
        except ValueError:
            return None
        if not 0 <= stop <= len(EFFORT_LEVELS):
            return None
        if stop:
            efforts[agent] = EFFORT_LEVELS[stop - 1]
    return efforts


def effort_groups(user, config):
    """The Advanced panel's sliders: each agent's saved stop and the default it otherwise follows."""
    saved = user_efforts(user)
    groups = []
    for title, note, agents in AGENT_GROUPS:
        rows = []
        for agent, name, does in agents:
            default = (config.get(f"OPENROUTER_REASONING_{AGENT_ROLES[agent].upper()}") or "").strip()
            # An empty default sends no effort at all, which leaves it to the model.
            default_label = "Default · " + EFFORT_LABELS.get(default, default) if default else "Default"
            effort = saved.get(agent)
            rows.append({
                "key": agent, "name": name, "does": does, "default_label": default_label,
                "stop": EFFORT_LEVELS.index(effort) + 1 if effort else 0,
                "label": EFFORT_LABELS[effort] if effort else default_label,
            })
        groups.append({"title": title, "note": note, "agents": rows})
    return groups
