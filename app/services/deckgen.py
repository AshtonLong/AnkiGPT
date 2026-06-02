import logging

from flask import current_app
from .chunking import clean_text, chunk_text, hash_text
from .llm import (
    CARD_RESPONSE_FORMAT,
    OpenRouterError,
    extract_json,
    message_content,
    openrouter_chat,
    repair_json,
)
from .schemas import ChunkSchema
from .validators import is_valid_cloze, normalize_text, normalize_math, is_math_valid, is_in_scope
from ..extensions import db
from ..models import Card, Deck, LLMRun, Source

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v5-cheat-sheet-pipeline"
CHEAT_SHEET_PROMPT_VERSION = f"{PROMPT_VERSION}:cheat_sheet"
CARD_PROMPT_VERSION = f"{PROMPT_VERSION}:cards"

TERMINAL_MARKERS = ("insufficient", "credit", "quota", "billing", "payment")

# Static instruction blocks live in the SYSTEM message so they form an identical,
# cacheable prefix across every chunk of a deck run. Only the variable chunk text
# goes in the user message (placed last), which maximizes prompt-cache hits.
CHEAT_SHEET_RULES = "\n".join(
    [
        "You create dense, source-grounded exam cheat sheets. Return Markdown only.",
        "Convert the source material into a dense exam cheat sheet section.",
        "Include only information that would be useful for a student on an exam.",
        "Be comprehensive: preserve definitions, formulas, variable meanings, procedures, constraints, contrasts, edge cases, and common pitfalls.",
        "Keep the content grounded in the source. Do not add facts that are not supported by the source.",
        "Use clear Markdown headings, bullets, and compact lists. Avoid wide Markdown tables.",
        "Write formulas with \\( ... \\) inline math and \\[ ... \\] display math.",
        "Remove filler, duplicated prose, anecdotes, generic examples, and low-value trivia.",
        "If source details are uncertain or incomplete, state only what the source supports.",
        "Return only the Markdown cheat sheet section. Do not create flashcards.",
    ]
)

CARD_RULES = "\n".join(
    [
        "You generate Anki flashcards from a Markdown cheat sheet section. Output strict JSON only, no prose.",
        "Scope: ONLY use facts explicitly stated in the cheat sheet section. The cheat sheet is the only allowed source; do not use outside knowledge.",
        "Coverage: make cards for exam-useful concepts, formulas, procedures, constraints, contrasts, and pitfalls in the cheat sheet.",
        "Minimum-information principle: one fact/idea/formula/step per card. Never put two independent facts on one card.",
        "If a fact has a 'because/therefore' or cause/effect, split it into separate cards.",
        "Keep answers short: 1-2 sentences or 1-3 concise bullets unless absolutely necessary.",
        "Cards are self-contained: no references to tables, figures, diagrams, or 'the text'.",
        "Avoid ambiguity: include the subject, scope, and conditions; no vague pronouns.",
        "Basic cards: a concise question -> a concise answer.",
        "Cloze cards: use {{c1::...}} with 1-2 deletions. Cloze the discriminating detail, NOT the topic word.",
        "Do not make a cloze guessable: the surrounding text must not give the answer away, and do not cloze the only capitalized term or the grammatical subject of the sentence.",
        "Do not cloze one item out of a short guessable list, and avoid clozing numbers/dates unless the number itself is the point.",
        "If a list is long, split it into several targeted cards instead of one heavy list card. Use a full-list card only for short lists meant to be memorized as a unit.",
        "Math: only \\( ... \\) inline and \\[ ... \\] display. Output plain text with minimal HTML; no Markdown tables.",
        "Skip low-value recall and anything not meaningfully testable.",
    ]
)

CARD_SCHEMA_HINT = (
    'Return only JSON of the form: {"cards": [{"type": "basic"|"cloze", '
    '"front": string|null, "back": string|null, "cloze_text": string|null, '
    '"extra": string|null, "tags": [string]}]}. '
    "For basic cards set front and back (cloze_text/extra null). "
    "For cloze cards set cloze_text (front/back null)."
)

CARD_FEWSHOT = (
    "Example of a GOOD cloze: {{c1::Mitochondria}} produce ATP via oxidative phosphorylation "
    "-> better: ATP is produced via oxidative phosphorylation in the {{c1::mitochondria}} "
    "(the discriminating detail is clozed, not the topic word).\n"
    "Example of a BAD card: 'List everything about the cell cycle.' (not atomic, not testable)."
)

IMPROVE_BASIC_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "improved_basic_card",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"front": {"type": "string"}, "back": {"type": "string"}},
            "required": ["front", "back"],
        },
    },
}

