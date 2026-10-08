"""The model picker: the catalog, the AI model form, what a run calls, and how the
reasoning effort follows the model."""

import pytest

from app.extensions import db as _db
from app.models import Deck, User
from app.services import deckgen
from app.services import llm as llm_module
from app.services.pipeline.catalog import (
    COMPANIES, EFFORT_LEVELS, MODELS, efforts_for, find, fit_effort, user_model,
)
from app.services.pipeline.efforts import EFFORT_LABELS
from app.services.pipeline.routing import LLMClient, ROLES

from conftest import FakeLLM, fake_embeddings, fake_response, register

LUNA = "openai/gpt-6-luna"
HAIKU = "anthropic/claude-haiku-5.5"
SOURCE = """# Photosynthesis

Photosynthesis converts CO2 and water into glucose using light. Water is split during
the light reactions, releasing oxygen.

# Respiration

Cellular respiration oxidises glucose to release energy stored as ATP. It happens in the
mitochondria of eukaryotic cells and consumes oxygen."""


def _pick(client, model):
    return client.post("/auth/profile", data={"section": "model", "model": model})


def _user(app, email="a@example.com"):
    return User.query.filter_by(email=email).one()


def _page(client):
    return client.get("/auth/profile").get_data(as_text=True)


# ------------------------------------------------------------------ the catalog
def test_every_model_names_a_listed_company_and_levels_openrouter_takes():
    assert len({model.id for model in MODELS}) == len(MODELS)
    for model in MODELS:
        assert model.company in COMPANIES, model.id
        assert model.efforts and set(model.efforts) <= set(EFFORT_LEVELS), model.id
        # Lowest first, which the sliders and `fit_effort` both rely on.
        assert list(model.efforts) == sorted(model.efforts, key=EFFORT_LEVELS.index), model.id
    assert set(EFFORT_LABELS) == set(EFFORT_LEVELS)


def test_claude_haiku_is_listed_under_anthropic_with_its_own_levels():
    haiku = find(HAIKU)
    assert haiku.company == "anthropic" and haiku.name == "Claude Haiku 5.5"
    # It cannot have reasoning turned off, and neither model takes "minimal".
    assert haiku.efforts == ("low", "medium", "high", "xhigh", "max")
    assert find(LUNA).efforts == ("none", "low", "medium", "high", "xhigh", "max")


@pytest.mark.parametrize("effort, model, sent", [
    ("high", HAIKU, "high"), ("max", HAIKU, "max"),
    # A level the model lacks becomes the next one up, and never turns reasoning off.
    ("none", HAIKU, "low"), ("minimal", HAIKU, "low"), ("minimal", LUNA, "low"), ("none", LUNA, "none"),
    # Nothing is known about an unlisted model, so it is sent what was asked for.
    ("minimal", "vendor/unlisted", "minimal"), ("max", "vendor/unlisted", "max"),
    ("", HAIKU, None), (None, LUNA, None),
])
def test_an_effort_is_fitted_to_the_levels_the_model_takes(effort, model, sent):
    assert fit_effort(effort, model) == sent


def test_an_unlisted_model_is_offered_every_level():
    assert efforts_for("vendor/unlisted") == EFFORT_LEVELS


# ------------------------------------------------------------------ routing
def test_a_picked_model_runs_every_role_and_the_server_routes_without_one():
    config = {"OPENROUTER_MODEL": LUNA, "OPENROUTER_MODEL_PLANNER": "vendor/strong-planner"}
    follows = LLMClient(config)
    assert follows.model_for("planner") == "vendor/strong-planner" and follows.model_for("worker") == LUNA

    picked = LLMClient(config, model=HAIKU)
    assert {picked.model_for(role) for role in ROLES} == {HAIKU} and picked.default_model == HAIKU


def test_a_call_sends_a_level_the_model_takes(monkeypatch):
    sent = []
    monkeypatch.setattr(llm_module, "openrouter_chat",
                        lambda messages, model, *args, **kwargs: sent.append((model, kwargs["reasoning_effort"]))
                        or fake_response("ok"))
    config = {"OPENROUTER_REASONING_CRITIC": "minimal", "OPENROUTER_REASONING_PLANNER": "medium"}
    efforts = {"judge": "none", "coach": "max"}
    for model in (None, HAIKU):
        client = LLMClient(config, "sk-or-test", efforts, model)
        client.chat("critic", [], agent="judge")
        client.chat("critic", [], agent="coach")
        client.chat("critic", [], agent="cold_reader")
        client.chat("planner", [])
    assert sent == [
        (LUNA, "none"), (LUNA, "max"), (LUNA, "low"), (LUNA, "medium"),
        (HAIKU, "low"), (HAIKU, "max"), (HAIKU, "low"), (HAIKU, "medium"),
    ]


