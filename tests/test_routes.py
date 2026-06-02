from app.config import Config
from app.extensions import db as _db
from app.models import Card, Deck, User

from conftest import login, logout, register


def _make_deck_with_card(app, email):
    """Create a user owning one deck with one card; return (deck_id, card_id)."""
    with app.app_context():
        user = User(email=email)
        user.set_password("password123")
        _db.session.add(user)
        _db.session.commit()
        deck = Deck(
            user_id=user.id,
            title="Owned",
            card_style="basic",
            status="ready",
            source_type="text",
            source_text="x",
            settings_json={},
        )
        _db.session.add(deck)
        _db.session.commit()
        card = Card(deck_id=deck.id, type="basic", front="Q", back="A", status="ok")
        _db.session.add(card)
        _db.session.commit()
        return deck.id, card.id


def test_index_ok(client):
    assert client.get("/").status_code == 200


def test_decks_requires_auth(client):
    resp = client.get("/decks")
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]


def test_signup_then_access(client):
    resp = register(client)
    assert resp.status_code == 200
    assert client.get("/decks").status_code == 200


def test_signup_rejects_short_password(client):
    resp = client.post(
        "/auth/signup", data={"email": "b@example.com", "password": "short"}, follow_redirects=True
    )
    assert b"at least 8 characters" in resp.data


def test_create_text_deck(client):
    register(client)
    resp = client.post(
        "/decks/new",
        data={"title": "My Deck", "source_type": "text", "card_style": "basic", "text_input": "Some study notes."},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "/preview" in resp.headers["Location"]


def test_cannot_view_other_users_deck(client, app):
    other_deck_id, _ = _make_deck_with_card(app, "owner@example.com")
    register(client, email="attacker@example.com")
    # Attacker is logged in as a different user; the owner's deck must 404, not leak.
    assert client.get(f"/decks/{other_deck_id}").status_code == 404
    assert client.get(f"/decks/{other_deck_id}/status").status_code == 404
    assert client.post(f"/decks/{other_deck_id}/export").status_code == 404


def test_cannot_update_other_users_card(client, app):
    _, other_card_id = _make_deck_with_card(app, "owner2@example.com")
    register(client, email="attacker2@example.com")
    resp = client.post(f"/cards/{other_card_id}", data={"front": "hacked", "back": "x", "tags": ""})
    assert resp.status_code == 404
    with app.app_context():
        assert _db.session.get(Card, other_card_id).front == "Q"


def test_bulk_cannot_touch_other_users_cards(client, app):
    _, other_card_id = _make_deck_with_card(app, "owner3@example.com")
    register(client, email="attacker3@example.com")
    client.post("/cards/bulk", data={"action": "delete", "card_ids": [str(other_card_id)]})
    with app.app_context():
        # The other user's card must remain untouched.
        assert _db.session.get(Card, other_card_id).status == "ok"


def test_owner_can_delete_own_deck(client, app):
    register(client, email="owner4@example.com")
    client.post(
        "/decks/new",
        data={"title": "D", "source_type": "text", "card_style": "basic", "text_input": "notes"},
    )
    with app.app_context():
        deck = Deck.query.filter_by(title="D").first()
        deck_id = deck.id
    resp = client.post(f"/decks/{deck_id}/delete", follow_redirects=False)
    assert resp.status_code == 302
    with app.app_context():
        assert _db.session.get(Deck, deck_id) is None


def test_csrf_is_enforced(tmp_path):
    # Separate app instance with CSRF turned ON (the default) to prove enforcement.
    from app import create_app

    class CsrfConfig(Config):
        TESTING = True
        SECRET_KEY = "csrf-test"
        WTF_CSRF_ENABLED = True
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{tmp_path / 'csrf.db'}"

    app = create_app(CsrfConfig)
    client = app.test_client()
    # POST without a token should be rejected.
    resp = client.post("/auth/signup", data={"email": "c@example.com", "password": "password123"})
    assert resp.status_code == 400
