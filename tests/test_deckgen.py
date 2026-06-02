import json

from app.extensions import db as _db
from app.models import Card, Deck, User
from app.services import deckgen
from app.services.llm import OpenRouterError


def _fake_response(content):
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.001},
    }


def _make_deck(app, source_text="Photosynthesis converts CO2 and water into glucose."):
    with app.app_context():
        user = User(email="gen@example.com")
        user.set_password("password123")
        _db.session.add(user)
        _db.session.commit()
        deck = Deck(
            user_id=user.id,
            title="Gen",
            card_style="basic",
            status="draft",
            source_type="text",
            source_text=source_text,
            settings_json={"max_chars": 3500},
        )
        _db.session.add(deck)
        _db.session.commit()
        return deck.id


def test_generate_deck_happy_path(app, monkeypatch):
    def fake_chat(*args, **kwargs):
        if kwargs.get("response_format"):  # card pass
            return _fake_response(
                json.dumps(
                    {"cards": [{"type": "basic", "front": "What does photosynthesis produce?",
                                "back": "Glucose", "cloze_text": None, "extra": None, "tags": []}]}
                )
            )
        return _fake_response("## Photosynthesis\n- Converts CO2 and water into glucose")

    monkeypatch.setattr(deckgen, "openrouter_chat", fake_chat)
    deck_id = _make_deck(app)
    with app.app_context():
        app.config["OPENROUTER_API_KEY"] = "test-key"
        result = deckgen.generate_deck(deck_id)
        assert result == deck_id
        deck = _db.session.get(Deck, deck_id)
        assert deck.status == "ready"
        ok_cards = Card.query.filter_by(deck_id=deck_id, status="ok").count()
        assert ok_cards >= 1


def test_failed_generation_is_non_destructive(app, monkeypatch):
    """If the cheat-sheet stage fully fails, the deck is marked failed and any
    pre-existing cards are NOT wiped."""
    deck_id = _make_deck(app)
    with app.app_context():
        # Seed a pre-existing "good" card from an earlier run.
        existing = Card(deck_id=deck_id, type="basic", front="old", back="card", status="ok")
        _db.session.add(existing)
        _db.session.commit()
        existing_id = existing.id

    def always_fail(*args, **kwargs):
        raise OpenRouterError("upstream down", status_code=503)

    monkeypatch.setattr(deckgen, "openrouter_chat", always_fail)
    with app.app_context():
        app.config["OPENROUTER_API_KEY"] = "test-key"
        result = deckgen.generate_deck(deck_id)
        assert result is None
        deck = _db.session.get(Deck, deck_id)
        assert deck.status == "failed"
        assert (deck.settings_json or {}).get("last_error")
        # The earlier card must survive a failed regeneration.
        assert _db.session.get(Card, existing_id) is not None


def test_terminal_auth_error_fails_fast(app, monkeypatch):
    def auth_fail(*args, **kwargs):
        raise OpenRouterError("bad key", status_code=401)

    monkeypatch.setattr(deckgen, "openrouter_chat", auth_fail)
    deck_id = _make_deck(app)
    with app.app_context():
        app.config["OPENROUTER_API_KEY"] = "test-key"
        assert deckgen.generate_deck(deck_id) is None
        assert _db.session.get(Deck, deck_id).status == "failed"
