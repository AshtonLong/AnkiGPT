import io
import os
import uuid
from collections import defaultdict

from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import current_user
from sqlalchemy import func
from werkzeug.utils import secure_filename

from ..extensions import db, login_manager
from ..models import Card, Deck, Figure, LLMRun, PipelineTask, Source
from ..services.billing import (
    CHARS_PER_PAGE,
    FREE_RERUNS,
    QuotaExceeded,
    allowance,
    billing_enabled,
    deck_char_limit,
    meter_generation,
    plan_for,
    run_charge,
)
from ..services.deckgen import improve_card, regenerate_source
from ..services.export import export_deck as export_deck_file
from ..services.pdf import extract_pdf_text
from ..services.pipeline import progress_for
from ..services.pipeline.feedback import ImportError_, apply_review_stats, coach_cards, read_review_stats
from ..services.pipeline.figures import extract_figures
from ..services.pipeline.planner import Plan
from ..services.pipeline.strategies import STRATEGIES
from ..services.pipeline.trace import PHASES
from ..services.validators import is_valid_cloze
from ..tasks import dispatch_generation

bp = Blueprint("main", __name__)

CARD_STYLES = ("basic", "cloze", "mixed")


@bp.before_request
def require_workspace_sign_in():
    """Every workspace route is private, including future routes and JSON endpoints."""
    if request.endpoint != "main.index" and not current_user.is_authenticated:
        return login_manager.unauthorized()


def get_actor():
    return current_user


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
def decks():
    user = get_actor()
    decks = Deck.query.filter_by(user_id=user.id).order_by(Deck.created_at.desc()).all()
    # Live (non-deleted) card count per deck in one query, for the deck grid.
    card_counts = dict(
        db.session.query(Card.deck_id, func.count(Card.id))
        .join(Deck, Card.deck_id == Deck.id)
        .filter(Deck.user_id == user.id, Card.status != "deleted")
        .group_by(Card.deck_id)
        .all()
    )
    return render_template("decks.html", decks=decks, card_counts=card_counts)


