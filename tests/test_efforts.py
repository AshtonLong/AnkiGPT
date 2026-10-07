"""Per-agent reasoning effort: routing, the Advanced form, and what a run sends."""

import pytest

from app.extensions import db as _db
from app.models import Card, Deck, User
from app.services import deckgen
from app.services import llm as llm_module
from app.services.pipeline.efforts import AGENT_GROUPS, user_efforts
from app.services.pipeline import reconcile
from app.services.pipeline.cache import make_key
from app.services.pipeline.feedback import coach_cards
from app.services.pipeline.routing import AGENT_ROLES, EFFORT_LEVELS, LLMClient, ROLES

from conftest import FakeLLM, fake_embeddings, fake_response, register

DEFAULTS = {
    "OPENROUTER_REASONING_PLANNER": "medium", "OPENROUTER_REASONING_CRITIC": "low",
    "OPENROUTER_REASONING_RECONCILE": "low", "OPENROUTER_REASONING_WORKER": "",
}
SOURCE = """# Photosynthesis

Photosynthesis converts CO2 and water into glucose using light. Water is split during
the light reactions, releasing oxygen.

# Respiration

Cellular respiration oxidises glucose to release energy stored as ATP. It happens in the
mitochondria of eukaryotic cells and consumes oxygen."""


def _save(client, **stops):
    return client.post("/auth/profile", data={"section": "advanced", **{f"effort_{a}": s for a, s in stops.items()}})


def _saved(app, email="a@example.com"):
    with app.app_context():
        return User.query.filter_by(email=email).one().agent_efforts_json


# ------------------------------------------------------------------ routing
def test_every_agent_runs_as_a_role_and_has_a_slider():
    assert set(AGENT_ROLES.values()) == set(ROLES)
    shown = [agent for _title, _note, agents in AGENT_GROUPS for agent, _name, _does in agents]
    assert sorted(shown) == sorted(AGENT_ROLES)


def test_an_agent_follows_its_role_until_the_user_sets_it():
    client = LLMClient(DEFAULTS)
    assert client.reasoning_for("planner") == "medium"
    assert {client.reasoning_for(a) for a in ("cold_reader", "judge", "gatekeeper", "improver", "coach", "merger", "coverage")} == {"low"}
    assert client.reasoning_for("worker") is None
    # A caller that names only the role still gets the role's effort.
    assert client.reasoning_for("critic") == "low"

    client = LLMClient(DEFAULTS, efforts={"judge": "high", "worker": "minimal"})
    assert client.reasoning_for("judge") == "high" and client.reasoning_for("worker") == "minimal"
    assert client.reasoning_for("cold_reader") == "low" and client.reasoning_for("planner") == "medium"


def test_unknown_agents_and_levels_are_ignored():
    client = LLMClient(DEFAULTS, efforts={"judge": "extreme", "nobody": "high", "critic": "high", "coach": "xhigh"})
    assert client.efforts == {"coach": "xhigh"}


def test_a_call_sends_the_calling_agents_effort(monkeypatch):
    sent = []
    monkeypatch.setattr(llm_module, "openrouter_chat",
                        lambda *args, **kwargs: sent.append(kwargs["reasoning_effort"]) or fake_response("ok"))
    client = LLMClient(DEFAULTS, "sk-or-test", {"judge": "high"})
    client.chat("critic", [], agent="judge")
    client.chat("critic", [], agent="cold_reader")
    client.chat("critic", [])
    client.chat("worker", [])
    client.chat("critic", [], agent="judge", reasoning_effort="minimal")
    assert sent == ["high", "low", "low", None, "minimal"]


def test_cached_results_are_kept_apart_by_effort():
    default = LLMClient(DEFAULTS)
    assert default.effort_key("cold_reader", "judge") == ()
    # Setting an agent to the effort it already had changes nothing, so it shares the cache.
    assert LLMClient(DEFAULTS, efforts={"judge": "low"}).effort_key("cold_reader", "judge") == ()

    moved = LLMClient(DEFAULTS, efforts={"judge": "high", "coverage": "xhigh"})
    assert moved.effort_key("cold_reader", "judge") == ("effort:judge=high",)
    assert moved.effort_key("worker") == ()
    keys = {make_key("critic", "m", "v1", "cards", *client.effort_key("cold_reader", "judge"))
            for client in (default, moved, LLMClient(DEFAULTS, efforts={"judge": "xhigh"}))}
    assert len(keys) == 3


# ------------------------------------------------------------ the Advanced form
def test_profile_has_a_slider_for_every_agent_on_its_default(client):
    register(client)
    html = client.get("/auth/profile").get_data(as_text=True)
    assert 'id="advanced"' in html and 'href="#advanced"' in html
    for agent in AGENT_ROLES:
        assert f'name="effort_{agent}" min="0" max="{len(EFFORT_LEVELS)}" step="1" value="0"' in html, agent
    # The planner's default is medium, the judge's low.
    assert 'data-default-label="Default · Medium" aria-describedby="effort_planner-hint"' in html
    assert 'data-default-label="Default · Low" aria-describedby="effort_judge-hint"' in html
    for name in ("Cold reader", "Judge", "Duplicate resolver", "Coverage auditor", "Card improver", "Coach"):
        assert f">{name}</label>" in html


