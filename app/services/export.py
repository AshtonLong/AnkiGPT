import io
import os
import tempfile

import genanki

from ..extensions import db
from ..models import Card, Deck, Figure


def sanitize_tag(tag):
    if not tag:
        return None
    clean = "".join([c if c.isalnum() or c in ("-", "_", ":") else "_" for c in tag.strip().lower()])
    clean = clean.strip("_")
    return clean or None


def build_basic_model():
    return genanki.Model(
        1607392319,
        "AnkiGPT Basic",
        fields=[{"name": "Front"}, {"name": "Back"}],
        templates=[
            {
                "name": "Card 1",
                "qfmt": "<div class='container'><div class='card-front'>{{Front}}</div></div>",
                "afmt": "<div class='container'>{{FrontSide}}<hr id='answer'><div class='card-back'>{{Back}}</div></div>",
            }
        ],
        css=card_css(),
    )


def build_cloze_model():
    model_type = getattr(genanki, "MODEL_CLOZE", 1)
    return genanki.Model(
        998877661,
        "AnkiGPT Cloze",
        fields=[{"name": "Text"}, {"name": "Extra"}],
        templates=[{"name": "Cloze", "qfmt": "<div class='container'>{{cloze:Text}}</div>", "afmt": "<div class='container'>{{cloze:Text}}<hr id='answer'>{{Extra}}</div>"}],
        css=card_css(),
        model_type=model_type,
    )


def card_css():
    return """
.card {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  font-size: 20px;
  line-height: 1.6;
  color: #333;
  background-color: #fcfcfc;
  text-align: left;
  padding: 20px;
  margin: 0;
  display: flex;
  justify-content: center;
}

.container {
  max-width: 650px;
  width: 100%;
  margin: 0 auto;
}

.card-front, .card-back {
  padding: 10px 0;
}

/* Cloze styling */
.cloze {
  font-weight: 600;
  color: #2563eb;
  background: rgba(37, 99, 235, 0.1);
  padding: 0 4px;
  border-radius: 4px;
}

/* Divider */
#answer {
  border: none;
  border-top: 2px dashed #e5e7eb;
  margin: 30px 0;
}

/* Dark Mode */
@media (prefers-color-scheme: dark) {
  .card {
    background-color: #1a1a1a;
    color: #e5e5e5;
  }

  .cloze {
    color: #818cf8;
    background: rgba(129, 140, 248, 0.15);
  }

  #answer {
    border-top-color: #333;
  }
}

/* Images */
img {
  max-width: 100%;
  border-radius: 8px;
  box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
  display: block;
  margin: 20px auto;
}
.figure { margin: 0 0 14px; }
.figure img { max-height: 420px; object-fit: contain; }

/* Lists */
ul, ol {
  padding-left: 24px;
  margin: 8px 0;
}
li {
  margin-bottom: 6px;
}

/* Blockquotes */
blockquote {
  border-left: 3px solid #e5e7eb;
  margin: 16px 0;
  padding-left: 12px;
  color: #6b7280;
  font-style: italic;
}
@media (prefers-color-scheme: dark) {
  blockquote {
    border-left-color: #404040;
    color: #a3a3a3;
  }
}
"""


def safe_filename(name):
    if not name:
        return "ankigpt_deck"
    clean = "".join([c if c.isalnum() or c in ("-", "_") else "_" for c in name.lower()])
    return clean.strip("_") or "ankigpt_deck"


def figure_media_name(figure_id):
    return f"ankigpt_fig_{figure_id}.png"


def note_guid(deck_id, card_id):
    """Stable per-card guid so review stats can be matched back after study."""
    return genanki.guid_for("ankigpt", deck_id, card_id)


def export_deck(deck_id):
    deck = db.session.get(Deck, deck_id)
    if not deck:
        return None
    cards = (
        Card.query.filter_by(deck_id=deck_id, status="ok")
        .order_by(Card.order_key, Card.id)
        .all()
    )
    if not cards:
        return None
    deck_id_seed = int(f"{deck_id}001")
    genanki_deck = genanki.Deck(deck_id_seed, deck.title)
    basic_model = build_basic_model()
    cloze_model = build_cloze_model()

    figure_ids = {c.figure_id for c in cards if c.figure_id}
    figures = {f.id: f for f in Figure.query.filter(Figure.id.in_(figure_ids)).all()} if figure_ids else {}

    for card in cards:
        img = ""
        if card.figure_id and card.figure_id in figures:
            img = f"<div class='figure'><img src='{figure_media_name(card.figure_id)}'></div>"
        guid = note_guid(deck_id, card.id)
        if card.type == "basic":
            note = genanki.Note(model=basic_model, fields=[img + sanitize(card.front), sanitize(card.back)], guid=guid)
        else:
            note = genanki.Note(model=cloze_model, fields=[img + sanitize(card.cloze_text), sanitize(card.extra or "")], guid=guid)
        if card.tags:
            safe_tags = [sanitize_tag(t) for t in card.tags]
            note.tags = [t for t in safe_tags if t]
        genanki_deck.add_note(note)
        card.guid = guid
    db.session.commit()

    filename = f"{safe_filename(deck.title)}_{deck_id}.apkg"
    package = genanki.Package(genanki_deck)
    buffer = io.BytesIO()
    if figures:
        with tempfile.TemporaryDirectory(prefix="ankigpt_media_") as tmp:
            paths = []
            for fid, fig in figures.items():
                path = os.path.join(tmp, figure_media_name(fid))
                with open(path, "wb") as fh:
                    fh.write(fig.image)
                paths.append(path)
            package.media_files = paths
            package.write_to_file(buffer)
    else:
        package.write_to_file(buffer)
    buffer.seek(0)
    return buffer, filename


def sanitize(text):
    if not text:
        return ""
    return text.replace("\n", "<br>")
