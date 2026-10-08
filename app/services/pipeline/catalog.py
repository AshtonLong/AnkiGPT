"""The models a user can pick, and the reasoning effort each one takes.

Every model here is called through OpenRouter, under its OpenRouter id, with the user's
one OpenRouter key. The server's OPENROUTER_MODEL is the default; under AI model on the
profile page (the Settings page on desktop) a user can pick another, which then runs
every agent of the pipeline. Only a pick that differs from the default is stored, so an
account that never chose keeps following the server, per-role overrides included.

Like routing, this module is free of Flask/DB state.
"""

from dataclasses import dataclass

# Every value OpenRouter takes for `reasoning.effort`, lowest first. "none" turns
# reasoning off. A model takes only some of them: see `Model.efforts`.
EFFORT_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

# Who makes the models, in the order the picker lists them. The key names the logo
# (see `company_logo` in templates/partials/ui.html).
COMPANIES = {"openai": "OpenAI", "anthropic": "Anthropic"}


@dataclass(frozen=True)
class Model:
    id: str  # OpenRouter's id, sent as is
    company: str
    name: str
    blurb: str
    # From `reasoning.supported_efforts` in OpenRouter's model list
    # (https://openrouter.ai/api/v1/models), lowest first. Copy it from there when
    # adding a model: sending a level a model lacks is not something to guess at.
    efforts: tuple


MODELS = (
    Model(
        "openai/gpt-6-luna", "openai", "GPT-6 Luna",
        "The fast, low-cost model of OpenAI's GPT-6 series.",
        ("none", "low", "medium", "high", "xhigh", "max"),
    ),
    Model(
        "anthropic/claude-haiku-5.5", "anthropic", "Claude Haiku 5.5",
        "Anthropic's small, fast model for high-volume work.",
        ("low", "medium", "high", "xhigh", "max"),
    ),
)
_BY_ID = {model.id: model for model in MODELS}


def find(model_id):
    """The catalog's entry for an OpenRouter id, or None for a model it does not list."""
    return _BY_ID.get(model_id)


def efforts_for(model_id):
    """The effort levels a model takes, lowest first.

    A model the catalog does not list (one set by the server's configuration) is offered
    every level, since nothing is known about it; OpenRouter decides what to make of each.
    """
    model = find(model_id)
    return model.efforts if model else EFFORT_LEVELS


def fit_effort(effort, model_id):
    """The level to send `model_id` for a wanted `effort`.

    A level the model lacks becomes the next one up that it has, or its highest when
    there is none above. So an effort saved under one model still means something after
    a switch to another, and reasoning is never turned off unless "none" was asked for.
    """
    if not effort:
        return None
    levels = efforts_for(model_id)
    if effort in levels or effort not in EFFORT_LEVELS:
        return effort
    rank = EFFORT_LEVELS.index(effort)
    above = [level for level in levels if EFFORT_LEVELS.index(level) > rank]
    return above[0] if above else levels[-1]


# ------------------------------------------------------------------ a user's pick
def _default_id(config):
    return config.get("OPENROUTER_MODEL") or ""


def user_model(user):
    """The model a user picked, or None while they follow the server's default.

    A saved id the catalog no longer lists is dropped, so a retired model falls back
    to the default instead of failing every run.
    """
    saved = getattr(user, "openrouter_model", None)
    return saved if saved in _BY_ID else None


def set_user_model(user, model_id, config):
    """Store the user's pick. Returns False for an id the picker never offers."""
    if model_id == _default_id(config):
        user.openrouter_model = None
    elif model_id in _BY_ID:
        user.openrouter_model = model_id
    else:
        return False
    return True


def active_model(user, config):
    """What the picker shows as chosen: {"id", "name", "company"} for the model the
    user's runs use."""
    model_id = user_model(user) or _default_id(config)
    model = find(model_id)
    if model:
        return {"id": model.id, "name": model.name, "company": model.company}
    return {"id": model_id, "name": model_id, "company": ""}


def model_groups(user, config):
    """The picker's options, grouped by company: [{"company", "name", "models"}].

    A server default the catalog does not list comes first, in a group of its own.
    """
    default_id = _default_id(config)
    chosen = active_model(user, config)["id"]

    def option(model_id, name, blurb):
        return {"id": model_id, "name": name, "blurb": blurb,
                "is_default": model_id == default_id, "checked": model_id == chosen}

    groups = []
    if default_id and default_id not in _BY_ID:
        groups.append({"company": "", "name": "Set for this app", "models": [
            option(default_id, default_id, "The model this app is configured to use."),
        ]})
    for company, name in COMPANIES.items():
        models = [option(m.id, m.name, m.blurb) for m in MODELS if m.company == company]
        if models:
            groups.append({"company": company, "name": name, "models": models})
    return groups
