"""Close-the-loop tests: export -> (simulated study) -> import review stats -> coach."""

import io
import sqlite3
import zipfile

from app.extensions import db as _db
from app.models import Card, Deck, Source, User
from app.services import llm as llm_module
from app.services.export import export_deck
from app.services.pipeline import feedback

from conftest import FakeLLM


def _deck_with_cards(app):
    with app.app_context():
        user = User(email="loop@example.com")
        user.set_password("password123")
        _db.session.add(user)
        _db.session.commit()
        deck = Deck(user_id=user.id, title="Loop", card_style="basic", status="ready", source_type="text",
                    source_text="Glucose is oxidised in the mitochondria to make ATP.", settings_json={}, run_json={})
        _db.session.add(deck)
        _db.session.commit()
        unit = Source(deck_id=deck.id, idx=0, title="Respiration", text=deck.source_text, hash="h")
        _db.session.add(unit)
        _db.session.commit()
        cards = [
            Card(deck_id=deck.id, source_id=unit.id, type="basic", front="Where is glucose oxidised and what is made?",
                 back="Mitochondria; ATP", status="ok", strategy="general"),
            Card(deck_id=deck.id, source_id=unit.id, type="basic", front="What is made?", back="ATP", status="ok", strategy="general"),
            Card(deck_id=deck.id, source_id=unit.id, type="cloze", cloze_text="Glucose is oxidised in the {{c1::mitochondria}}.",
                 extra="", status="ok", strategy="general"),
        ]
        _db.session.add_all(cards)
        _db.session.commit()
        return deck.id, [c.id for c in cards]


def _simulate_study(package_bytes, guid_to_stats):
    """Rewrite the exported package's collection with cards/revlog rows that Anki would
    produce after study: (reps, lapses, again_count) per note guid."""
    zin = zipfile.ZipFile(io.BytesIO(package_bytes))
    name = next(n for n in zin.namelist() if n.startswith("collection."))
    raw = zin.read(name)
    with open("_tmp_col.sqlite", "wb") as fh:
        fh.write(raw)
    conn = sqlite3.connect("_tmp_col.sqlite")
    cur = conn.cursor()
    cur.execute("SELECT id, guid FROM notes")
    guid_by_nid = {nid: guid for nid, guid in cur.fetchall()}
    cur.execute("SELECT id, nid FROM cards")
    rev_id = 1
    for cid, nid in cur.fetchall():
        stats = guid_to_stats.get(guid_by_nid[nid])
        if not stats:
            continue
        reps, lapses, again = stats
        cur.execute("UPDATE cards SET reps=?, lapses=?, ivl=3, factor=2100 WHERE id=?", (reps, lapses, cid))
        for i in range(reps):
            ease = 1 if i < again else 3
            cur.execute("INSERT INTO revlog (id, cid, usn, ease, ivl, lastIvl, factor, time, type) VALUES (?,?,?,?,?,?,?,?,?)",
                        (rev_id, cid, -1, ease, 1, 1, 2100, 5000, 1))
            rev_id += 1
    conn.commit()
    conn.close()
    with open("_tmp_col.sqlite", "rb") as fh:
        modified = fh.read()
    import os

    os.remove("_tmp_col.sqlite")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zout:
        for n in zin.namelist():
            zout.writestr(n, modified if n == name else zin.read(n))
    return out.getvalue()


def test_export_import_flags_struggling_and_coach_rewrites(app, monkeypatch):
    deck_id, card_ids = _deck_with_cards(app)
    with app.app_context():
        buffer, filename = export_deck(deck_id)
        assert filename.endswith(".apkg")
        cards = Card.query.filter_by(deck_id=deck_id).order_by(Card.id).all()
        assert all(c.guid for c in cards), "export must stamp guids"
        package = buffer.getvalue()
        # Card 0: 8 reviews, 4 lapses, 5 'again' -> struggling. Card 1: fine. Card 2: too few reps.
        studied = _simulate_study(package, {
            cards[0].guid: (8, 4, 5), cards[1].guid: (8, 0, 1), cards[2].guid: (2, 2, 2),
        })
        stats = feedback.read_review_stats(studied)
        assert stats[cards[0].guid]["lapses"] == 4
        assert abs(stats[cards[0].guid]["again_rate"] - 5 / 8) < 0.01
        matched, struggling = feedback.apply_review_stats(deck_id, stats)
        assert (matched, struggling) == (3, 1)
        _db.session.refresh(cards[0])
        assert cards[0].review_stats_json["struggling"] is True
        assert "struggling" in cards[0].tags
        assert not (_db.session.get(Card, card_ids[1]).review_stats_json or {}).get("struggling")

        # Coach: the struggling card is split into two atomic replacements.
        fake = FakeLLM()
        monkeypatch.setattr(llm_module, "openrouter_chat", fake)
        app.config["OPENROUTER_API_KEY"] = "test-key"
        result = feedback.coach_cards(deck_id)
        assert result["cards"] == 1 and result["split"] == 1
        original = _db.session.get(Card, card_ids[0])
        assert original.status == "deleted" and "coach:split" in original.tags
        children = Card.query.filter_by(deck_id=deck_id, status="needs_review").all()
        assert {c.front for c in children} == {"Split A?", "Split B?"}
        assert all("coach:split_child" in c.tags and c.source_id == original.source_id for c in children)


def test_bad_package_is_rejected():
    import pytest

    with pytest.raises(feedback.ImportError_):
        feedback.read_review_stats(b"not a zip")
