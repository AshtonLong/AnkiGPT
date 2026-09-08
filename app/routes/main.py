import os
import secrets
import uuid
from functools import wraps

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import current_user
from werkzeug.utils import secure_filename

from ..extensions import db
from ..models import Card, Deck, LLMRun, Source, User
from ..services.pdf import extract_pdf_text
from ..services.validators import is_valid_cloze
from ..services.deckgen import CARD_PROMPT_VERSION, regenerate_source, improve_card
from ..services.export import export_deck as export_deck_file
from ..tasks import generate_deck_task

bp = Blueprint("main", __name__)


def auth_required(view):
    """Send anonymous users to the login page when AUTH_REQUIRED is on.

    Not `flask_login.login_required`: with AUTH_REQUIRED=false the app runs in demo
    mode against a local `demo@local` user and every view stays reachable.
    """

    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_app.config["AUTH_REQUIRED"] and not current_user.is_authenticated:
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)

    return wrapped


def get_actor():
    if current_app.config["AUTH_REQUIRED"]:
        return current_user
    user = User.query.filter_by(email="demo@local").first()
    if not user:
        user = User(email="demo@local")
        # Demo mode bypasses auth entirely; this password is never used for login,
        # so make it unguessable rather than a fixed "demo".
        user.set_password(secrets.token_urlsafe(32))
        db.session.add(user)
        db.session.commit()
    return user


def get_owned_deck(deck_id):
    """Load a deck only if it belongs to the current actor, else 404.

    Returning 404 (not 403) avoids leaking whether a given deck id exists.
    """
    actor = get_actor()
    return Deck.query.filter_by(id=deck_id, user_id=actor.id).first_or_404()


def get_owned_card(card_id):
    """Load a card only if its deck belongs to the current actor, else 404."""
    actor = get_actor()
    return (
        Card.query.join(Deck, Card.deck_id == Deck.id)
        .filter(Card.id == card_id, Deck.user_id == actor.id)
        .first_or_404()
    )


@bp.route("/")
def index():
    return render_template("index.html")


@bp.route("/decks")
@auth_required
def decks():
    user = get_actor()
    decks = Deck.query.filter_by(user_id=user.id).order_by(Deck.created_at.desc()).all()
    return render_template("decks.html", decks=decks)


@bp.route("/decks/<int:deck_id>/delete", methods=["POST"])
@auth_required
def delete_deck(deck_id):
    deck = get_owned_deck(deck_id)
    db.session.delete(deck)
    db.session.commit()
    flash("Deck deleted.", "info")
    return redirect(url_for("main.decks"))


def _parse_page(value):
    """Parse an optional 1-based page number from a form field; None if blank/invalid."""
    if not value:
        return None
    try:
        page = int(value)
    except (TypeError, ValueError):
        return None
    return page if page > 0 else None


@bp.route("/decks/new", methods=["GET", "POST"])
@auth_required
def new_deck():
    if request.method == "POST":
        title = request.form.get("title", "").strip() or "Untitled Deck"
        source_type = request.form.get("source_type")
        card_style = request.form.get("card_style") or current_app.config["DEFAULT_CARD_STYLE"]
        text_input = request.form.get("text_input", "").strip()
        source_text = ""
        if source_type == "text":
            source_text = text_input
        elif source_type == "pdf":
            pdf_file = request.files.get("pdf_file")
            if not pdf_file or not pdf_file.filename:
                flash("PDF file is required", "error")
                return render_template("deck_new.html")
            allowed = current_app.config["ALLOWED_UPLOAD_EXTENSIONS"]
            ext = pdf_file.filename.rsplit(".", 1)[-1].lower() if "." in pdf_file.filename else ""
            if ext not in allowed:
                flash("Only PDF files are supported.", "error")
                return render_template("deck_new.html")
            # Never trust the client filename. Store under a server-generated name to
            # prevent path traversal and cross-user collisions.
            safe_name = f"{uuid.uuid4().hex}_{secure_filename(pdf_file.filename)}"
            upload_path = os.path.join(current_app.config["UPLOAD_FOLDER"], safe_name)
            pdf_file.save(upload_path)
            try:
                start = _parse_page(request.form.get("page_start"))
                end = _parse_page(request.form.get("page_end"))
                source_text, _total_pages = extract_pdf_text(upload_path, start, end)
            except Exception:
                current_app.logger.exception("PDF extraction failed for %s", safe_name)
                source_text = ""
            finally:
                # Don't leave uploads accumulating on disk after extraction.
                try:
                    os.remove(upload_path)
                except OSError:
                    pass
        else:
            flash("Choose a source type.", "error")
            return render_template("deck_new.html")
        if not source_text:
            flash(
                "No text could be extracted. If this is a scanned PDF, it has no "
                "selectable text (OCR is not supported).",
                "error",
            )
            return render_template("deck_new.html")
        max_source_chars = current_app.config["MAX_SOURCE_CHARS"]
        if max_source_chars and len(source_text) > max_source_chars:
            source_text = source_text[:max_source_chars]
            flash(
                f"Source was truncated to {max_source_chars:,} characters to keep "
                "generation fast and affordable.",
                "info",
            )
        user = get_actor()
        deck = Deck(
            user_id=user.id,
            title=title,
            card_style=card_style,
            status="draft",
            source_type=source_type,
            source_text=source_text,
            settings_json={},
        )
        db.session.add(deck)
        db.session.commit()
        return redirect(url_for("main.preview_deck", deck_id=deck.id))
    return render_template("deck_new.html")