@bp.route("/decks/<int:deck_id>/delete", methods=["POST"])
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
def new_deck():
    if request.method == "POST":
        def invalid(message):
            return render_template("deck_new.html", form_error=message)

        title = request.form.get("title", "").strip()
        if not title:
            return invalid("Give your deck a title before continuing.")
        if len(title) > 200:
            return invalid("Keep the deck title to 200 characters or fewer.")
        source_type = request.form.get("source_type")
        card_style = request.form.get("card_style") or current_app.config["DEFAULT_CARD_STYLE"]
        if card_style not in CARD_STYLES:
            card_style = current_app.config["DEFAULT_CARD_STYLE"]
        text_input = request.form.get("text_input", "").strip()
        source_text = ""
        page_offsets = []
        figures = []
        if source_type == "text":
            source_text = text_input
            if not source_text:
                return invalid("Paste some source material before continuing.")
        elif source_type == "pdf":
            start = _parse_page(request.form.get("page_start"))
            end = _parse_page(request.form.get("page_end"))
            if any(request.form.get(key) and value is None for key, value in (("page_start", start), ("page_end", end))):
                return invalid("Page numbers must be whole numbers starting at 1.")
            if start and end and end < start:
                return invalid("End page must be the same as or after the start page.")
            pdf_file = request.files.get("pdf_file")
            if not pdf_file or not pdf_file.filename:
                return invalid("PDF file is required. Choose your PDF to continue.")
            allowed = current_app.config["ALLOWED_UPLOAD_EXTENSIONS"]
            ext = pdf_file.filename.rsplit(".", 1)[-1].lower() if "." in pdf_file.filename else ""
            if ext not in allowed:
                return invalid("Only PDF files are supported.")
            # Never trust the client filename. Store under a server-generated name to
            # prevent path traversal and cross-user collisions.
            safe_name = f"{uuid.uuid4().hex}_{secure_filename(pdf_file.filename)}"
            upload_path = os.path.join(current_app.config["UPLOAD_FOLDER"], safe_name)
            pdf_file.save(upload_path)
            try:
                start = _parse_page(request.form.get("page_start"))
                end = _parse_page(request.form.get("page_end"))
                source_text, _total_pages, page_offsets = extract_pdf_text(upload_path, start, end)
                if current_app.config.get("PIPELINE_FIGURES_ENABLED", True):
                    figures = extract_figures(upload_path, start, end, max_figures=current_app.config.get("PIPELINE_MAX_FIGURES", 24))
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
            return invalid("Choose a source type.")
        if not source_text:
            return invalid(
                "No text could be extracted. If this is a scanned PDF, it has no "
                "selectable text (OCR is not supported). Choose a text-based PDF or paste your notes.",
            )
        user = get_actor()
        max_source_chars = deck_char_limit(user)
        if max_source_chars and len(source_text) > max_source_chars:
            source_text = source_text[:max_source_chars]
            page_offsets = [po for po in page_offsets if po[1] < max_source_chars]
            plan = plan_for(user)
            if billing_enabled() and max_source_chars == plan.max_deck_chars and plan.key != "max":
                flash(
                    f"Your {plan.name} plan covers decks up to {plan.pages_per_deck} pages "
                    f"({max_source_chars:,} characters), so the source was trimmed to fit. "
                    "Pick a page range, or upgrade under Plan & billing for longer decks.",
                    "info",
                )
            else:
                flash(
                    f"Source was truncated to {max_source_chars:,} characters to keep "
                    "generation fast and affordable.",
                    "info",
                )
        deck = Deck(
            user_id=user.id,
            title=title,
            card_style=card_style,
            status="draft",
            source_type=source_type,
            source_text=source_text,
            settings_json={"page_offsets": page_offsets} if page_offsets else {},
            run_json={},
        )
        db.session.add(deck)
        db.session.commit()
        if figures:
            for f in figures:
                db.session.add(Figure(deck_id=deck.id, page=f["page"], hash=f["hash"], mime="image/png",
                                      width=f["width"], height=f["height"], image=f["image"]))
            db.session.commit()
        return redirect(url_for("main.preview_deck", deck_id=deck.id))
    return render_template("deck_new.html")


def _read_settings_form(deck):
    settings = dict(deck.settings_json or {})
    settings["focus"] = request.form.get("focus", "").strip()
    settings["exclude"] = request.form.get("exclude", "").strip()
    settings["glossary"] = request.form.get("glossary", "").strip()
    settings["exam_context"] = request.form.get("exam_context", "").strip()
    target = request.form.get("target_cards", "").strip().lower()
    if target in ("", "auto", "0"):
        settings["target_cards"] = None
    else:
        try:
            settings["target_cards"] = max(5, min(600, int(target)))
        except ValueError:
            settings["target_cards"] = None
    settings["review_plan"] = request.form.get("review_plan") == "on"
    settings["use_figures"] = "on" in request.form.getlist("use_figures") if "use_figures" in request.form else True
    settings["card_style"] = deck.card_style
    settings.pop("max_chars", None)
    return settings


def _meter_or_refuse(deck):
    """Charge this run to the monthly allowance. On QuotaExceeded, flash why and return
    False; the caller commits either way so the user's form settings are kept."""
    try:
        meter_generation(get_actor(), deck)
    except QuotaExceeded as exc:
        a = exc.allowance
        flash(
            f"This run needs {exc.needed} pages and your {a.plan.name} plan has {a.remaining} "
            f"left this month. Upgrade under Plan & billing, or wait for your pages to reset on {a.reset_label}.",
            "error",
        )
        return False
    return True


