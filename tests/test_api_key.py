"""Bring-your-own OpenRouter key: storage, the profile form, and who pays for a run."""

import sqlite3

import pytest

from app.extensions import db as _db
from app.models import Card, Deck, User
from app.routes import main as main_routes
from app.services import deckgen
from app.services import llm as llm_module
from app.services.credentials import openrouter_key_for, set_user_key, user_key

from conftest import FakeLLM, fake_embeddings, register

KEY = "sk-or-v1-" + "a1b2c3d4" * 8
OTHER_KEY = "sk-or-v1-" + "9f8e7d6c" * 8


def _save_key(client, key=KEY, **extra):
    return client.post("/auth/profile", data={"section": "api-key", "openrouter_api_key": key, **extra})


def _deck(app, email="a@example.com", status="draft"):
    with app.app_context():
        user = User.query.filter_by(email=email).one()
        deck = Deck(user_id=user.id, title="Chapter", card_style="basic", status=status, source_type="text",
                    source_text="Photosynthesis converts light into glucose.", settings_json={}, run_json={})
        _db.session.add(deck)
        _db.session.commit()
        return deck.id


# ------------------------------------------------------------------ storage
def test_key_is_saved_encrypted_and_never_sent_back(client, app):
    register(client)
    page = client.get("/auth/profile")
    assert b"No key yet" in page.data and b'name="openrouter_api_key"' in page.data

    response = _save_key(client, f"  {KEY}\n")
    assert response.status_code == 303 and response.location.endswith("#api-key")
    with app.app_context():
        user = User.query.one()
        assert user_key(user) == KEY and user.openrouter_key_hint == KEY[-4:]
        assert KEY not in user.openrouter_key_encrypted
    with sqlite3.connect(app.config["SQLALCHEMY_DATABASE_URI"].database) as connection:
        assert KEY not in repr(connection.execute("SELECT * FROM user").fetchall())

    page = client.get("/auth/profile")
    assert f"A key ending in <code>{KEY[-4:]}</code> is saved".encode() in page.data
    assert KEY.encode() not in page.data and b"Remove key" in page.data


@pytest.mark.parametrize("bad", ["", "   ", "not-a-key", "sk-or-v1-abc def", "sk-or-" + "x" * 200])
def test_invalid_key_is_rejected_and_the_saved_one_kept(client, app, bad):
    register(client)
    _save_key(client)
    response = _save_key(client, bad)
    assert response.status_code == 422 and b'id="openrouter_api_key-error"' in response.data
    # A rejected value is not echoed back into the form.
    assert b'value="not-a-key"' not in response.data
    with app.app_context():
        assert user_key(User.query.one()) == KEY


def test_key_can_be_replaced_and_removed(client, app):
    register(client)
    _save_key(client)
    _save_key(client, OTHER_KEY)
    with app.app_context():
        assert user_key(User.query.one()) == OTHER_KEY
    response = client.post("/auth/profile", data={"section": "api-key", "action": "remove"}, follow_redirects=True)
    assert b"Your OpenRouter key is removed." in response.data and b"No key yet" in response.data
    with app.app_context():
        user = User.query.one()
        assert user.openrouter_key_encrypted is None and user.openrouter_key_hint is None


def test_key_saved_under_an_old_secret_is_treated_as_missing(client, app):
    register(client)
    _save_key(client)
    app.config["SECRET_KEY"] = "rotated-secret"
    client.post("/auth/login", data={"email": "a@example.com", "password": "password123"})
    with app.app_context():
        assert user_key(User.query.one()) == ""
    assert b"can no longer be read" in client.get("/auth/profile").data


def test_keys_are_private_to_each_account(client, app):
    register(client)
    _save_key(client)
    client.post("/auth/logout")
    register(client, email="b@example.com")
    assert KEY[-4:].encode() not in client.get("/auth/profile").data
    with app.app_context():
        assert user_key(User.query.filter_by(email="b@example.com").one()) == ""


# ------------------------------------------------------------------ who pays
def test_user_key_wins_over_the_server_key(app):
    with app.app_context():
        user = User(email="x@example.com")
        assert openrouter_key_for(user) == ""
        app.config["OPENROUTER_API_KEY"] = "sk-or-server"
        assert openrouter_key_for(user) == "sk-or-server"
        set_user_key(user, KEY)
        assert openrouter_key_for(user) == KEY


