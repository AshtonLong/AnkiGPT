from datetime import datetime, timezone
from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash
from .extensions import db


def utcnow():
    """Timezone-aware UTC now. datetime.utcnow() is deprecated in Python 3.12+."""
    return datetime.now(timezone.utc)


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)

    decks = db.relationship(
        "Deck", backref="user", cascade="all, delete-orphan", passive_deletes=True
    )

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Deck(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title = db.Column(db.String(200), nullable=False)
    card_style = db.Column(db.String(20), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="draft", index=True)
    source_type = db.Column(db.String(20), nullable=False)
    source_text = db.Column(db.Text, nullable=False)
    settings_json = db.Column(db.JSON, nullable=False, default=dict)
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    sources = db.relationship(
        "Source", backref="deck", cascade="all, delete-orphan", passive_deletes=True
    )
    cards = db.relationship(
        "Card", backref="deck", cascade="all, delete-orphan", passive_deletes=True
    )


class Source(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    deck_id = db.Column(
        db.Integer, db.ForeignKey("deck.id", ondelete="CASCADE"), nullable=False, index=True
    )
    idx = db.Column(db.Integer, nullable=False)
    title = db.Column(db.String(200))
    text = db.Column(db.Text, nullable=False)
    hash = db.Column(db.String(64), nullable=False)

    cards = db.relationship(
        "Card", backref="source", cascade="all, delete-orphan", passive_deletes=True
    )


class Card(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    deck_id = db.Column(
        db.Integer, db.ForeignKey("deck.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id = db.Column(
        db.Integer, db.ForeignKey("source.id", ondelete="CASCADE"), index=True
    )
    type = db.Column(db.String(20), nullable=False, index=True)
    front = db.Column(db.Text)
    back = db.Column(db.Text)
    cloze_text = db.Column(db.Text)
    extra = db.Column(db.Text)
    tags = db.Column(db.JSON, default=list)
    status = db.Column(db.String(20), nullable=False, default="ok", index=True)
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)


class LLMRun(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    deck_id = db.Column(
        db.Integer, db.ForeignKey("deck.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id = db.Column(
        db.Integer, db.ForeignKey("source.id", ondelete="SET NULL"), index=True
    )
    model = db.Column(db.String(100))
    prompt_version = db.Column(db.String(50), index=True)
    input_tokens = db.Column(db.Integer)
    output_tokens = db.Column(db.Integer)
    cost_estimate = db.Column(db.Float)
    request_json = db.Column(db.JSON)
    response_text = db.Column(db.Text)
    parsed_json = db.Column(db.JSON)
    error = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow)
