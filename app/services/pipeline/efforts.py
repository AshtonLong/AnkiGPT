"""Per-user reasoning effort for each agent of the pipeline.

The server sets a default effort per role (OPENROUTER_REASONING_<ROLE>). Under Advanced
on the profile page (the Settings page on desktop) a user can move any single agent off
that default. Only the agents they moved are stored, so every other agent keeps following
the server's defaults, including ones that change later.

The sliders offer the levels the user's model takes (see pipeline.catalog), which differ
from model to model. A level saved under one model that another lacks is shown, and sent,
as the nearest level that model has.
"""

from .catalog import active_model, efforts_for, fit_effort
from .routing import AGENT_ROLES, EFFORT_LEVELS

EFFORT_LABELS = {
    "none": "Off", "minimal": "Minimal", "low": "Low", "medium": "Medium", "high": "High",
    "xhigh": "Extra high", "max": "Max",
}

# What the Advanced panel shows: (group, note, ((agent, name, what it does), ...)), in the
# order a run reaches them.
AGENT_GROUPS = (
    ("Reading and planning", "Before any card is written", (
        ("mapper", "Outliner", "Splits your source into study units."),
        ("vision", "Figure reader", "Reads the figures found in a PDF."),
        ("cheatsheet", "Cheat sheet writer", "Boils each unit down when “Make a cheat sheet first” is on."),
        ("planner", "Planner", "Decides what to cover, figures included, and hands out the writing tasks."),
    )),
    ("Writing", "The cards themselves", (
        ("worker", "Card writers", "Write the cards for each task in the plan."),
    )),
    ("Review agents", "Every check a card goes through afterwards", (
        ("cold_reader", "Cold reader", "Answers each card without the source, to catch cards that give their answer away."),
        ("judge", "Judge", "Rules keep, rewrite or drop on each card against the source."),
        ("coverage", "Coverage auditor", "Looks for testable facts that no card covers yet."),
        ("gatekeeper", "Back-fill reviewer", "Decides whether each card written for a coverage gap earns its place in the deck."),
        ("merger", "Duplicate resolver", "Picks the card to keep when several test the same fact."),
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


def efforts_from_form(form, levels):
    """Read the Advanced form's sliders into {agent: level}.

    Each slider runs from 0 (follow the default, which stores nothing) to one stop per
    level of the scale the page was drawn with. The form names that scale itself, so a
    page drawn before the user changed model still saves what it showed; `levels` is the
    scale to read a form without one against. Returns None when the scale or a slider
    holds a value the page never offers.
    """
    scale = form.get("effort_scale")
    if scale is not None:
        levels = tuple(scale.split("|"))
        if not levels or len(set(levels)) != len(levels) or not set(levels) <= set(EFFORT_LEVELS):
            return None
    efforts = {}
    for agent in AGENT_ROLES:
        try:
            stop = int(form.get(f"effort_{agent}", "0"))
        except ValueError:
            return None
        if not 0 <= stop <= len(levels):
            return None
        if stop:
            efforts[agent] = levels[stop - 1]
    return efforts


def effort_levels(user, config):
    """The levels the user's sliders offer: the ones their model takes, lowest first."""
    return efforts_for(active_model(user, config)["id"])


def effort_panel(user, config):
    """What the Advanced panel shows: the model's scale, and each agent's saved stop on
    it with the default it otherwise follows."""
    model = active_model(user, config)
    levels = efforts_for(model["id"])
    saved = user_efforts(user)
    groups = []
    for title, note, agents in AGENT_GROUPS:
        rows = []
        for agent, name, does in agents:
            default = (config.get(f"OPENROUTER_REASONING_{AGENT_ROLES[agent].upper()}") or "").strip()
            default = fit_effort(default, model["id"])
            # An empty default sends no effort at all, which leaves it to the model.
            default_label = "Default · " + EFFORT_LABELS.get(default, default) if default else "Default"
            effort = fit_effort(saved.get(agent), model["id"])
            rows.append({
                "key": agent, "name": name, "does": does, "default_label": default_label,
                "stop": levels.index(effort) + 1 if effort else 0,
                "label": EFFORT_LABELS[effort] if effort else default_label,
            })
        groups.append({"title": title, "note": note, "agents": rows})
    return {
        "effort_groups": groups, "effort_levels": levels,
        "effort_labels": [EFFORT_LABELS[level] for level in levels], "effort_model": model,
    }