@bp.route("/decks/<int:deck_id>/preview", methods=["GET", "POST"])
def preview_deck(deck_id):
    deck = get_owned_deck(deck_id)
    if request.method == "POST":
        deck.settings_json = _read_settings_form(deck)
        if not _meter_or_refuse(deck):
            db.session.commit()
            return redirect(url_for("main.preview_deck", deck_id=deck.id))
        deck.status = "processing"
        deck.run_json = {}
        db.session.commit()
        dispatch_generation(deck.id)
        return redirect(url_for("main.status", deck_id=deck.id))
    figure_count = Figure.query.filter_by(deck_id=deck.id).count()
    figures = Figure.query.filter_by(deck_id=deck.id).order_by(Figure.page, Figure.id).limit(6).all()
    return render_template(
        "deck_preview.html", deck=deck, figure_count=figure_count, figures=figures,
        run_pages=run_charge(deck), allowance=allowance(get_actor()), chars_per_page=CHARS_PER_PAGE, free_reruns=FREE_RERUNS,
    )


def _status_payload(deck):
    pct, phases, tasks = progress_for(deck)
    run = deck.run_json or {}
    phase_rows = []
    for key, label in PHASES:
        p = phases.get(key)
        phase_rows.append({
            "key": key, "label": label,
            "status": p["status"] if p else "pending",
            "done": p["done"] if p else 0, "total": p["total"] if p else 0,
            "cost": (p["node"].cost or 0.0) if p else 0.0,
        })
    task_rows = [t.to_dict() for t in tasks if t.kind != "phase"]
    return {
        "status": deck.status,
        "pct": pct,
        "phase": run.get("phase"),
        "phases": phase_rows,
        "tasks": task_rows,
        "totals": run.get("totals") or {},
        "summary": run.get("summary"),
        "stats": run.get("stats"),
        "last_error": run.get("last_error"),
        "cards_ok": Card.query.filter_by(deck_id=deck.id, status="ok").count(),
        "urls": {
            "editor": url_for("main.deck_editor", deck_id=deck.id),
            "plan": url_for("main.plan_deck", deck_id=deck.id),
        },
    }


@bp.route("/decks/<int:deck_id>/status")
def status(deck_id):
    deck = get_owned_deck(deck_id)
    if deck.status == "planned":
        return redirect(url_for("main.plan_deck", deck_id=deck.id))
    payload = _status_payload(deck)
    failure_message = payload.get("last_error") or "Generation failed."
    return render_template("deck_status.html", deck=deck, payload=payload, failure_message=failure_message)


@bp.route("/decks/<int:deck_id>/progress.json")
def progress_json(deck_id):
    deck = get_owned_deck(deck_id)
    return jsonify(_status_payload(deck))


@bp.route("/decks/<int:deck_id>/plan", methods=["GET", "POST"])
def plan_deck(deck_id):
    deck = get_owned_deck(deck_id)
    run = dict(deck.run_json or {})
    plan = Plan.from_dict(run.get("plan") or {})
    units = Source.query.filter_by(deck_id=deck.id).order_by(Source.idx).all()
    if request.method == "POST":
        action = request.form.get("action", "run")
        if action == "replan":
            if not _meter_or_refuse(deck):
                return redirect(url_for("main.plan_deck", deck_id=deck.id))
            settings = dict(deck.settings_json or {})
            settings["review_plan"] = True
            deck.settings_json = settings
            deck.status = "processing"
            deck.run_json = {}
            db.session.commit()
            dispatch_generation(deck.id)
            return redirect(url_for("main.status", deck_id=deck.id))
        if deck.status != "planned":
            flash("This deck is not waiting for plan review.", "error")
            return redirect(url_for("main.status", deck_id=deck.id))
        kept = []
        for task in plan.tasks:
            if request.form.get(f"skip_{task.id}") == "on":
                continue
            try:
                target = int(request.form.get(f"target_{task.id}") or task.target_cards)
            except ValueError:
                target = task.target_cards
            task.target_cards = max(1, min(60, target))
            strategy = request.form.get(f"strategy_{task.id}") or task.strategy
            if strategy in STRATEGIES:
                task.strategy = strategy
            task.notes = (request.form.get(f"notes_{task.id}") or task.notes or "")[:1500]
            kept.append(task)
        if not kept:
            flash("Keep at least one task, or re-plan.", "error")
            return redirect(url_for("main.plan_deck", deck_id=deck.id))
        plan.tasks = kept
        run["plan"] = plan.to_dict()
        deck.run_json = run
        deck.status = "processing"
        db.session.commit()
        dispatch_generation(deck.id, resume_from_plan=True)
        return redirect(url_for("main.status", deck_id=deck.id))
    unit_by_idx = {u.idx: u for u in units}
    figure_count = Figure.query.filter_by(deck_id=deck.id).count()
    return render_template(
        "deck_plan.html", deck=deck, plan=plan, units=units, unit_by_idx=unit_by_idx, run=run,
        strategies=STRATEGIES, figure_count=figure_count,
    )


