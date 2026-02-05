import io
import genanki
from ..models import Card, Deck


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
                "qfmt": "<div class='card-front'>{{Front}}</div>",
                "afmt": "{{FrontSide}}<hr id='answer'><div class='card-back'>{{Back}}</div>",
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
        templates=[{"name": "Cloze", "qfmt": "{{cloze:Text}}", "afmt": "{{cloze:Text}}<hr id='answer'>{{Extra}}"}],
        css=card_css(),
        model_type=model_type,
    )


def card_css():
    return """
.card {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
  font-size: 18px;
  text-align: left;
  color: #111;
  background: #fff;
  padding: 20px;
  line-height: 1.5;
}
.card-front, .card-back {
  max-width: 720px;
  margin: 0 auto;
}
#answer {
  border: none;
  border-top: 1px solid #e5e7eb;
  margin: 16px 0;
}
"""


def safe_filename(name):
    if not name:
        return "ankigpt_deck"
    clean = "".join([c if c.isalnum() or c in ("-", "_") else "_" for c in name.lower()])
    return clean.strip("_") or "ankigpt_deck"


def export_deck(deck_id):
    deck = Deck.query.get(deck_id)
    if not deck:
        return None
    cards = Card.query.filter_by(deck_id=deck_id, status="ok").all()
    if not cards:
        return None
    deck_id_seed = int(f"{deck_id}001")
    genanki_deck = genanki.Deck(deck_id_seed, deck.title)
    basic_model = build_basic_model()
    cloze_model = build_cloze_model()

    for card in cards:
        if card.type == "basic":
            note = genanki.Note(model=basic_model, fields=[sanitize(card.front), sanitize(card.back)])
        else:
            note = genanki.Note(model=cloze_model, fields=[sanitize(card.cloze_text), sanitize(card.extra or "")])
        if card.tags:
            safe_tags = [sanitize_tag(t) for t in card.tags]
            note.tags = [t for t in safe_tags if t]
        genanki_deck.add_note(note)

    filename = f"{safe_filename(deck.title)}_{deck_id}.apkg"
    package = genanki.Package(genanki_deck)
    buffer = io.BytesIO()
    package.write_to_file(buffer)
    buffer.seek(0)
    return buffer, filename


def sanitize(text):
    if not text:
        return ""
    return text.replace("\n", "<br>")