IMPROVE_CLOZE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "improved_cloze_card",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"cloze_text": {"type": "string"}, "extra": {"type": "string"}},
            "required": ["cloze_text", "extra"],
        },
    },
}


def tagify(text):
    return "".join([c if c.isalnum() or c in ("-", "_") else "_" for c in text.lower()]).strip("_")


def _llm_config():
    """Single source of truth for the per-call LLM settings."""
    return {
        "model": current_app.config["OPENROUTER_MODEL"],
        "api_key": current_app.config["OPENROUTER_API_KEY"],
        "site_url": current_app.config["OPENROUTER_SITE_URL"],
        "app_name": current_app.config["OPENROUTER_APP_NAME"],
        "max_retries": int(current_app.config.get("OPENROUTER_MAX_RETRIES", 2)),
        "backoff_seconds": float(current_app.config.get("OPENROUTER_RETRY_BACKOFF_SECONDS", 1.5)),
        "timeout_seconds": float(current_app.config.get("OPENROUTER_TIMEOUT_SECONDS", 120)),
        "max_tokens": int(current_app.config.get("OPENROUTER_MAX_TOKENS", 4000)),
    }


def _chat(cfg, messages, temperature=0.2, response_format=None):
    return openrouter_chat(
        messages,
        cfg["model"],
        cfg["api_key"],
        cfg["site_url"],
        cfg["app_name"],
        temperature=temperature,
        max_retries=cfg["max_retries"],
        backoff_seconds=cfg["backoff_seconds"],
        timeout_seconds=cfg["timeout_seconds"],
        response_format=response_format,
        max_tokens=cfg["max_tokens"],
    )


def build_cheat_sheet_prompt(chunk_title, chunk_text, settings, chunk_number, total_chunks):
    focus = settings.get("focus", "")
    exclude = settings.get("exclude", "")
    glossary = settings.get("glossary", "")
    # System: static rules + deck-level settings (identical across chunks -> cacheable).
    system = "\n".join(
        [
            CHEAT_SHEET_RULES,
            "",
            f"Focus: {focus or 'all exam-useful concepts, formulas, and procedures'}",
            f"Exclude: {exclude or 'none'}",
            f"Must-include terms: {glossary or 'none'}",
        ]
    )
    # User: only the variable chunk, placed last.
    user_prompt = (
        f"Create an exam cheat sheet section from this source chunk "
        f"({chunk_number} of {total_chunks}).\n"
        f"Title: {chunk_title or 'Untitled'}\n\n"
        f"Source:\n{chunk_text}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_prompt},
    ]


def build_prompt(chunk_title, chunk_text, settings, card_style):
    focus = settings.get("focus", "")
    exclude = settings.get("exclude", "")
    glossary = settings.get("glossary", "")
    system = "\n".join(
        [
            CARD_RULES,
            "",
            CARD_FEWSHOT,
            "",
            CARD_SCHEMA_HINT,
            "",
            f"Preferred card style: {card_style}",
            f"Focus: {focus or 'general coverage'}",
            f"Exclude: {exclude or 'none'}",
            f"Glossary: {glossary or 'none'}",
        ]
    )
    user_prompt = (
        f"Generate Anki cards from this cheat sheet section.\n"
        f"Title: {chunk_title or 'Untitled'}\n\n"
        f"Cheat sheet section:\n{chunk_text}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_prompt},
    ]


def normalize_cheat_sheet_section(content, chunk_number):
    section = clean_text(content)
    if not section:
        return ""
    if not section.lstrip().startswith("#"):
        section = f"## Cheat Sheet Section {chunk_number}\n\n{section}"
    return section


def parse_cards(raw_text, cfg):
    try:
        data = extract_json(raw_text)
    except Exception:
        # With structured outputs this should rarely trigger; kept as a last resort.
        data = repair_json(
            raw_text,
            cfg["model"],
            cfg["api_key"],
            cfg["site_url"],
            cfg["app_name"],
            max_retries=cfg["max_retries"],
            backoff_seconds=cfg["backoff_seconds"],
            timeout_seconds=cfg["timeout_seconds"],
        )
    parsed = ChunkSchema.model_validate(data)
    return parsed.cards, data


def normalize_card(card):
    if card.type == "basic":
        front = normalize_math(normalize_text(card.front))
        back = normalize_math(normalize_text(card.back))
        return {"type": "basic", "front": front, "back": back}
    cloze_text = normalize_math(normalize_text(card.cloze_text))
    extra = normalize_math(normalize_text(card.extra or ""))
    return {"type": "cloze", "cloze_text": cloze_text, "extra": extra}


