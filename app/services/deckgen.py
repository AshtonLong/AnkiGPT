"""Deck-level operations outside the main run: regenerate one unit, improve one card.

The generation run itself lives in `pipeline.orchestrator`; this module re-exports its
entry points so existing callers keep working.
"""

import logging

from flask import current_app

from ..extensions import db
from ..models import Card, Deck, LLMRun, PipelineTask, Source, utcnow
from .llm import extract_json, json_schema_format
from .pipeline import critic as critic_mod
from .pipeline import workers as workers_mod
from .pipeline.document_map import Unit
from .pipeline.orchestrator import format_generation_error, generate_deck  # noqa: F401
from .pipeline.planner import PlanTask, heuristic_target
from .pipeline.routing import LLMClient
from .pipeline.strategies import DEFAULT_STRATEGY
from .validators import is_math_valid, is_valid_cloze, normalize_math, normalize_text

logger = logging.getLogger(__name__)

IMPROVE_BASIC_FORMAT = json_schema_format(
    "improved_basic_card",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {"front": {"type": "string"}, "back": {"type": "string"}},
        "required": ["front", "back"],
    },
)

IMPROVE_CLOZE_FORMAT = json_schema_format(
    "improved_cloze_card",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {"cloze_text": {"type": "string"}, "extra": {"type": "string"}},
        "required": ["cloze_text", "extra"],
    },
)


def _unit_from_source(source):
    return Unit(
        idx=source.idx, title=source.title or f"Unit {source.idx + 1}", text=source.text, char_start=source.char_start,
        char_end=source.char_end, kind=source.kind or "prose", density=source.density or 3,
        depends_on=list(source.depends_on or []), summary=source.summary, page_start=source.page_start,
        page_end=source.page_end,
    )


def regenerate_source(source_id, strategy=None):
    """Rewrite the cards of one unit (worker + critic). Existing cards are only replaced
    once the new ones exist, so a failed regenerate never wipes a working section."""
    source = db.session.get(Source, source_id)
    if not source:
        return None
    deck = db.session.get(Deck, source.deck_id)
    if not deck:
        return None
    settings = dict(deck.settings_json or {})
    client = LLMClient(current_app.config)
    unit = _unit_from_source(source)
    # Reuse the strategy the planner chose for this unit when we can find it.
    previous = (
        PipelineTask.query.filter_by(deck_id=deck.id, phase="write", kind="task")
        .order_by(PipelineTask.seq).all()
    )
    chosen = strategy
    if not chosen:
        for t in previous:
            if source.idx in (t.unit_ids or []) and t.strategy and t.strategy != "figure_recall":
                chosen = t.strategy
                break
    chosen = chosen or DEFAULT_STRATEGY
    task = PlanTask(id=0, unit_idxs=[source.idx], strategy=chosen, target_cards=heuristic_target(unit, chosen),
                    notes="Regeneration requested by the user for this unit.", origin="regenerate")
    node = PipelineTask(deck_id=deck.id, seq=0, phase="regenerate", kind="task", label=f"Regenerate · {unit.title[:100]}",
                        strategy=chosen, unit_ids=[source.idx], model=client.model_for("worker"),
                        target_cards=task.target_cards, status="running", started_at=utcnow())
    db.session.add(node)
    db.session.commit()
    messages = workers_mod.build_worker_messages(task, [unit], settings, deck.card_style)
    try:
        value = workers_mod.run_worker(client, messages, task.target_cards)
    except Exception as exc:
        node.status = "failed"
        node.error = format_generation_error(exc)
        node.finished_at = utcnow()
        db.session.commit()
        raise
    rows = []
    for raw in value.get("cards") or []:
        try:
            from .schemas import CardSchema

            card = workers_mod.normalize_card(CardSchema.model_validate(raw), strategy=chosen)
        except Exception:
            continue
        content = " ".join(filter(None, [card.get("front"), card.get("back"), card.get("cloze_text"), card.get("extra")]))
        bad = (card["type"] == "cloze" and not is_valid_cloze(card["cloze_text"])) or not is_math_valid(content)
        tags = list(card["tags"]) + [f"strategy:{chosen}", f"unit:{source.idx + 1}", "origin:regenerate"]
        rows.append(Card(deck_id=deck.id, source_id=source.id, task_id=node.id, type=card["type"], front=card.get("front"),
                         back=card.get("back"), cloze_text=card.get("cloze_text"), extra=card.get("extra"), tags=tags,
                         status="deleted" if bad else "ok", strategy=chosen, source_quote=card.get("source_quote")))
    if not rows:
        node.status = "failed"
        node.error = "The model returned no usable cards."
        node.finished_at = utcnow()
        db.session.commit()
        raise RuntimeError("Regeneration produced no cards.")
    # Critic on the fresh cards (best effort).
    ok_rows = [r for r in rows if r.status == "ok"]
    if ok_rows and current_app.config.get("PIPELINE_CRITIC_ENABLED", True):
        try:
            dicts = [{"type": r.type, "front": r.front, "back": r.back, "cloze_text": r.cloze_text, "extra": r.extra,
                      "tags": r.tags, "source_quote": r.source_quote} for r in ok_rows]
            verdicts = critic_mod.run_critic_batch(client, dicts, unit.text).get("verdicts") or {}
            for i, r in enumerate(ok_rows):
                v = verdicts.get(i)
                if not v:
                    continue
                new_card, status, tags = critic_mod.apply_verdict(dicts[i], v)
                r.type, r.front, r.back = new_card["type"], new_card.get("front"), new_card.get("back")
                r.cloze_text, r.extra = new_card.get("cloze_text"), new_card.get("extra")
                r.status = status
                r.difficulty = new_card.get("difficulty")
                r.tags = list(r.tags or []) + tags
                r.critic_json = {k: v.get(k) for k in ("verdict", "reason", "supported", "leaks_answer", "difficulty")}
        except Exception:
            logger.exception("Critic failed during regenerate of unit %s", source_id)
    Card.query.filter_by(source_id=source_id).delete()
    order_base = source.idx * 1000
    for i, r in enumerate(rows, start=1):
        r.order_key = order_base + i
    db.session.add_all(rows)
    db.session.add(LLMRun(deck_id=deck.id, source_id=source.id, task_id=node.id, role="worker", model=value.get("model"),
                          prompt_version=workers_mod.WORKER_PROMPT_VERSION, input_tokens=(value.get("usage") or {}).get("prompt_tokens"),
                          output_tokens=(value.get("usage") or {}).get("completion_tokens"),
                          cost_estimate=(value.get("usage") or {}).get("cost"), request_json={"messages": messages[-1:]},
                          response_text=value.get("content"), parsed_json={"cards": value.get("cards")}))
    node.status = "done"
    node.cards_made = len(rows)
    node.cards_kept = sum(1 for r in rows if r.status == "ok")
    node.finished_at = utcnow()
    db.session.commit()
    return source_id