def test_cached_results_are_shared_only_between_efforts_sent_as_the_same_level():
    config = {"OPENROUTER_REASONING_CRITIC": "low", "OPENROUTER_REASONING_PLANNER": "medium"}
    # Haiku takes "none" as "low", which is the critic's default: nothing moved.
    assert LLMClient(config, efforts={"judge": "none"}, model=HAIKU).effort_key("judge") == ()
    assert LLMClient(config, efforts={"judge": "none"}).effort_key("judge") == ("effort:judge=none",)
    # An agent that is its own role is still told apart from that role's default.
    assert LLMClient(config, efforts={"planner": "high"}).effort_key("planner") == ("effort:planner=high",)
    assert LLMClient(config, efforts={"planner": "medium"}).effort_key("planner") == ()


# ------------------------------------------------------------------ the AI model form
def test_the_picker_lists_each_model_under_its_company_with_the_logo(client):
    register(client)
    html = _page(client)
    assert 'id="model"' in html and 'href="#model"' in html
    openai, anthropic = html.index("<legend>OpenAI</legend>"), html.index("<legend>Anthropic</legend>")
    assert openai < html.index(f'value="{LUNA}"') < anthropic < html.index(f'value="{HAIKU}"')
    # Each company's mark is drawn beside its model.
    assert 'class="model-logo model-logo-openai"><svg' in html
    assert 'class="model-logo model-logo-anthropic"><svg' in html
    assert "M17.3041 3.541h-3.6718l6.696 16.918H24Z" in html and "M22.2819 9.8211a5.9847" in html
    # The server's model is the one chosen until the user picks.
    assert f'name="model" value="{LUNA}" checked' in html and f'name="model" value="{HAIKU}" checked' not in html
    assert 'GPT-6 Luna <span class="model-default">Default</span>' in html
    assert f"<code>{HAIKU}</code>" in html


def test_a_pick_is_saved_and_the_default_is_stored_as_no_pick(client, app):
    register(client)
    response = _pick(client, HAIKU)
    assert response.status_code == 303 and response.location.endswith("/auth/profile#model")
    with app.app_context():
        assert _user(app).openrouter_model == HAIKU
    html = _page(client)
    assert "Claude Haiku 5.5 is now your model." in html
    assert f'name="model" value="{HAIKU}" checked' in html and f'name="model" value="{LUNA}" checked' not in html

    _pick(client, LUNA)
    with app.app_context():
        assert _user(app).openrouter_model is None


@pytest.mark.parametrize("bad", ["", "vendor/unlisted", "anthropic/claude-haiku-5.5:batch"])
def test_a_model_the_picker_never_offers_is_rejected_and_nothing_changes(client, app, bad):
    register(client)
    _pick(client, HAIKU)
    response = _pick(client, bad)
    assert response.status_code == 422 and b"Choose one of the models listed" in response.data
    with app.app_context():
        assert _user(app).openrouter_model == HAIKU


def test_a_pick_is_private_to_each_account(client, app):
    register(client)
    _pick(client, HAIKU)
    client.post("/auth/logout")
    register(client, email="b@example.com")
    assert f'name="model" value="{LUNA}" checked' in _page(client)
    with app.app_context():
        assert _user(app, "b@example.com").openrouter_model is None


def test_a_saved_model_the_catalog_no_longer_lists_falls_back_to_the_default(app):
    with app.app_context():
        user = User(email="x@example.com", openrouter_model="vendor/retired")
        assert user_model(user) is None
        assert LLMClient(app.config, model=user_model(user)).default_model == app.config["OPENROUTER_MODEL"]


def test_the_brief_names_the_model_the_run_will_use(client, app):
    register(client)
    client.post("/decks/new", data={"title": "Cells", "card_style": "basic", "source_type": "text", "text_input": SOURCE})
    assert "Written by <b>GPT-6 Luna</b>" in client.get("/decks/1/preview").get_data(as_text=True)
    _pick(client, HAIKU)
    html = client.get("/decks/1/preview").get_data(as_text=True)
    assert "Written by <b>Claude Haiku 5.5</b>" in html and 'class="model-logo model-logo-anthropic"><svg' in html
    assert 'href="/auth/profile#model"' in html


