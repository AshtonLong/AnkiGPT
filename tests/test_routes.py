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


# ------------------------------------------------------- editing cards by hand
def _own_deck(app, client, card_style="basic", status="ready", email="editor@example.com"):
    """Sign `client` in as a new user and give them a deck; returns its id."""
    register(client, email=email)
    with app.app_context():
        user = User.query.filter_by(email=email).first()
        deck = Deck(user_id=user.id, title="Mine", card_style=card_style, status=status, source_type="text",
                    source_text="x", settings_json={})
        _db.session.add(deck)
        _db.session.commit()
        return deck.id


def _add(app, deck_id, **fields):
    with app.app_context():
        card = Card(deck_id=deck_id, status="ok", **fields)
        _db.session.add(card)
        _db.session.commit()
        return card.id


def _card(app, card_id):
    with app.app_context():
        card = _db.session.get(Card, card_id)
        _db.session.expunge(card)
        return card


def test_saving_a_cloze_makes_its_deletions_safe_for_anki(client, app):
    deck_id = _own_deck(app, client)
    card_id = _add(app, deck_id, type="cloze", cloze_text="The slope is {{c1::m}}.", extra="")
    resp = client.post(f"/cards/{card_id}", data={"cloze_text": r"The slope is {{c2::\frac{a}{b}}}.", "extra": "", "tags": ""})
    card = _card(app, card_id)
    # A space keeps Anki from ending the deletion at the brace of \frac{a}{b}; the number is the student's.
    assert card.cloze_text == r"The slope is {{c2::\frac{a}{b} }}." and card.status == "ok"
    assert b"The slope is <mark>[...]</mark>." in resp.data


def test_a_cloze_with_no_sound_deletion_waits_for_review(client, app):
    deck_id = _own_deck(app, client)
    card_id = _add(app, deck_id, type="cloze", cloze_text="The slope is {{c1::m}}.", extra="")
    resp = client.post(f"/cards/{card_id}", data={"cloze_text": "The slope is {{c1::m.", "extra": "", "tags": ""})
    assert _card(app, card_id).status == "needs_review"
    assert b"never closed" in resp.data
    client.post(f"/cards/{card_id}", data={"cloze_text": "The slope is {{c1::m}}.", "extra": "", "tags": ""})
    assert _card(app, card_id).status == "ok"


def test_a_basic_card_missing_a_side_waits_for_review(client, app):
    deck_id = _own_deck(app, client)
    card_id = _add(app, deck_id, type="basic", front="Q", back="A")
    client.post(f"/cards/{card_id}", data={"front": "Q?", "back": "", "tags": ""})
    assert _card(app, card_id).status == "needs_review"
    client.post(f"/cards/{card_id}", data={"front": "Q?", "back": "A.", "tags": "one, two"})
    card = _card(app, card_id)
    assert (card.status, card.tags) == ("ok", ["one", "two"])


def test_the_editor_previews_the_cards_a_cloze_note_makes(client, app):
    deck_id = _own_deck(app, client)
    _add(app, deck_id, type="cloze", cloze_text="{{c1::Insulin::hormone}} lowers blood {{c2::glucose}}.", extra="")
    page = client.get(f"/decks/{deck_id}").get_data(as_text=True)
    assert "<mark>[hormone]</mark> lowers blood glucose." in page
    assert "Insulin lowers blood <mark>[...]</mark>." in page
    assert "Make basic" in page and "Add card" in page


def test_the_editor_shows_how_the_deck_splits_between_types(client, app):
    deck_id = _own_deck(app, client, card_style="mixed")
    _add(app, deck_id, type="basic", front="Q", back="A")
    _add(app, deck_id, type="cloze", cloze_text="{{c1::a}} b", extra="")
    _add(app, deck_id, type="cloze", cloze_text="{{c1::c}} d", extra="")
    gone = _add(app, deck_id, type="cloze", cloze_text="{{c1::e}} f", extra="")
    client.post("/cards/bulk", data={"action": "delete", "card_ids": [str(gone)]})
    # Whatever the filter shows, the split is the whole deck's, deleted cards left out.
    assert "1 basic · 2 cloze" in client.get(f"/decks/{deck_id}?type=basic").get_data(as_text=True)