def improve_card(card_id):
    card = db.session.get(Card, card_id)
    if not card:
        return None
    deck = db.session.get(Deck, card.deck_id)
    if not deck:
        return None
    client = LLMClient(current_app.config)
    source = db.session.get(Source, card.source_id) if card.source_id else None
    grounding = ""
    if source:
        grounding = f"\n\nSource excerpt the card was written from (stay within it):\n{source.text[:6000]}"
    if card.type == "basic":
        prompt = (
            "Improve this Anki basic card for clarity, precision and concision. Keep it atomic and grounded. "
            "Return only JSON.\n\n"
            f"Front: {card.front}\nBack: {card.back}{grounding}"
        )
        response_format = IMPROVE_BASIC_FORMAT
    else:
        prompt = (
            "Improve this Anki cloze card for clarity and concision. Preserve valid {{c1::...}} cloze syntax, cloze "
            "the discriminating detail (not the topic word), and keep it atomic. Return only JSON.\n\n"
            f"Cloze: {card.cloze_text}\nExtra: {card.extra or ''}{grounding}"
        )
        response_format = IMPROVE_CLOZE_FORMAT
    messages = [
        {"role": "system", "content": "You are an expert Anki card editor. You output strict JSON only. No prose."},
        {"role": "user", "content": prompt},
    ]
    result = client.chat("critic", messages, response_format=response_format, max_tokens=2000)
    data = extract_json(result.content)
    if card.type == "basic":
        card.front = normalize_math(normalize_text(data.get("front", card.front)))
        card.back = normalize_math(normalize_text(data.get("back", card.back)))
    else:
        card.cloze_text = normalize_math(normalize_text(data.get("cloze_text", card.cloze_text)))
        card.extra = normalize_math(normalize_text(data.get("extra", card.extra or "")))
        card.status = "ok" if is_valid_cloze(card.cloze_text) else "needs_review"
    db.session.add(LLMRun(deck_id=deck.id, source_id=card.source_id, role="critic", model=result.model,
                          prompt_version="improve-v2", input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                          cost_estimate=result.cost, response_text=result.content, parsed_json=data))
    db.session.commit()
    return card_id