def card_text_for_scope(normalized):
    if normalized["type"] == "basic":
        return f"{normalized.get('front','')} {normalized.get('back','')}"
    return f"{normalized.get('cloze_text','')} {normalized.get('extra','')}"


def validation_issues(normalized, source_text):
    issues = []
    content = card_text_for_scope(normalized)
    if normalized["type"] == "cloze" and not is_valid_cloze(normalized["cloze_text"]):
        issues.append("invalid_cloze")
    if not is_math_valid(content):
        issues.append("invalid_math")
    if not is_in_scope(content, source_text):
        issues.append("out_of_scope")
    return issues


def apply_validation_tags(tags, issues):
    if not issues:
        return list(tags)
    tagged = list(tags)
    tagged.append("auto_deleted")
    for issue in issues:
        tagged.append(f"validation:{issue}")
    deduped = []
    seen = set()
    for tag in tagged:
        if not tag or tag in seen:
            continue
        seen.add(tag)
        deduped.append(tag)
    return deduped


def _build_cards_for_source(cards, source):
    """Turn parsed/validated card models into Card rows for a given source."""
    rows = []
    auto_deleted = 0
    for card in cards:
        normalized = normalize_card(card)
        issues = validation_issues(normalized, source.text)
        status = "deleted" if issues else "ok"
        if issues:
            auto_deleted += 1
        tags = list(card.tags or [])
        tags.append(f"section:{source.idx + 1}")
        if source.title:
            tags.append(f"title:{tagify(source.title)}")
        tags = apply_validation_tags(tags, issues)
        rows.append(
            Card(
                deck_id=source.deck_id,
                source_id=source.id,
                type=normalized["type"],
                front=normalized.get("front"),
                back=normalized.get("back"),
                cloze_text=normalized.get("cloze_text"),
                extra=normalized.get("extra"),
                tags=tags,
                status=status,
            )
        )
    return rows, auto_deleted


def format_generation_error(exc):
    if isinstance(exc, OpenRouterError):
        status = exc.status_code
        detail = (exc.response_body or "").lower()
        if status == 429:
            if any(marker in detail for marker in TERMINAL_MARKERS):
                return "OpenRouter credits/quota were exhausted while processing this deck."
            return "OpenRouter rate limit was hit while processing this deck. Wait a minute and try again."
        if status in (401, 403):
            return "OpenRouter authentication failed. Check your API key and model access."
        if status == 400:
            if "context" in detail or "token" in detail or "too long" in detail:
                return "OpenRouter rejected this request because the chunk is too large. Lower chunk size and retry."
            return "OpenRouter rejected this request. Try reducing chunk size or splitting the source text."
        if status and status >= 500:
            return "OpenRouter is temporarily unavailable. Please try again shortly."
        return str(exc)
    if isinstance(exc, RuntimeError) and "OPENROUTER_API_KEY" in str(exc):
        return "OpenRouter API key is not configured. Set OPENROUTER_API_KEY and retry."
    return str(exc)


def _is_terminal_llm_error(exc):
    """A failure that will recur on every call, so there's no point continuing."""
    if isinstance(exc, OpenRouterError):
        if exc.status_code in (401, 403):
            return True
        detail = (exc.response_body or "").lower()
        if exc.status_code == 429 and any(m in detail for m in TERMINAL_MARKERS):
            return True
    if isinstance(exc, RuntimeError) and "OPENROUTER_API_KEY" in str(exc):
        return True
    return False


def _usage_run(deck_id, source_id, prompt_version, model, usage, messages, content, parsed_json):
    return LLMRun(
        deck_id=deck_id,
        source_id=source_id,
        model=model,
        prompt_version=prompt_version,
        input_tokens=usage.get("prompt_tokens"),
        output_tokens=usage.get("completion_tokens"),
        cost_estimate=usage.get("cost") or usage.get("total_cost"),
        request_json={"messages": messages, "model": model},
        response_text=content,
        parsed_json=parsed_json,
    )


def _set_progress(deck, **updates):
    settings = dict(deck.settings_json or {})
    settings.update(updates)
    deck.settings_json = settings
    db.session.commit()


def _pop_progress(deck, *keys, **updates):
    settings = dict(deck.settings_json or {})
    for key in keys:
        settings.pop(key, None)
    settings.update(updates)
    deck.settings_json = settings
    db.session.commit()


def _mark_failed(deck, message):
    try:
        db.session.rollback()
        settings = dict(deck.settings_json or {})
        settings["last_error"] = message
        for key in ("generation_stage", "source_chunks", "cheat_sheet_chunks_done"):
            settings.pop(key, None)
        deck.settings_json = settings
        deck.status = "failed"
        db.session.commit()
    except Exception:
        logger.exception("Failed to mark deck %s as failed", deck.id)