def test_generation_and_card_fixes_run_on_the_deck_owners_key(client, app, monkeypatch):
    register(client)
    _save_key(client)
    other = app.test_client()
    register(other, email="b@example.com")
    _save_key(other, OTHER_KEY)
    app.config["OPENROUTER_API_KEY"] = "sk-or-server"

    fake, keys = FakeLLM(), []

    def chat(messages, model, api_key, *args, **kwargs):
        keys.append(api_key)
        return fake(messages, model, api_key, *args, **kwargs)

    def embeddings(texts, model, api_key, *args, **kwargs):
        keys.append(api_key)
        return fake_embeddings(texts)

    monkeypatch.setattr(llm_module, "openrouter_chat", chat)
    monkeypatch.setattr(llm_module, "openrouter_embeddings", embeddings)
    deck_id = _deck(app, email="b@example.com")
    with app.app_context():
        assert deckgen.generate_deck(deck_id) == deck_id
        card = Card.query.filter_by(deck_id=deck_id, type="basic", status="ok").first()
        card_id, source_id = card.id, card.source_id
        deckgen.improve_card(card_id)
        _db.session.expunge_all()  # regenerate replaces the unit's cards, reusing their ids
        deckgen.regenerate_source(source_id)
    assert keys and set(keys) == {OTHER_KEY}


# ------------------------------------------------------------------ without a key
def test_generation_is_refused_without_a_key_and_the_brief_is_kept(client, app, monkeypatch):
    started = []
    monkeypatch.setattr(main_routes, "dispatch_generation", lambda *args, **kwargs: started.append(args))
    register(client)
    deck_id = _deck(app)
    page = client.get(f"/decks/{deck_id}/preview")
    assert b"Add your OpenRouter API key to generate." in page.data and b"/auth/profile#api-key" in page.data

    response = client.post(f"/decks/{deck_id}/preview", data={"focus": "enzymes", "target_cards": "auto"},
                           follow_redirects=True)
    assert b"Add your OpenRouter API key under My profile before generating." in response.data
    assert started == []
    with app.app_context():
        deck = _db.session.get(Deck, deck_id)
        assert deck.status == "draft" and deck.settings_json["focus"] == "enzymes"

    _save_key(client)
    assert b"Add your OpenRouter API key to generate." not in client.get(f"/decks/{deck_id}/preview").data
    response = client.post(f"/decks/{deck_id}/preview", data={"target_cards": "auto"})
    assert response.status_code == 302 and "/status" in response.location
    assert len(started) == 1
    with app.app_context():
        assert _db.session.get(Deck, deck_id).status == "processing"


@pytest.mark.parametrize("action", ["run", "replan"])
def test_plan_actions_need_a_key(client, app, monkeypatch, action):
    started = []
    monkeypatch.setattr(main_routes, "dispatch_generation", lambda *args, **kwargs: started.append(args))
    register(client)
    deck_id = _deck(app, status="planned")
    response = client.post(f"/decks/{deck_id}/plan", data={"action": action})
    assert response.status_code == 302 and response.location.endswith(f"/decks/{deck_id}/plan")
    assert started == []
    with app.app_context():
        assert _db.session.get(Deck, deck_id).status == "planned"


def test_editor_ai_actions_explain_the_missing_key(client, app, monkeypatch):
    called = []
    monkeypatch.setattr(main_routes, "improve_card", lambda *args: called.append(args))
    monkeypatch.setattr(main_routes, "regenerate_source", lambda *args: called.append(args))
    monkeypatch.setattr(main_routes, "coach_cards", lambda *args: called.append(args))
    register(client)
    deck_id = _deck(app, status="ready")
    with app.app_context():
        card = Card(deck_id=deck_id, type="basic", front="q", back="a", status="ok")
        _db.session.add(card)
        _db.session.commit()
        card_id = card.id

    response = client.post(f"/cards/{card_id}/improve")
    assert response.status_code == 200 and "My profile" in response.headers["HX-Trigger"]
    for action in ("regenerate", "coach"):
        client.post("/cards/bulk", data={"card_ids": [str(card_id)], "action": action})
    response = client.post(f"/decks/{deck_id}/coach", follow_redirects=True)
    assert b"Add your OpenRouter API key under My profile before generating." in response.data
    assert called == []


def test_sidebar_prompts_for_a_key_until_one_is_usable(client, app):
    register(client)
    assert b"Add your OpenRouter key." in client.get("/decks").data
    _save_key(client)
    assert b"Add your OpenRouter key." not in client.get("/decks").data
    client.post("/auth/profile", data={"section": "api-key", "action": "remove"})
    app.config["OPENROUTER_API_KEY"] = "sk-or-server"
    assert b"Add your OpenRouter key." not in client.get("/decks").data
    assert b"This server has a shared key" in client.get("/auth/profile").data