# ------------------------------------------------------------------ effort follows the model
def test_the_sliders_offer_the_levels_of_the_picked_model(client):
    register(client)
    html = _page(client)
    assert 'name="effort_scale" value="none|low|medium|high|xhigh|max"' in html
    assert "These are the levels <b>GPT-6 Luna</b> takes: Off, Low, Medium, High, Extra high, Max." in html
    assert '<button type="button" data-effort-all="1">Off</button>' in html

    _pick(client, HAIKU)
    html = _page(client)
    assert 'name="effort_scale" value="low|medium|high|xhigh|max"' in html
    assert 'data-effort-labels="Low|Medium|High|Extra high|Max"' in html
    assert "These are the levels <b>Claude Haiku 5.5</b> takes: Low, Medium, High, Extra high, Max." in html
    assert 'name="effort_judge" min="0" max="5" step="1" value="0"' in html
    assert ">Off</button>" not in html and ">Minimal</button>" not in html


def test_an_effort_saved_under_one_model_shows_as_the_nearest_level_of_another(client, app):
    register(client)
    # On Luna: stop 1 is Off, stop 6 is Max.
    client.post("/auth/profile", data={"section": "advanced", "effort_scale": "none|low|medium|high|xhigh|max",
                                       "effort_judge": "1", "effort_coach": "6", "effort_planner": "3"})
    with app.app_context():
        assert _user(app).agent_efforts_json == {"judge": "none", "coach": "max", "planner": "medium"}

    _pick(client, HAIKU)
    html = _page(client)
    # Haiku has no Off, so the judge shows Low; what was saved is kept for a switch back.
    assert 'name="effort_judge" min="0" max="5" step="1" value="1"' in html
    assert '<output for="effort_judge">Low</output>' in html
    assert '<output for="effort_coach">Max</output>' in html and '<output for="effort_planner">Medium</output>' in html
    with app.app_context():
        assert _user(app).agent_efforts_json["judge"] == "none"


def test_a_form_drawn_before_the_model_changed_saves_what_it_showed(client, app):
    register(client)
    _pick(client, HAIKU)
    # The page still open in another tab was drawn with Luna's six stops: 5 there is Extra high.
    response = client.post("/auth/profile", data={"section": "advanced", "effort_scale": "none|low|medium|high|xhigh|max",
                                                  "effort_judge": "5"})
    assert response.status_code == 303
    with app.app_context():
        assert _user(app).agent_efforts_json == {"judge": "xhigh"}
    # A form that names no scale is read against the user's model: Haiku's stop 5 is Max.
    client.post("/auth/profile", data={"section": "advanced", "effort_judge": "5"})
    with app.app_context():
        assert _user(app).agent_efforts_json == {"judge": "max"}


@pytest.mark.parametrize("scale", ["", "low|turbo", "low|low", "low|"])
def test_a_scale_the_page_never_sends_is_rejected(client, app, scale):
    register(client)
    response = client.post("/auth/profile", data={"section": "advanced", "effort_scale": scale, "effort_judge": "1"})
    assert response.status_code == 422 and b"Set each agent" in response.data
    with app.app_context():
        assert _user(app).agent_efforts_json is None


# ------------------------------------------------------------------ what a run calls
def test_a_run_calls_the_model_its_owner_picked_at_levels_that_model_takes(client, app, monkeypatch):
    register(client)
    app.config["OPENROUTER_API_KEY"] = "sk-or-server"
    client.post("/auth/profile", data={"section": "advanced", "effort_scale": "none|low|medium|high|xhigh|max",
                                       "effort_worker": "1", "effort_judge": "6"})
    _pick(client, HAIKU)

    fake, efforts = FakeLLM(), {}

    def chat(messages, model, api_key, *args, **kwargs):
        schema = ((kwargs.get("response_format") or {}).get("json_schema") or {}).get("name")
        efforts.setdefault("planner" if kwargs.get("tools") else schema, set()).add(kwargs.get("reasoning_effort"))
        return fake(messages, model, api_key, *args, **kwargs)

    monkeypatch.setattr(llm_module, "openrouter_chat", chat)
    monkeypatch.setattr(llm_module, "openrouter_embeddings", fake_embeddings)
    monkeypatch.setattr("app.services.pipeline.document_map.SINGLE_UNIT_MAX_CHARS", 10)
    with app.app_context():
        deck = Deck(user_id=_user(app).id, title="Cells", card_style="mixed", status="draft", source_type="text",
                    source_text=SOURCE, settings_json={}, run_json={})
        _db.session.add(deck)
        _db.session.commit()
        assert deckgen.generate_deck(deck.id) == deck.id
        assert deck.status == "ready" and deck.run_json["model"] == HAIKU

    assert {call["model"] for call in fake.calls} == {HAIKU}
    # The writers were set to Off, which Haiku lacks: they think at Low instead.
    assert efforts["anki_cards"] == {"low"} and efforts["critic_verdicts"] == {"max"}
    assert set().union(*efforts.values()) <= set(find(HAIKU).efforts)
