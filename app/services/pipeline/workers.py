"""Phase 2 — card-writing workers.

A worker is one model call: strategy system prompt + the planner's notes + the unit
text verbatim (never a lossy summary) + a short "what your siblings are covering" hint so
adjacent tasks don't write the same cards. Workers are pure functions so they can be run
in a thread pool; the orchestrator persists what they return.
"""

import logging

from pydantic import ValidationError

from ..llm import CARD_RESPONSE_FORMAT, extract_json, finish_reason
from ..schemas import ChunkSchema
from ..validators import normalize_math, normalize_text
from .strategies import PROMPT_VERSION, system_prompt

logger = logging.getLogger(__name__)

WORKER_PROMPT_VERSION = f"{PROMPT_VERSION}:worker"
SIBLING_HINT_LIMIT = 6


def _unit_block(unit):
    header = f"### Unit {unit.idx}: {unit.title}"
    if unit.page_start:
        header += f" (p.{unit.page_start}" + (f"-{unit.page_end}" if unit.page_end and unit.page_end != unit.page_start else "") + ")"
    return f"{header}\n\n{unit.text}"


def build_worker_messages(task, units, settings, card_style, siblings=None, figure=None, target_override=None):
    """`units` are the Unit objects for task.unit_idxs in order; `siblings` is a list of
    (strategy, unit_idxs, notes) for other tasks touching the same units."""
    system = system_prompt(
        task.strategy, card_style,
        focus=settings.get("focus", ""), exclude=settings.get("exclude", ""), glossary=settings.get("glossary", ""),
    )
    target = target_override or task.target_cards
    parts = [
        f"TASK: write about {target} cards using the {task.strategy} strategy.",
        f"Planner notes: {task.notes or '(none)'}",
    ]
    context = settings.get("exam_context") or ""
    if context:
        parts.append(f"Student context: {context}")
    if siblings:
        hints = []
        for strategy, idxs, notes in siblings[:SIBLING_HINT_LIMIT]:
            hints.append(f"- {strategy} over units {list(idxs)}: {notes or 'general coverage'}")
        parts.append(
            "Other workers are covering the same material with these briefs; do NOT duplicate their cards:\n"
            + "\n".join(hints)
        )
    if figure is not None:
        parts.append(
            "FIGURE under study (an image of it will be shown on the card):\n"
            f"Caption: {figure.get('caption') or '(none)'}\n"
            f"Description: {figure.get('description') or '(none)'}\n"
            f"Labelled parts / data: {figure.get('parts') or '(none)'}\n"
            f"Testable facts the figure conveys: {figure.get('facts') or '(none)'}"
        )
    parts.append("SOURCE (use only this):\n\n" + "\n\n".join(_unit_block(u) for u in units))
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def parse_cards(content, client=None):
    """Structured outputs make this reliable; the repair path is a last resort."""
    try:
        data = extract_json(content)
    except Exception:
        if client is None:
            raise
        data = client.repair_json(content)
    try:
        parsed = ChunkSchema.model_validate(data)
        return parsed.cards, data
    except ValidationError:
        # Salvage the valid cards from a partially-bad list instead of losing the batch.
        good = []
        for item in (data.get("cards") or []) if isinstance(data, dict) else []:
            try:
                good.append(ChunkSchema.model_validate({"cards": [item]}).cards[0])
            except ValidationError:
                continue
        if not good:
            raise
        return good, data


def normalize_card(card, strategy=None):
    """Pydantic card -> plain dict ready for validation/persistence."""
    if card.type == "basic":
        out = {
            "type": "basic",
            "front": normalize_math(normalize_text(card.front)),
            "back": normalize_math(normalize_text(card.back)),
            "cloze_text": None,
            "extra": None,
        }
    else:
        out = {
            "type": "cloze",
            "front": None,
            "back": None,
            "cloze_text": normalize_math(normalize_text(card.cloze_text)),
            "extra": normalize_math(normalize_text(card.extra or "")),
        }
    out["tags"] = [t.strip().lower() for t in (card.tags or []) if t and t.strip()][:5]
    out["source_quote"] = normalize_text(card.source_quote or "")[:400] or None
    out["strategy"] = strategy
    return out


def run_worker(client, messages, task_target, retry_on_truncation=True):
    """One worker call (plus one retry with a smaller ask if the output was truncated).
    Returns {"cards": [...], "usage": {...}, "content": str, "truncated": bool, "attempts": n}."""
    result = client.chat("worker", messages, response_format=CARD_RESPONSE_FORMAT)
    usage = dict(result.usage)
    attempts = 1
    truncated = result.finish_reason == "length"
    content = result.content
    if truncated and retry_on_truncation:
        # The model ran out of output budget mid-JSON. Ask for fewer, tighter cards.
        smaller = max(3, int(task_target * 0.6))
        retry_messages = list(messages)
        retry_messages[-1] = {
            "role": "user",
            "content": messages[-1]["content"].replace(
                f"write about {task_target} cards", f"write about {smaller} cards"
            ) + f"\n\nYour previous attempt was cut off. Write at most {smaller} cards and keep answers tight.",
        }
        result2 = client.chat("worker", retry_messages, response_format=CARD_RESPONSE_FORMAT)
        attempts += 1
        for k in ("prompt_tokens", "completion_tokens"):
            usage[k] = int(usage.get(k) or 0) + int(result2.usage.get(k) or 0)
        usage["cost"] = float(usage.get("cost") or 0) + result2.cost
        if result2.finish_reason != "length":
            content = result2.content
            truncated = False
    cards, _data = parse_cards(content, client=client)
    return {
        "cards": [c.model_dump() for c in cards],
        "usage": usage,
        "content": content,
        "truncated": truncated,
        "attempts": attempts,
        "model": result.model,
    }