def generate_deck(deck_id):
    deck = db.session.get(Deck, deck_id)
    if not deck:
        return None
    try:
        return _run_generation(deck)
    except Exception as exc:
        # Single guaranteed exit: ANY unhandled failure marks the deck failed so it
        # can never get stuck in "processing" forever.
        logger.exception("Deck %s generation failed", deck_id)
        _mark_failed(deck, format_generation_error(exc))
        return None


def _run_generation(deck):
    deck_id = deck.id
    settings = deck.settings_json or {}
    cfg = _llm_config()

    _pop_progress(deck, "last_error", generation_stage="cheat_sheet")
    deck.status = "processing"
    db.session.commit()

    cleaned = clean_text(deck.source_text)
    max_chars = int(settings.get("max_chars", 3500))
    source_chunks = chunk_text(cleaned, max_chars=max_chars)
    _set_progress(
        deck,
        generation_stage="cheat_sheet",
        source_chunks=len(source_chunks),
        cheat_sheet_chunks_done=0,
    )

    # --- Stage 1: build the cheat sheet IN MEMORY (nothing destroyed yet) ---
    cheat_sheet_sections = []
    pending_runs = []
    for idx, (title, text) in enumerate(source_chunks):
        messages = build_cheat_sheet_prompt(title, text, settings, idx + 1, len(source_chunks))
        try:
            response = _chat(cfg, messages)
            content = message_content(response)
            usage = response.get("usage", {})
            section = normalize_cheat_sheet_section(content, idx + 1)
            if not section:
                raise ValueError("The model returned an empty cheat sheet section.")
            cheat_sheet_sections.append(section)
            pending_runs.append(
                _usage_run(
                    deck_id, None, CHEAT_SHEET_PROMPT_VERSION, cfg["model"], usage,
                    messages, content, {"cheat_sheet_section": section},
                )
            )
        except Exception as exc:
            if _is_terminal_llm_error(exc):
                raise
            logger.warning("Cheat sheet chunk %s failed: %s", idx + 1, exc)
            pending_runs.append(
                LLMRun(
                    deck_id=deck_id, source_id=None, model=cfg["model"],
                    prompt_version=CHEAT_SHEET_PROMPT_VERSION,
                    request_json={"messages": messages, "model": cfg["model"]},
                    error=f"Cheat sheet chunk {idx + 1}: {format_generation_error(exc)}",
                )
            )
        _set_progress(deck, cheat_sheet_chunks_done=idx + 1)

    if not cheat_sheet_sections:
        # Nothing usable and we haven't touched existing data — fail cleanly.
        raise OpenRouterError("Could not build a cheat sheet from the source material.")

    # --- Now it is safe to replace prior generation output ---
    Card.query.filter_by(deck_id=deck_id).delete()
    LLMRun.query.filter_by(deck_id=deck_id).delete()
    Source.query.filter_by(deck_id=deck_id).delete()
    db.session.commit()
    for run in pending_runs:
        db.session.add(run)
    db.session.commit()

    cheat_sheet = clean_text("\n\n".join(cheat_sheet_sections))
    cheat_sheet_chunks = chunk_text(cheat_sheet, max_chars=max_chars)
    sources = []
    for idx, (title, text) in enumerate(cheat_sheet_chunks):
        sources.append(
            Source(
                deck_id=deck_id,
                idx=idx,
                title=title or f"Cheat Sheet Section {idx + 1}",
                text=text,
                hash=hash_text(text),
            )
        )
    db.session.add_all(sources)
    _set_progress(
        deck,
        cheat_sheet=cheat_sheet,
        cheat_sheet_sections=len(sources),
        generation_stage="cards",
    )

    # --- Stage 2: cards per source, fault-tolerant (one bad chunk != dead deck) ---
    auto_deleted_cards = 0
    card_failures = 0
    cards_made = 0
    for source in sources:
        messages = build_prompt(source.title, source.text, settings, deck.card_style)
        try:
            response = _chat(cfg, messages, response_format=CARD_RESPONSE_FORMAT)
            content = message_content(response)
            usage = response.get("usage", {})
            cards, parsed_json = parse_cards(content, cfg)
            rows, auto_deleted = _build_cards_for_source(cards, source)
            auto_deleted_cards += auto_deleted
            cards_made += len(rows)
            db.session.add_all(rows)
            db.session.add(
                _usage_run(
                    deck_id, source.id, CARD_PROMPT_VERSION, cfg["model"], usage,
                    messages, content, parsed_json,
                )
            )
            db.session.commit()
        except Exception as exc:
            if _is_terminal_llm_error(exc):
                raise
            card_failures += 1
            logger.warning("Card chunk %s failed: %s", source.idx + 1, exc)
            db.session.rollback()
            db.session.add(
                LLMRun(
                    deck_id=deck_id, source_id=source.id, model=cfg["model"],
                    prompt_version=CARD_PROMPT_VERSION,
                    request_json={"messages": messages, "model": cfg["model"]},
                    error=f"Chunk {source.idx + 1}: {format_generation_error(exc)}",
                )
            )
            db.session.commit()

    dedupe_cards(deck_id)

    ok_cards = Card.query.filter_by(deck_id=deck_id, status="ok").count()
    if ok_cards == 0:
        raise OpenRouterError("No valid cards could be generated from this source.")

    updates = {}
    if auto_deleted_cards:
        updates["auto_deleted_cards"] = auto_deleted_cards
    if card_failures:
        updates["partial_failures"] = card_failures
    _pop_progress(
        deck,
        "generation_stage", "source_chunks", "cheat_sheet_chunks_done", "dropped_cards",
        **updates,
    )
    if not auto_deleted_cards:
        _pop_progress(deck, "auto_deleted_cards")
    deck.status = "ready"
    db.session.commit()
    logger.info(
        "Deck %s ready: %s cards, %s auto-deleted, %s chunk failures",
        deck_id, cards_made, auto_deleted_cards, card_failures,
    )
    return deck_id


