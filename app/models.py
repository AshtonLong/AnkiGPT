from datetime import datetime, timezone
from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash
from .extensions import db


def utcnow():
    """Timezone-aware UTC now. datetime.utcnow() is deprecated in Python 3.12+."""
    return datetime.now(timezone.utc)


class User(UserMixin, db.Model):
    AVATAR_COLORS = ("terracotta", "sage", "blue", "lavender", "slate")

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)
    display_name = db.Column(db.String(80))
    bio = db.Column(db.String(280))
    avatar_color = db.Column(db.String(20))

    @property
    def profile_name(self):
        return self.display_name or self.email.split("@")[0] or "Learner"

    @property
    def initials(self):
        parts = self.profile_name.split()
        return (parts[0][0] + parts[-1][0] if len(parts) > 1 else parts[0][:2]).upper()

    @property
    def profile_color(self):
        return self.avatar_color if self.avatar_color in self.AVATAR_COLORS else "terracotta"

    decks = db.relationship(
        "Deck", backref="user", cascade="all, delete-orphan", passive_deletes=True
    )

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Deck(db.Model):
    """A deck. `status` walks draft -> processing -> (planned ->) processing -> ready | failed.

    `planned` is the optional pause after the planner has produced a work order and the
    user asked to review it before any cards are written.
    """

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
    # Planner output and run-level bookkeeping (phase, summary, cost) live here so a
    # single row describes the current generation without a join.
    run_json = db.Column(db.JSON, nullable=False, default=dict)
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    sources = db.relationship(
        "Source", backref="deck", cascade="all, delete-orphan", passive_deletes=True
    )
    cards = db.relationship(
        "Card", backref="deck", cascade="all, delete-orphan", passive_deletes=True
    )
    tasks = db.relationship(
        "PipelineTask", backref="deck", cascade="all, delete-orphan", passive_deletes=True
    )
    figures = db.relationship(
        "Figure", backref="deck", cascade="all, delete-orphan", passive_deletes=True
    )


class Source(db.Model):
    """One *unit* of the document map: a semantically coherent slice of the source.

    Historically this held cheat-sheet sections; it now holds the planner's view of the
    document (kind, density, prerequisites) and is what worker tasks read verbatim.
    """

    id = db.Column(db.Integer, primary_key=True)
    deck_id = db.Column(
        db.Integer, db.ForeignKey("deck.id", ondelete="CASCADE"), nullable=False, index=True
    )
    idx = db.Column(db.Integer, nullable=False)
    title = db.Column(db.String(200))
    text = db.Column(db.Text, nullable=False)
    hash = db.Column(db.String(64), nullable=False)
    # Document-map metadata (filled by the mapper).
    kind = db.Column(db.String(30), default="prose")
    density = db.Column(db.Integer, default=3)  # 1 (fluff) .. 5 (dense definitions/formulas)
    char_start = db.Column(db.Integer)
    char_end = db.Column(db.Integer)
    page_start = db.Column(db.Integer)
    page_end = db.Column(db.Integer)
    depends_on = db.Column(db.JSON, default=list)  # list of Source.idx this unit builds on
    skipped = db.Column(db.Boolean, default=False)
    skip_reason = db.Column(db.Text)
    summary = db.Column(db.Text)

    cards = db.relationship(
        "Card", backref="source", cascade="all, delete-orphan", passive_deletes=True
    )


