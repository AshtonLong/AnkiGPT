from flask import current_app
from .chunking import clean_text, chunk_text, hash_text
from .llm import OpenRouterError, extract_json, openrouter_chat, repair_json
from .schemas import ChunkSchema
from .validators import is_valid_cloze, normalize_text, normalize_math, is_math_valid, is_in_scope
from ..extensions import db
from ..models import Card, Deck, LLMRun, Source


PROMPT_VERSION = "v3"


def tagify(text):
    return "".join([c if c.isalnum() or c in ("-", "_") else "_" for c in text.lower()]).strip("_")


def build_prompt(chunk_title, chunk_text, settings, card_style):
    focus = settings.get("focus", "")
    exclude = settings.get("exclude", "")
    glossary = settings.get("glossary", "")
    rules = "\n".join(
        [
            "Output strict JSON only.",
            "Scope: ONLY use facts explicitly stated in the chunk.",
            "Coverage: be exhaustive for all main topics and key details; exam prep.",
            "Cards are self-contained; no references to tables/figures/diagrams.",
            "Style: clean, minimal formatting, consistent wording.",
            "Input may include Markdown headings/lists from PDF conversion; use that structure for coverage.",
            "Basic: concise Q -> A. Cloze: use {{c1::...}} (1–2 deletions).",
            "Math: only \\( ... \\) inline and \\[ ... \\] display.",
            "Avoid ambiguity; include subject, scope, conditions; no vague pronouns.",
            "Prefer list-style cards when content is a list; include full list on one card.",
            "Cover: definitions, equations + variable meanings, steps, constraints, edge cases, contrasts, pitfalls.",
        ]
    )
    schema = (
        "Return only JSON: {\"cards\": [{\"type\": \"basic|cloze\", "
        "\"front\": string?, \"back\": string?, "
        "\"cloze_text\": string?, \"extra\": string?, \"tags\": [string]}]}"
    )
    user_prompt = f"""Generate Anki cards from this chunk.

Title: {chunk_title or "Untitled"}
Content:
{chunk_text}

Card style: {card_style}
Focus: {focus or "general coverage"}
Exclude: {exclude or "none"}
Glossary: {glossary or "none"}

Rules:
{rules}

{schema}
"""
    messages = [
        {"role": "system", "content": "You output strict JSON only. No prose."},
        {"role": "user", "content": user_prompt.strip()},
    ]
    return messages