@bp.route("/decks/<int:deck_id>/preview", methods=["GET", "POST"])
@auth_required
def preview_deck(deck_id):
    deck = get_owned_deck(deck_id)
    if request.method == "POST":
        try:
            max_chars = int(request.form.get("max_chars") or 3500)
        except (TypeError, ValueError):
            max_chars = 3500
        max_chars = max(1000, min(max_chars, 8000))
        settings = {
            "focus": request.form.get("focus", ""),
            "exclude": request.form.get("exclude", ""),
            "glossary": request.form.get("glossary", ""),
            "max_chars": max_chars,
        }
        deck.settings_json = settings
        deck.status = "processing"
        db.session.commit()
        try:
            generate_deck_task.delay(deck.id)
        except Exception:
            # Broker unreachable: fall back to running inline so the deck still
            # generates, but log it so the operator knows async mode degraded.
            current_app.logger.warning(
                "Celery dispatch failed for deck %s; running inline.", deck.id, exc_info=True
            )
            generate_deck_task.apply(args=(deck.id,))
        return redirect(url_for("main.status", deck_id=deck.id))
    return render_template("deck_preview.html", deck=deck)


@bp.route("/decks/<int:deck_id>/status")
@auth_required
def status(deck_id):
    deck = get_owned_deck(deck_id)
    settings = deck.settings_json or {}
    total_sources = Source.query.filter_by(deck_id=deck_id).count()
    if deck.status == "processing" and settings.get("generation_stage") == "cheat_sheet":
        total_sources = int(settings.get("source_chunks") or 0)
        done_sources = int(settings.get("cheat_sheet_chunks_done") or 0)
        progress_label = "source chunks converted to cheat sheet"
    else:
        done_sources = min(
            LLMRun.query.filter_by(deck_id=deck_id, prompt_version=CARD_PROMPT_VERSION).count(),
            total_sources,
        )
        progress_label = "cheat sheet sections processed"
    failure_message = settings.get("last_error") or "Generation failed."
    template = "partials/status_panel.html" if request.args.get("partial") else "deck_status.html"
    return render_template(
        template,
        deck=deck,
        total_sources=total_sources,
        done_sources=done_sources,
        failure_message=failure_message,
        progress_label=progress_label,
    )