class Figure(db.Model):
    """An image pulled out of an uploaded PDF, described by the vision model."""

    id = db.Column(db.Integer, primary_key=True)
    deck_id = db.Column(
        db.Integer, db.ForeignKey("deck.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id = db.Column(
        db.Integer, db.ForeignKey("source.id", ondelete="SET NULL"), index=True
    )
    page = db.Column(db.Integer)
    hash = db.Column(db.String(64), nullable=False)
    mime = db.Column(db.String(30), default="image/png")
    width = db.Column(db.Integer)
    height = db.Column(db.Integer)
    image = db.Column(db.LargeBinary, nullable=False)
    caption = db.Column(db.Text)
    description = db.Column(db.Text)
    kind = db.Column(db.String(30))
    useful = db.Column(db.Boolean)
    analysis_json = db.Column(db.JSON)


class Card(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    deck_id = db.Column(
        db.Integer, db.ForeignKey("deck.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id = db.Column(
        db.Integer, db.ForeignKey("source.id", ondelete="CASCADE"), index=True
    )
    task_id = db.Column(
        db.Integer, db.ForeignKey("pipeline_task.id", ondelete="SET NULL"), index=True
    )
    figure_id = db.Column(
        db.Integer, db.ForeignKey("figure.id", ondelete="SET NULL"), index=True
    )
    type = db.Column(db.String(20), nullable=False, index=True)
    front = db.Column(db.Text)
    back = db.Column(db.Text)
    cloze_text = db.Column(db.Text)
    extra = db.Column(db.Text)
    tags = db.Column(db.JSON, default=list)
    status = db.Column(db.String(20), nullable=False, default="ok", index=True)
    strategy = db.Column(db.String(40))
    difficulty = db.Column(db.Integer)  # 1 easy .. 3 hard, from the critic
    source_quote = db.Column(db.Text)  # verbatim span the card was written from
    critic_json = db.Column(db.JSON)
    order_key = db.Column(db.Integer, default=0, index=True)
    # Stable Anki note guid so review stats can be matched back after study.
    guid = db.Column(db.String(64), index=True)
    review_stats_json = db.Column(db.JSON)
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)


class PipelineTask(db.Model):
    """One node of the generation trace tree.

    Run -> phase -> task -> (subtask). Every LLM call hangs off a task via LLMRun.task_id,
    so the status page can render exactly what the planner decided and how each worker
    is doing, and the insights panel can attribute cost.
    """

    __tablename__ = "pipeline_task"

    id = db.Column(db.Integer, primary_key=True)
    deck_id = db.Column(
        db.Integer, db.ForeignKey("deck.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_id = db.Column(
        db.Integer, db.ForeignKey("pipeline_task.id", ondelete="CASCADE"), index=True
    )
    seq = db.Column(db.Integer, default=0)
    phase = db.Column(db.String(20), nullable=False, index=True)  # map|plan|write|critique|reconcile|coverage|figures|finish
    kind = db.Column(db.String(30), nullable=False)  # phase|task|subtask
    label = db.Column(db.String(200))
    strategy = db.Column(db.String(40))
    unit_ids = db.Column(db.JSON, default=list)  # Source.idx values
    model = db.Column(db.String(100))
    status = db.Column(db.String(20), nullable=False, default="queued", index=True)  # queued|running|done|failed|cached|skipped
    target_cards = db.Column(db.Integer)
    notes = db.Column(db.Text)
    cards_made = db.Column(db.Integer, default=0)
    cards_kept = db.Column(db.Integer, default=0)
    input_tokens = db.Column(db.Integer, default=0)
    output_tokens = db.Column(db.Integer, default=0)
    cost = db.Column(db.Float, default=0.0)
    error = db.Column(db.Text)
    result_json = db.Column(db.JSON)
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "parent_id": self.parent_id,
            "seq": self.seq,
            "phase": self.phase,
            "kind": self.kind,
            "label": self.label,
            "strategy": self.strategy,
            "unit_ids": self.unit_ids or [],
            "model": self.model,
            "status": self.status,
            "target_cards": self.target_cards,
            "notes": self.notes,
            "cards_made": self.cards_made or 0,
            "cards_kept": self.cards_kept or 0,
            "input_tokens": self.input_tokens or 0,
            "output_tokens": self.output_tokens or 0,
            "cost": self.cost or 0.0,
            "error": self.error,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class LLMRun(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    deck_id = db.Column(
        db.Integer, db.ForeignKey("deck.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id = db.Column(
        db.Integer, db.ForeignKey("source.id", ondelete="SET NULL"), index=True
    )
    task_id = db.Column(
        db.Integer, db.ForeignKey("pipeline_task.id", ondelete="SET NULL"), index=True
    )
    role = db.Column(db.String(30), index=True)
    model = db.Column(db.String(100))
    prompt_version = db.Column(db.String(50), index=True)
    input_tokens = db.Column(db.Integer)
    output_tokens = db.Column(db.Integer)
    cost_estimate = db.Column(db.Float)
    cached = db.Column(db.Boolean, default=False)
    request_json = db.Column(db.JSON)
    response_text = db.Column(db.Text)
    parsed_json = db.Column(db.JSON)
    error = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow)


class GenerationCache(db.Model):
    """Content-addressed cache of LLM results keyed on (role, model, prompt version, inputs).

    Regenerating a deck, or generating another deck from the same chapter, costs nothing
    for any task whose inputs are byte-identical.
    """

    __tablename__ = "generation_cache"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(64), unique=True, nullable=False, index=True)
    role = db.Column(db.String(30))
    model = db.Column(db.String(100))
    value_json = db.Column(db.JSON, nullable=False)
    usage_json = db.Column(db.JSON)
    hits = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=utcnow)