def test_cloze_preview_is_redrawn_from_what_is_typed(client, app):
    _own_deck(app, client)
    typed = client.post("/cloze/preview", data={"cloze_text": r"\( v = {{c1::\sqrt{2gh}}} \)"}).get_data(as_text=True)
    assert r"\( v = <mark>[...]</mark> \)" in typed and r"\sqrt{2gh}" in typed
    assert "Saving adds a space" in typed
    assert "No deletion yet" in client.post("/cloze/preview", data={"cloze_text": "plain"}).get_data(as_text=True)


def test_cloze_preview_requires_sign_in(client):
    assert client.post("/cloze/preview", data={"cloze_text": "{{c1::x}}"}).status_code == 302


def test_add_card_appends_a_blank_card_that_is_not_exported_yet(client, app):
    deck_id = _own_deck(app, client)
    first = _add(app, deck_id, type="basic", front="Q", back="A", order_key=2004)
    resp = client.post(f"/decks/{deck_id}/cards")
    with app.app_context():
        new = Card.query.filter(Card.deck_id == deck_id, Card.id != first).one()
        assert (new.type, new.status, new.front, new.back) == ("basic", "needs_review", "", "")
        assert new.order_key == 2005
        assert resp.status_code == 302 and resp.headers["Location"].endswith(f"/decks/{deck_id}#card-{new.id}")
    # Filled in and saved, it joins the deck.
    client.post(f"/cards/{new.id}", data={"front": "Why?", "back": "Because.", "tags": ""})
    assert _card(app, new.id).status == "ok"


def test_add_card_follows_the_decks_style(client, app):
    deck_id = _own_deck(app, client, card_style="cloze")
    client.post(f"/decks/{deck_id}/cards")
    with app.app_context():
        new = Card.query.filter_by(deck_id=deck_id).one()
        assert (new.type, new.cloze_text, new.status) == ("cloze", "", "needs_review")


def test_no_card_is_added_while_a_deck_is_generating(client, app):
    deck_id = _own_deck(app, client, status="processing")
    assert client.post(f"/decks/{deck_id}/cards").status_code == 302
    with app.app_context():
        assert Card.query.filter_by(deck_id=deck_id).count() == 0


def test_convert_basic_to_cloze_carries_the_wording_over_as_a_draft(client, app):
    deck_id = _own_deck(app, client)
    card_id = _add(app, deck_id, type="basic", front="Saved question?", back="Saved", guid="abc123")
    # What is on screen is converted, not the last save.
    resp = client.post(f"/cards/{card_id}/convert", data={"front": "What is the slope?", "back": r"\frac{a}{b}", "tags": "maths"})
    card = _card(app, card_id)
    assert card.type == "cloze" and card.cloze_text == r"What is the slope? {{c1::\frac{a}{b} }}"
    assert (card.front, card.back, card.extra, card.tags) == (None, None, "", ["maths"])
    # A draft until the student rewords and saves it, and a new note for Anki.
    assert card.status == "needs_review" and card.guid is None
    assert b"Make basic" in resp.data and b"What is the slope? <mark>[...]</mark>" in resp.data


def test_convert_cloze_to_basic_asks_for_the_blanks(client, app):
    deck_id = _own_deck(app, client)
    card_id = _add(app, deck_id, type="cloze", cloze_text="{{c1::Insulin::hormone}} lowers blood {{c2::glucose}}.",
                   extra="Released by beta cells.")
    client.post(f"/cards/{card_id}/convert")
    card = _card(app, card_id)
    assert card.type == "basic" and card.front == "[...] lowers blood [...]."
    assert card.back == "Insulin / glucose\nReleased by beta cells."
    assert (card.cloze_text, card.extra, card.status) == (None, None, "needs_review")


def test_converting_a_deleted_card_does_not_restore_it(client, app):
    deck_id = _own_deck(app, client)
    card_id = _add(app, deck_id, type="basic", front="Q", back="A")
    client.post("/cards/bulk", data={"action": "delete", "card_ids": [str(card_id)]})
    client.post(f"/cards/{card_id}/convert")
    card = _card(app, card_id)
    assert (card.type, card.status) == ("cloze", "deleted")


def test_cannot_add_to_or_convert_in_other_users_deck(client, app):
    other_deck_id, other_card_id = _make_deck_with_card(app, "owner5@example.com")
    register(client, email="attacker5@example.com")
    assert client.post(f"/decks/{other_deck_id}/cards").status_code == 404
    assert client.post(f"/cards/{other_card_id}/convert").status_code == 404
    with app.app_context():
        assert Card.query.filter_by(deck_id=other_deck_id).count() == 1
        assert _db.session.get(Card, other_card_id).type == "basic"
