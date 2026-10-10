"""What an exported package tells Anki about who its deck and notes are."""

import io
import json
import os
import sqlite3
import zipfile

from app.extensions import db as _db
from app.models import Card, Deck, User
from app.services.export import export_deck, legacy_note_guid


def _deck(title, fronts):
    user = User.query.first()
    if user is None:
        user = User(email="export@example.com")
        user.set_password("password123")
        _db.session.add(user)
        _db.session.commit()
    deck = Deck(user_id=user.id, title=title, card_style="basic", status="ready", source_type="text",
                source_text="x", settings_json={}, run_json={})
    _db.session.add(deck)
    _db.session.commit()
    _db.session.add_all(Card(deck_id=deck.id, type="basic", front=front, back="answer", status="ok") for front in fronts)
    _db.session.commit()
    return deck


def _cards(deck):
    return Card.query.filter_by(deck_id=deck.id).order_by(Card.id).all()


def _identity(package_bytes, tmp_path):
    """(the package's deck ids, {note front: guid}) as Anki would read them."""
    path = os.path.join(tmp_path, "collection.anki2")
    with open(path, "wb") as fh:
        fh.write(zipfile.ZipFile(io.BytesIO(package_bytes)).read("collection.anki2"))
    conn = sqlite3.connect(path)
    try:
        decks = json.loads(conn.execute("SELECT decks FROM col").fetchone()[0])
        notes = {flds.split("\x1f")[0]: guid for flds, guid in conn.execute("SELECT flds, guid FROM notes")}
    finally:
        conn.close()
    os.remove(path)
    return {int(deck_id) for deck_id in decks} - {1}, notes


def test_a_deck_that_reuses_a_deleted_decks_row_ids_stays_separate_in_anki(app, tmp_path):
    with app.app_context():
        first = _deck("Theory of Computation", ["What is a DFA?", "What is an NFA?"])
        first_rows = (first.id, [c.id for c in _cards(first)])
        first_decks, first_notes = _identity(export_deck(first.id)[0].getvalue(), tmp_path)
        _db.session.delete(first)
        _db.session.commit()

        second = _deck("Economics", ["What is supply?", "What is demand?", "What is a market?"])
        # SQLite hands the freed ids out again, so both decks were deck 1 with cards 1, 2.
        assert (second.id, [c.id for c in _cards(second)][:2]) == first_rows
        second_decks, second_notes = _identity(export_deck(second.id)[0].getvalue(), tmp_path)

        assert len(set(second_notes.values())) == 3
        assert not set(first_notes.values()) & set(second_notes.values())
        assert not first_decks & second_decks


def test_exporting_a_deck_again_keeps_its_identity(app, tmp_path):
    with app.app_context():
        deck = _deck("Economics", ["What is supply?", "What is demand?"])
        decks, notes = _identity(export_deck(deck.id)[0].getvalue(), tmp_path)
        assert notes == {c.front: c.guid for c in _cards(deck)}

        _db.session.add(Card(deck_id=deck.id, type="basic", front="What is a market?", back="answer", status="ok"))
        _db.session.commit()
        decks_again, notes_again = _identity(export_deck(deck.id)[0].getvalue(), tmp_path)

        assert decks_again == decks
        assert {front: notes_again[front] for front in notes} == notes
        assert notes_again["What is a market?"] not in notes.values()


def test_guids_derived_from_row_ids_by_older_exports_are_replaced(app, tmp_path):
    with app.app_context():
        deck = _deck("Economics", ["What is supply?", "What is demand?"])
        old, kept = _cards(deck)
        old.guid = legacy_note_guid(deck.id, old.id)
        kept.guid = "a-guid-of-its-own"
        _db.session.commit()
        legacy = old.guid

        _, notes = _identity(export_deck(deck.id)[0].getvalue(), tmp_path)

        assert notes["What is supply?"] == old.guid != legacy
        assert notes["What is demand?"] == kept.guid == "a-guid-of-its-own"