def test_only_the_agents_moved_off_default_are_saved(client, app):
    register(client)
    response = _save(client, judge=4, coverage=5, worker=0, cold_reader=1)
    assert response.status_code == 303 and response.location.endswith("/auth/profile#advanced")
    assert _saved(app) == {"cold_reader": "minimal", "judge": "high", "coverage": "xhigh"}

    html = client.get("/auth/profile").get_data(as_text=True)
    assert "Your effort settings are saved." in html
    assert 'name="effort_judge" min="0" max="5" step="1" value="4"' in html
    assert '<output for="effort_judge">High</output>' in html
    assert '<output for="effort_coverage">Extra high</output>' in html
    assert '<output for="effort_worker">Default · Low</output>' in html

    # Saving again replaces the whole set: the judge goes back to its default.
    _save(client, coverage=2)
    assert _saved(app) == {"coverage": "low"}


def test_reset_puts_every_agent_back_on_its_default(client, app):
    register(client)
    _save(client, judge=4, planner=5)
    response = client.post("/auth/profile", data={"section": "advanced", "action": "reset", "effort_judge": "3"},
                           follow_redirects=True)
    assert b"Every agent is back on its default effort." in response.data
    assert _saved(app) is None


@pytest.mark.parametrize("bad", ["6", "-1", "high", "", "1.5"])
def test_a_value_the_sliders_never_offer_is_rejected_and_nothing_changes(client, app, bad):
    register(client)
    _save(client, judge=4)
    response = _save(client, judge=bad, planner=5)
    assert response.status_code == 422 and b"Set each agent" in response.data
    assert _saved(app) == {"judge": "high"}


def test_efforts_are_private_to_each_account(client, app):
    register(client)
    _save(client, judge=5)
    client.post("/auth/logout")
    register(client, email="b@example.com")
    assert 'name="effort_judge" min="0" max="5" step="1" value="0"' in client.get("/auth/profile").get_data(as_text=True)
    assert _saved(app, "b@example.com") is None


def test_stored_values_that_no_longer_mean_anything_are_dropped(app):
    with app.app_context():
        user = User(email="x@example.com", agent_efforts_json={"judge": "high", "retired_agent": "low", "coach": "turbo"})
        assert user_efforts(user) == {"judge": "high"}
        user.agent_efforts_json = ["judge"]
        assert user_efforts(user) == {}


# ------------------------------------------------------------------ what a run sends
def test_a_run_sends_each_agent_the_effort_its_owner_chose(client, app, monkeypatch):
    register(client)
    app.config["OPENROUTER_API_KEY"] = "sk-or-server"
    _save(client, mapper=1, planner=5, worker=3, cold_reader=1, judge=4, gatekeeper=2, merger=3, coverage=5, improver=2,
          coach=4)

    fake, sent = FakeLLM(), {}

    def chat(messages, model, api_key, *args, **kwargs):
        schema = ((kwargs.get("response_format") or {}).get("json_schema") or {}).get("name")
        sent.setdefault("planner" if kwargs.get("tools") else schema, set()).add(kwargs.get("reasoning_effort"))
        return fake(messages, model, api_key, *args, **kwargs)

    monkeypatch.setattr(llm_module, "openrouter_chat", chat)
    monkeypatch.setattr(llm_module, "openrouter_embeddings", fake_embeddings)
    monkeypatch.setattr("app.services.pipeline.document_map.SINGLE_UNIT_MAX_CHARS", 10)
    with app.app_context():
        user = User.query.one()
        deck = Deck(user_id=user.id, title="Cells", card_style="mixed", status="draft", source_type="text",
                    source_text=SOURCE, settings_json={}, run_json={})
        _db.session.add(deck)
        _db.session.commit()
        deck_id = deck.id
        assert deckgen.generate_deck(deck_id) == deck_id
        card = Card.query.filter_by(deck_id=deck_id, type="basic", status="ok").first()
        deckgen.improve_card(card.id)
        coach_cards(deck_id, [card.id])
        # The scripted run has no near-duplicates to resolve, so that agent is called directly.
        twins = [{"type": "basic", "front": "What does photosynthesis make?", "back": "Glucose"}] * 2
        reconcile.resolve_clusters(LLMClient(app.config, "sk-or-server", user_efforts(user)), [[0, 1]], twins)

    assert sent == {
        "document_map": {"minimal"}, "planner": {"xhigh"}, "anki_cards": {"medium"},
        "cold_answers": {"minimal"}, "critic_verdicts": {"high"}, "duplicate_resolution": {"medium"},
        "coverage_audit": {"xhigh"}, "backfill_review": {"low"}, "improved_basic_card": {"low"},
        "diagnosed_cards": {"high"},
    }
