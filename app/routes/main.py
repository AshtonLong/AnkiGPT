import os
from flask import Blueprint, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user
from ..extensions import db
from ..models import Card, Deck, LLMRun, Source, User
from ..services.pdf import extract_pdf_text
from ..services.validators import is_valid_cloze
from ..services.deckgen import regenerate_source, improve_card
from ..services.export import export_deck as export_deck_file
from ..tasks import generate_deck_task

bp = Blueprint("main", __name__)


def guard_auth():
    if current_app.config["AUTH_REQUIRED"] and not current_user.is_authenticated:
        return redirect(url_for("auth.login"))
    return None


def get_actor():
    if current_app.config["AUTH_REQUIRED"]:
        return current_user
    user = User.query.filter_by(email="demo@local").first()
    if not user:
        user = User(email="demo@local")
        user.set_password("demo")
        db.session.add(user)
        db.session.commit()
    return user


@bp.route("/")
def index():
    return render_template("index.html")


@bp.route("/decks")
def decks():
    redirect_resp = guard_auth()
    if redirect_resp:
        return redirect_resp
    user = get_actor()
    decks = Deck.query.filter_by(user_id=user.id).order_by(Deck.created_at.desc()).all()
    return render_template("decks.html", decks=decks)


@bp.route("/decks/<int:deck_id>/delete", methods=["POST"])
def delete_deck(deck_id):
    redirect_resp = guard_auth()
    if redirect_resp:
        return redirect_resp
    deck = Deck.query.get_or_404(deck_id)
    user = get_actor()
    if deck.user_id != user.id:
        flash("Not authorized to delete this deck", "error")
        return redirect(url_for("main.decks"))
    db.session.delete(deck)
    db.session.commit()
    return redirect(url_for("main.decks"))


@bp.route("/decks/new", methods=["GET", "POST"])
def new_deck():
    redirect_resp = guard_auth()
    if redirect_resp:
        return redirect_resp
    if request.method == "POST":
        title = request.form.get("title", "").strip() or "Untitled Deck"
        source_type = request.form.get("source_type")
        card_style = request.form.get("card_style") or current_app.config["DEFAULT_CARD_STYLE"]
        text_input = request.form.get("text_input", "").strip()
        page_start = request.form.get("page_start")
        page_end = request.form.get("page_end")
        source_text = ""
        if source_type == "text":
            source_text = text_input
        elif source_type == "pdf":
            pdf_file = request.files.get("pdf_file")
            if not pdf_file:
                flash("PDF file is required", "error")
                return render_template("deck_new.html")
            filename = pdf_file.filename
            upload_path = os.path.join(current_app.config["UPLOAD_FOLDER"], filename)
            pdf_file.save(upload_path)
            start = int(page_start) if page_start else None
            end = int(page_end) if page_end else None
            source_text, total_pages = extract_pdf_text(upload_path, start, end)
        if not source_text:
            flash("No text could be extracted", "error")
            return render_template("deck_new.html")
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
def preview_deck(deck_id):
    redirect_resp = guard_auth()
    if redirect_resp:
        return redirect_resp
    deck = Deck.query.get_or_404(deck_id)
    if request.method == "POST":
        settings = {
            "focus": request.form.get("focus", ""),
            "exclude": request.form.get("exclude", ""),
            "glossary": request.form.get("glossary", ""),
            "max_chars": int(request.form.get("max_chars") or 3500),
        }
        deck.settings_json = settings
        db.session.commit()
        try:
            generate_deck_task.delay(deck.id)
        except Exception:
            generate_deck_task.apply(args=(deck.id,))
        return redirect(url_for("main.status", deck_id=deck.id))
    return render_template("deck_preview.html", deck=deck)


@bp.route("/decks/<int:deck_id>/status")
def status(deck_id):
    redirect_resp = guard_auth()
    if redirect_resp:
        return redirect_resp
    deck = Deck.query.get_or_404(deck_id)
    total_sources = Source.query.filter_by(deck_id=deck_id).count()
    done_sources = min(LLMRun.query.filter_by(deck_id=deck_id).count(), total_sources)
    failure_message = (deck.settings_json or {}).get("last_error") or "Generation failed."
    return render_template(
        "deck_status.html",
        deck=deck,
        total_sources=total_sources,
        done_sources=done_sources,
        failure_message=failure_message,
    )


@bp.route("/decks/<int:deck_id>")
def deck_editor(deck_id):
    redirect_resp = guard_auth()
    if redirect_resp:
        return redirect_resp
    deck = Deck.query.get_or_404(deck_id)
    dropped = (deck.settings_json or {}).get("dropped_cards")
    if dropped:
        flash(f"{dropped} cards were dropped during generation checks.", "info")
        updated_settings = dict(deck.settings_json or {})
        updated_settings.pop("dropped_cards", None)
        deck.settings_json = updated_settings
        db.session.commit()
    q = request.args.get("q", "").strip()
    card_type = request.args.get("type", "")
    status = request.args.get("status", "")
    query = Card.query.filter_by(deck_id=deck_id)
    if q:
        like = f"%{q}%"
        query = query.filter((Card.front.ilike(like)) | (Card.back.ilike(like)) | (Card.cloze_text.ilike(like)))
    if card_type:
        query = query.filter_by(type=card_type)
    if status:
        query = query.filter_by(status=status)
    cards = query.order_by(Card.created_at.desc()).all()
    return render_template("deck_editor.html", deck=deck, cards=cards, q=q, card_type=card_type, status=status)


@bp.route("/cards/<int:card_id>", methods=["POST"])
def update_card(card_id):
    redirect_resp = guard_auth()
    if redirect_resp:
        return redirect_resp
    card = Card.query.get_or_404(card_id)
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
def bulk_cards():
    redirect_resp = guard_auth()
    if redirect_resp:
        return redirect_resp
    action = request.form.get("action")
    ids = request.form.getlist("card_ids")
    cards = Card.query.filter(Card.id.in_(ids)).all()
    if action == "delete":
        for card in cards:
            card.status = "deleted"
    elif action == "tag":
        tag = request.form.get("tag", "").strip()
        for card in cards:
            tags = set(card.tags or [])
            if tag:
                tags.add(tag)
            card.tags = list(tags)
    elif action == "restore":
        for card in cards:
            card.status = "ok"
    elif action == "regenerate":
        source_ids = {card.source_id for card in cards if card.source_id}
        for source_id in source_ids:
            regenerate_source(source_id)
    db.session.commit()
    return redirect(request.referrer or url_for("main.decks"))


@bp.route("/cards/<int:card_id>/improve", methods=["POST"])
def improve(card_id):
    redirect_resp = guard_auth()
    if redirect_resp:
        return redirect_resp
    improve_card(card_id)
    card = Card.query.get_or_404(card_id)
    return render_template("partials/card_row.html", card=card)


@bp.route("/decks/<int:deck_id>/export", methods=["POST"])
def export_deck(deck_id):
    redirect_resp = guard_auth()
    if redirect_resp:
        return redirect_resp
    result = export_deck_file(deck_id)
    if not result:
        flash("No cards to export", "error")
        return redirect(url_for("main.deck_editor", deck_id=deck_id))
    file_obj, filename = result
    file_obj.seek(0)
    return send_file(file_obj, as_attachment=True, download_name=filename)