def _insights(deck):
    run = deck.run_json or {}
    tasks = PipelineTask.query.filter_by(deck_id=deck.id).order_by(PipelineTask.seq).all()
    by_phase = defaultdict(lambda: {"calls": 0, "cost": 0.0, "input": 0, "output": 0, "cached": 0})
    for t in tasks:
        if t.kind == "phase":
            continue
        row = by_phase[t.phase]
        row["calls"] += 1
        row["cost"] += t.cost or 0.0
        row["input"] += t.input_tokens or 0
        row["output"] += t.output_tokens or 0
        if t.status == "cached":
            row["cached"] += 1
    return {"run": run, "by_phase": dict(by_phase), "tasks": tasks}


@bp.route("/decks/<int:deck_id>")
def deck_editor(deck_id):
    deck = get_owned_deck(deck_id)
    run = dict(deck.run_json or {})
    flagged = run.get("flagged")
    if flagged:
        flash(
            f"{flagged} cards were dropped or flagged by validation and the critic. "
            "Filter by status to review, fix, and restore them.",
            "info",
        )
        run.pop("flagged", None)
        deck.run_json = run
        db.session.commit()
    q = request.args.get("q", "").strip()
    card_type = request.args.get("type", "")
    # Named *_filter so it doesn't shadow the `status` view function in this module.
    status_filter = request.args.get("status", "")
    strategy_filter = request.args.get("strategy", "")
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
    if strategy_filter == "struggling":
        cards = [c for c in query.order_by(Card.order_key, Card.id).all() if (c.review_stats_json or {}).get("struggling")]
    else:
        if strategy_filter:
            query = query.filter_by(strategy=strategy_filter)
        cards = query.order_by(Card.order_key, Card.id).all()
    if request.args.get("view") == "coach":
        cards = [c for c in cards if (c.review_stats_json or {}).get("struggling") or (c.critic_json or {}).get("coach")]
        if not status_filter:
            cards = [c for c in cards if c.status != "deleted"]
    strategies_used = [s for (s,) in db.session.query(Card.strategy).filter_by(deck_id=deck_id).distinct().all() if s]
    struggling = sum(1 for c in Card.query.filter_by(deck_id=deck_id).filter(Card.status != "deleted").all() if (c.review_stats_json or {}).get("struggling"))
    units = Source.query.filter_by(deck_id=deck_id).order_by(Source.idx).all()
    return render_template(
        "deck_editor.html",
        deck=deck,
        cards=cards,
        q=q,
        card_type=card_type,
        status=status_filter,
        strategy=strategy_filter,
        strategies_used=strategies_used,
        struggling=struggling,
        insights=_insights(deck),
        units=units,
        phases=PHASES,
    )


@bp.route("/figures/<int:figure_id>.png")
def figure_image(figure_id):
    actor = get_actor()
    fig = (
        Figure.query.join(Deck, Figure.deck_id == Deck.id)
        .filter(Figure.id == figure_id, Deck.user_id == actor.id)
        .first_or_404()
    )
    return Response(fig.image, mimetype=fig.mime or "image/png")