def parse_cards(
    raw_text,
    model,
    api_key,
    site_url,
    app_name,
    max_retries=2,
    backoff_seconds=1.5,
    timeout_seconds=120,
):
    try:
        data = extract_json(raw_text)
    except Exception:
        data = repair_json(
            raw_text,
            model,
            api_key,
            site_url,
            app_name,
            max_retries=max_retries,
            backoff_seconds=backoff_seconds,
            timeout_seconds=timeout_seconds,
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


def format_generation_error(exc):
    if isinstance(exc, OpenRouterError):
        status = exc.status_code
        detail = (exc.response_body or "").lower()
        if status == 429:
            if any(marker in detail for marker in ("insufficient", "credit", "quota", "billing", "payment")):
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
    return str(exc)


def generate_deck(deck_id):
    deck = Deck.query.get(deck_id)
    if not deck:
        return None
    settings = deck.settings_json or {}
    llm_max_retries = int(current_app.config.get("OPENROUTER_MAX_RETRIES", 2))
    llm_backoff_seconds = float(current_app.config.get("OPENROUTER_RETRY_BACKOFF_SECONDS", 1.5))
    llm_timeout_seconds = float(current_app.config.get("OPENROUTER_TIMEOUT_SECONDS", 120))
    updated_settings = dict(settings)
    updated_settings.pop("last_error", None)
    deck.settings_json = updated_settings
    deck.status = "processing"
    db.session.commit()

    Card.query.filter_by(deck_id=deck_id).delete()
    Source.query.filter_by(deck_id=deck_id).delete()
    LLMRun.query.filter_by(deck_id=deck_id).delete()
    db.session.commit()

    cleaned = clean_text(deck.source_text)
    max_chars = int(settings.get("max_chars", 3500))
    chunks = chunk_text(cleaned, max_chars=max_chars)
    sources = []
    for idx, (title, text) in enumerate(chunks):
        source = Source(
            deck_id=deck_id,
            idx=idx,
            title=title,
            text=text,
            hash=hash_text(text),
        )
        sources.append(source)
    db.session.add_all(sources)
    db.session.commit()

    model = current_app.config["OPENROUTER_MODEL"]
    api_key = current_app.config["OPENROUTER_API_KEY"]
    site_url = current_app.config["OPENROUTER_SITE_URL"]
    app_name = current_app.config["OPENROUTER_APP_NAME"]

    created_cards = []
    auto_deleted_cards = 0
    for source in sources:
        messages = build_prompt(source.title, source.text, settings, deck.card_style)
        try:
            response = openrouter_chat(
                messages,
                model,
                api_key,
                site_url,
                app_name,
                max_retries=llm_max_retries,
                backoff_seconds=llm_backoff_seconds,
                timeout_seconds=llm_timeout_seconds,
            )
            content = response["choices"][0]["message"]["content"]
            usage = response.get("usage", {})
            cards, parsed_json = parse_cards(
                content,
                model,
                api_key,
                site_url,
                app_name,
                max_retries=llm_max_retries,
                backoff_seconds=llm_backoff_seconds,
                timeout_seconds=llm_timeout_seconds,
            )
            for card in cards:
                normalized = normalize_card(card)
                issues = validation_issues(normalized, source.text)
                status = "deleted" if issues else "ok"
                if issues:
                    auto_deleted_cards += 1
                tags = list(card.tags or [])
                tags.append(f"section:{source.idx + 1}")
                if source.title:
                    tags.append(f"title:{tagify(source.title)}")
                tags = apply_validation_tags(tags, issues)
                created_cards.append(
                    Card(
                        deck_id=deck_id,
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
            llm_run = LLMRun(
                deck_id=deck_id,
                source_id=source.id,
                model=model,
                prompt_version=PROMPT_VERSION,
                input_tokens=usage.get("prompt_tokens"),
                output_tokens=usage.get("completion_tokens"),
                cost_estimate=usage.get("total_cost"),
                request_json={"messages": messages, "model": model},
                response_text=content,
                parsed_json=parsed_json,
            )
            db.session.add(llm_run)
            db.session.commit()
        except Exception as exc:
            user_error = format_generation_error(exc)
            chunk_error = f"Chunk {source.idx + 1}: {user_error}"
            llm_run = LLMRun(
                deck_id=deck_id,
                source_id=source.id,
                model=model,
                prompt_version=PROMPT_VERSION,
                request_json={"messages": messages, "model": model},
                error=chunk_error,
            )
            db.session.add(llm_run)
            updated_settings = dict(deck.settings_json or {})
            updated_settings["last_error"] = chunk_error
            deck.settings_json = updated_settings
            deck.status = "failed"
            db.session.commit()
            return None

    if created_cards:
        db.session.add_all(created_cards)
        db.session.commit()

    dedupe_cards(deck_id)
    updated_settings = dict(deck.settings_json or {})
    if auto_deleted_cards:
        updated_settings["auto_deleted_cards"] = auto_deleted_cards
    else:
        updated_settings.pop("auto_deleted_cards", None)
    updated_settings.pop("dropped_cards", None)
    deck.settings_json = updated_settings
    deck.status = "ready"
    db.session.commit()
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
    source = Source.query.get(source_id)
    if not source:
        return None
    deck = Deck.query.get(source.deck_id)
    if not deck:
        return None
    Card.query.filter_by(source_id=source_id).delete()
    db.session.commit()
    settings = deck.settings_json or {}
    llm_max_retries = int(current_app.config.get("OPENROUTER_MAX_RETRIES", 2))
    llm_backoff_seconds = float(current_app.config.get("OPENROUTER_RETRY_BACKOFF_SECONDS", 1.5))
    llm_timeout_seconds = float(current_app.config.get("OPENROUTER_TIMEOUT_SECONDS", 120))
    model = current_app.config["OPENROUTER_MODEL"]
    api_key = current_app.config["OPENROUTER_API_KEY"]
    site_url = current_app.config["OPENROUTER_SITE_URL"]
    app_name = current_app.config["OPENROUTER_APP_NAME"]
    messages = build_prompt(source.title, source.text, settings, deck.card_style)
    response = openrouter_chat(
        messages,
        model,
        api_key,
        site_url,
        app_name,
        max_retries=llm_max_retries,
        backoff_seconds=llm_backoff_seconds,
        timeout_seconds=llm_timeout_seconds,
    )
    content = response["choices"][0]["message"]["content"]
    cards, parsed_json = parse_cards(
        content,
        model,
        api_key,
        site_url,
        app_name,
        max_retries=llm_max_retries,
        backoff_seconds=llm_backoff_seconds,
        timeout_seconds=llm_timeout_seconds,
    )
    created_cards = []
    for card in cards:
        normalized = normalize_card(card)
        issues = validation_issues(normalized, source.text)
        status = "deleted" if issues else "ok"
        tags = list(card.tags or [])
        tags.append(f"section:{source.idx + 1}")
        if source.title:
            tags.append(f"title:{tagify(source.title)}")
        tags = apply_validation_tags(tags, issues)
        created_cards.append(
            Card(
                deck_id=deck.id,
                source_id=source_id,
                type=normalized["type"],
                front=normalized.get("front"),
                back=normalized.get("back"),
                cloze_text=normalized.get("cloze_text"),
                extra=normalized.get("extra"),
                tags=tags,
                status=status,
            )
        )
    if created_cards:
        db.session.add_all(created_cards)
        db.session.commit()
    llm_run = LLMRun(
        deck_id=deck.id,
        source_id=source_id,
        model=model,
        prompt_version=PROMPT_VERSION,
        request_json={"messages": messages, "model": model},
        response_text=content,
        parsed_json=parsed_json,
    )
    db.session.add(llm_run)
    db.session.commit()
    return source_id


def improve_card(card_id):
    card = Card.query.get(card_id)
    if not card:
        return None
    deck = Deck.query.get(card.deck_id)
    if not deck:
        return None
    model = current_app.config["OPENROUTER_MODEL"]
    api_key = current_app.config["OPENROUTER_API_KEY"]
    site_url = current_app.config["OPENROUTER_SITE_URL"]
    app_name = current_app.config["OPENROUTER_APP_NAME"]
    llm_max_retries = int(current_app.config.get("OPENROUTER_MAX_RETRIES", 2))
    llm_backoff_seconds = float(current_app.config.get("OPENROUTER_RETRY_BACKOFF_SECONDS", 1.5))
    llm_timeout_seconds = float(current_app.config.get("OPENROUTER_TIMEOUT_SECONDS", 120))
    if card.type == "basic":
        prompt = (
            "Improve this Anki basic card for clarity and concision. "
            "Return only JSON: {\"front\": \"...\", \"back\": \"...\"}.\n\n"
            f"Front: {card.front}\nBack: {card.back}"
        )
    else:
        prompt = (
            "Improve this Anki cloze card for clarity and concision. "
            "Preserve valid cloze syntax. Return only JSON: "
            "{\"cloze_text\": \"...\", \"extra\": \"...\"}.\n\n"
            f"Cloze: {card.cloze_text}\nExtra: {card.extra or ''}"
        )
    messages = [
        {"role": "system", "content": "You output strict JSON only. No prose."},
        {"role": "user", "content": prompt},
    ]
    response = openrouter_chat(
        messages,
        model,
        api_key,
        site_url,
        app_name,
        max_retries=llm_max_retries,
        backoff_seconds=llm_backoff_seconds,
        timeout_seconds=llm_timeout_seconds,
    )
    content = response["choices"][0]["message"]["content"]
    data = extract_json(content)
    if card.type == "basic":
        card.front = normalize_math(normalize_text(data.get("front", card.front)))
        card.back = normalize_math(normalize_text(data.get("back", card.back)))
    else:
        card.cloze_text = normalize_math(normalize_text(data.get("cloze_text", card.cloze_text)))
        card.extra = normalize_math(normalize_text(data.get("extra", card.extra or "")))
        if not is_valid_cloze(card.cloze_text):
            card.status = "needs_review"
    db.session.commit()
    return card_id
    scope_rules = (
        "Scope: ONLY use facts explicitly stated in the chunk content. "
        "Do not add external knowledge or assumptions."
    )