@bp.route("/decks/<int:deck_id>")
@auth_required
def deck_editor(deck_id):
    deck = get_owned_deck(deck_id)
    settings = deck.settings_json or {}
    auto_deleted = settings.get("auto_deleted_cards")
    legacy_dropped = settings.get("dropped_cards")
    flagged_count = auto_deleted if auto_deleted is not None else legacy_dropped
    if flagged_count:
        flash(
            f"{flagged_count} cards failed validation and were moved to Deleted. "
            "Filter by status=Deleted to review, fix, and restore them.",
            "info",
        )
        updated_settings = dict(deck.settings_json or {})
        updated_settings.pop("auto_deleted_cards", None)
        updated_settings.pop("dropped_cards", None)
        deck.settings_json = updated_settings
        db.session.commit()
    q = request.args.get("q", "").strip()
    card_type = request.args.get("type", "")
    # Named *_filter so it doesn't shadow the `status` view function in this module.
    status_filter = request.args.get("status", "")
    query = Card.query.filter_by(deck_id=deck_id)
    if q:
        like = f"%{q}%"
        query = query.filter(
            Card.front.ilike(like) | Card.back.ilike(like) | Card.cloze_text.ilike(like)
        )
    if card_type:
        query = query.filter_by(type=card_type)
    if status_filter:
        query = query.filter_by(status=status_filter)
    cards = query.order_by(Card.created_at.desc()).all()
    return render_template(
        "deck_editor.html",
        deck=deck,
        cards=cards,
        q=q,
        card_type=card_type,
        status=status_filter,
    )


@bp.route("/cards/<int:card_id>", methods=["POST"])
@auth_required
def update_card(card_id):
    card = get_owned_card(card_id)
    if card.type == "basic":
        card.front = request.form.get("front", "").strip()
        card.back = request.form.get("back", "").strip()
    else:
        card.cloze_text = request.form.get("cloze_text", "").strip()
        card.extra = request.form.get("extra", "").strip()
        if not is_valid_cloze(card.cloze_text):
            card.status = "needs_review"
        else:
            card.status = "ok"
    tags_raw = request.form.get("tags", "")
    card.tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    db.session.commit()
    return render_template("partials/card_row.html", card=card)


@bp.route("/cards/bulk", methods=["POST"])
@auth_required
def bulk_cards():
    actor = get_actor()
    action = request.form.get("action")
    ids = request.form.getlist("card_ids")
    if not ids:
        flash("No cards selected.", "error")
        return redirect(request.referrer or url_for("main.decks"))
    # Scope to cards in decks the actor owns — never operate on arbitrary ids.
    cards = (
        Card.query.join(Deck, Card.deck_id == Deck.id)
        .filter(Card.id.in_(ids), Deck.user_id == actor.id)
        .all()
    )
    affected = len(cards)
    if action == "delete":
        for card in cards:
            card.status = "deleted"
        message = f"Deleted {affected} cards."
    elif action == "tag":
        tag = request.form.get("tag", "").strip()
        for card in cards:
            tags = set(card.tags or [])
            if tag:
                tags.add(tag)
            card.tags = list(tags)
        message = f"Tagged {affected} cards." if tag else "No tag provided."
    elif action == "restore":
        for card in cards:
            card.status = "ok"
        message = f"Restored {affected} cards."
    elif action == "regenerate":
        source_ids = {card.source_id for card in cards if card.source_id}
        regenerated = 0
        for source_id in source_ids:
            try:
                regenerate_source(source_id)
                regenerated += 1
            except Exception:
                current_app.logger.exception("Regenerate failed for source %s", source_id)
        message = f"Regenerated {regenerated} source section(s)."
    else:
        flash("Unknown bulk action.", "error")
        return redirect(request.referrer or url_for("main.decks"))
    db.session.commit()
    flash(message, "info")
    return redirect(request.referrer or url_for("main.decks"))


@bp.route("/cards/<int:card_id>/improve", methods=["POST"])
@auth_required
def improve(card_id):
    card = get_owned_card(card_id)
    try:
        improve_card(card_id)
    except Exception:
        current_app.logger.exception("AI improve failed for card %s", card_id)
        db.session.rollback()
        response = render_template("partials/card_row.html", card=card)
        # Signal the client to show an error toast (handled in base.html).
        return response, 200, {"HX-Trigger": "improveError"}
    db.session.refresh(card)
    return render_template("partials/card_row.html", card=card)


@bp.route("/decks/<int:deck_id>/export", methods=["POST"])
@auth_required
def export_deck(deck_id):
    deck = get_owned_deck(deck_id)
    result = export_deck_file(deck.id)
    if not result:
        flash("No cards to export", "error")
        return redirect(url_for("main.deck_editor", deck_id=deck.id))
    file_obj, filename = result
    file_obj.seek(0)
    return send_file(file_obj, as_attachment=True, download_name=filename)