@bp.route("/cards/<int:card_id>", methods=["POST"])
def update_card(card_id):
    card = get_owned_card(card_id)
    if card.type == "basic":
        card.front = request.form.get("front", "").strip()
        card.back = request.form.get("back", "").strip()
        if card.status == "needs_review":
            card.status = "ok"
    else:
        card.cloze_text = request.form.get("cloze_text", "").strip()
        card.extra = request.form.get("extra", "").strip()
        if card.status == "deleted":
            pass  # Editing a deleted card must not implicitly restore it.
        elif not is_valid_cloze(card.cloze_text):
            card.status = "needs_review"
        else:
            card.status = "ok"
    tags_raw = request.form.get("tags", "")
    card.tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    db.session.commit()
    return render_template("partials/card_row.html", card=card)


@bp.route("/cards/bulk", methods=["POST"])
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
        message = f"Regenerated {regenerated} unit(s)."
    elif action == "coach":
        deck_ids = {card.deck_id for card in cards}
        total = {"rewritten": 0, "split": 0, "kept": 0}
        for deck_id in deck_ids:
            try:
                result = coach_cards(deck_id, [c.id for c in cards if c.deck_id == deck_id]) or {}
                for k in total:
                    total[k] += result.get(k, 0)
            except Exception:
                current_app.logger.exception("Coach failed for deck %s", deck_id)
        message = f"Coach: {total['rewritten']} rewritten, {total['split']} split, {total['kept']} kept as-is. Rewritten cards are marked Needs review."
    else:
        flash("Unknown bulk action.", "error")
        return redirect(request.referrer or url_for("main.decks"))
    db.session.commit()
    flash(message, "info")
    return redirect(request.referrer or url_for("main.decks"))


@bp.route("/cards/<int:card_id>/improve", methods=["POST"])
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


@bp.route("/decks/<int:deck_id>/reviews", methods=["POST"])
def import_reviews(deck_id):
    deck = get_owned_deck(deck_id)
    def result(message, ok=False, matched=0, struggling=0):
        destination = url_for("main.deck_editor", deck_id=deck.id, view="coach")
        if request.accept_mimetypes.best == "application/json":
            return jsonify(ok=ok, message=message, matched=matched, struggling=struggling, url=destination), (200 if ok else 422)
        flash(message, "success" if ok else "error")
        return redirect(destination)

    upload = request.files.get("anki_package")
    if not upload or not upload.filename:
        return result("Choose an .apkg or .colpkg exported from Anki.")
    try:
        stats = read_review_stats(upload.read())
    except ImportError_ as exc:
        return result(str(exc))
    except Exception:
        current_app.logger.exception("Review import failed for deck %s", deck.id)
        return result("Could not read that Anki package.")
    matched, struggling = apply_review_stats(deck.id, stats)
    if not matched:
        return result("No cards from this deck were found in that package. Export this deck first, study it in Anki, then export it back with scheduling information.")
    return result(f"Imported review history for {matched} cards · {struggling} struggling.", True, matched, struggling)


@bp.route("/decks/<int:deck_id>/coach", methods=["POST"])
def coach(deck_id):
    deck = get_owned_deck(deck_id)
    try:
        result = coach_cards(deck.id) or {}
    except Exception:
        current_app.logger.exception("Coach failed for deck %s", deck.id)
        flash("The coach pass failed. Check the server log.", "error")
        return redirect(url_for("main.deck_editor", deck_id=deck.id))
    if not result.get("cards"):
        flash("No struggling cards to coach. Import review history first.", "info")
    else:
        flash(f"Coach: {result['rewritten']} rewritten, {result['split']} split, {result['kept']} kept. Rewritten cards are marked Needs review.", "success")
    return redirect(url_for("main.deck_editor", deck_id=deck.id, status="needs_review", view="coach"))


@bp.route("/decks/<int:deck_id>/export", methods=["POST"])
def export_deck(deck_id):
    deck = get_owned_deck(deck_id)
    result = export_deck_file(deck.id)
    if not result:
        flash("No cards to export", "error")
        return redirect(url_for("main.deck_editor", deck_id=deck.id))
    file_obj, filename = result
    file_obj.seek(0)
    return send_file(file_obj, as_attachment=True, download_name=filename)