def dedupe_cards(deck_id):
    cards = Card.query.filter_by(deck_id=deck_id, status="ok").all()
    seen = set()
    for card in cards:
        key = (
            card.type,
            (card.front or "").lower().strip(),
            (card.back or "").lower().strip(),
            (card.cloze_text or "").lower().strip(),
        )
        if key in seen:
            card.status = "deleted"
        else:
            seen.add(key)
    db.session.commit()


def regenerate_source(source_id):
    source = db.session.get(Source, source_id)
    if not source:
        return None
    deck = db.session.get(Deck, source.deck_id)
    if not deck:
        return None
    settings = deck.settings_json or {}
    cfg = _llm_config()
    messages = build_prompt(source.title, source.text, settings, deck.card_style)
    # Build new cards first; only swap out the old ones if generation succeeds, so a
    # failed regenerate never wipes a working section.
    response = _chat(cfg, messages, response_format=CARD_RESPONSE_FORMAT)
    content = message_content(response)
    usage = response.get("usage", {})
    cards, parsed_json = parse_cards(content, cfg)
    rows, _auto_deleted = _build_cards_for_source(cards, source)

    Card.query.filter_by(source_id=source_id).delete()
    if rows:
        db.session.add_all(rows)
    db.session.add(
        _usage_run(
            deck.id, source_id, CARD_PROMPT_VERSION, cfg["model"], usage,
            messages, content, parsed_json,
        )
    )
    db.session.commit()
    return source_id


def improve_card(card_id):
    card = db.session.get(Card, card_id)
    if not card:
        return None
    deck = db.session.get(Deck, card.deck_id)
    if not deck:
        return None
    cfg = _llm_config()
    if card.type == "basic":
        prompt = (
            "Improve this Anki basic card for clarity and concision. Keep it atomic. "
            "Return only JSON.\n\n"
            f"Front: {card.front}\nBack: {card.back}"
        )
        response_format = IMPROVE_BASIC_FORMAT
    else:
        prompt = (
            "Improve this Anki cloze card for clarity and concision. Preserve valid "
            "{{c1::...}} cloze syntax and keep it atomic. Return only JSON.\n\n"
            f"Cloze: {card.cloze_text}\nExtra: {card.extra or ''}"
        )
        response_format = IMPROVE_CLOZE_FORMAT
    messages = [
        {"role": "system", "content": "You output strict JSON only. No prose."},
        {"role": "user", "content": prompt},
    ]
    response = _chat(cfg, messages, temperature=0.2, response_format=response_format)
    content = message_content(response)
    data = extract_json(content)
    if card.type == "basic":
        card.front = normalize_math(normalize_text(data.get("front", card.front)))
        card.back = normalize_math(normalize_text(data.get("back", card.back)))
    else:
        card.cloze_text = normalize_math(normalize_text(data.get("cloze_text", card.cloze_text)))
        card.extra = normalize_math(normalize_text(data.get("extra", card.extra or "")))
        card.status = "ok" if is_valid_cloze(card.cloze_text) else "needs_review"
    db.session.commit()
    return card_id
