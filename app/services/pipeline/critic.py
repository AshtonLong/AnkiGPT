"""Phase 3 — the adversarial critic.

Two cheap calls per batch of cards:

1. Cold pass: the model answers every card front *without* the source. This exposes
   cards whose front leaks the answer (the classic cloze failure) and gives a difficulty
   signal: a well-formed card a strong model still can't answer cold is discriminating.
2. Judge pass: with the source, the cards, and the cold answers in view, the model rules
   on each card — supported by the source, atomic, unambiguous, not leaking — and
   returns keep / rewrite / drop with a reason and, for rewrites, the fixed card.

This replaces the old bag-of-words `is_in_scope`, which could not tell a wrong card that
used the right vocabulary from a right one.
"""

import logging
import re

from ..llm import extract_json, json_schema_format
from ..validators import is_valid_cloze, normalize_math, normalize_text

logger = logging.getLogger(__name__)

CRITIC_PROMPT_VERSION = "critic-v1"
BATCH_SIZE = 20
SOURCE_CAP = 24000

COLD_SCHEMA = json_schema_format(
    "cold_answers",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "answers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"index": {"type": "integer"}, "answer": {"type": "string"}},
                    "required": ["index", "answer"],
                },
            }
        },
        "required": ["answers"],
    },
)

REWRITE_SCHEMA = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "properties": {
        "type": {"type": "string", "enum": ["basic", "cloze"]},
        "front": {"type": ["string", "null"]},
        "back": {"type": ["string", "null"]},
        "cloze_text": {"type": ["string", "null"]},
        "extra": {"type": ["string", "null"]},
    },
    "required": ["type", "front", "back", "cloze_text", "extra"],
}

JUDGE_SCHEMA = json_schema_format(
    "critic_verdicts",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "verdicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "index": {"type": "integer"},
                        "supported": {"type": "boolean"},
                        "atomic": {"type": "boolean"},
                        "ambiguous": {"type": "boolean"},
                        "leaks_answer": {"type": "boolean"},
                        "cold_answer_correct": {"type": "boolean"},
                        "difficulty": {"type": "integer"},
                        "verdict": {"type": "string", "enum": ["keep", "rewrite", "drop"]},
                        "reason": {"type": "string"},
                        "rewrite": REWRITE_SCHEMA,
                    },
                    "required": [
                        "index", "supported", "atomic", "ambiguous", "leaks_answer", "cold_answer_correct",
                        "difficulty", "verdict", "reason", "rewrite",
                    ],
                },
            }
        },
        "required": ["verdicts"],
    },
)

COLD_SYSTEM = """You are a strong student sitting a quiz with no notes. For each flashcard prompt, give your best short answer from memory. For cloze prompts, the blank is shown as [...]; answer with what fills the blank. If you genuinely do not know, answer "unknown". Return only JSON."""

JUDGE_SYSTEM = """You are the quality critic for machine-written Anki cards. You receive the SOURCE the cards were written from, the cards, and a "cold answer" produced by a student model that had no access to the source.

Judge every card on:
- supported: every claim in the card is stated in the source (paraphrase is fine; new facts are not). Check the numbers, names and directions of relationships carefully.
- atomic: it tests one fact, step, or relationship.
- ambiguous: the question could reasonably have several correct answers, or lacks the scope/conditions needed to answer it.
- leaks_answer: the prompt itself gives the answer away (a cloze whose surrounding words name the deleted term, a question containing its own answer). Use the cold answer as evidence: if it matched only because the wording leaks, flag it; if it matched because the fact is common knowledge, do not.
- cold_answer_correct: whether the cold answer is right according to the source.
- difficulty 1-3: 1 = basic recall most students know, 2 = requires studying this material, 3 = a fine distinction most students get wrong.

Verdicts:
- keep: supported, atomic, not ambiguous, not leaking. Minor stylistic issues are still keep.
- rewrite: the fact is valuable and supported but the wording is ambiguous, leaks, is not atomic (rewrite as the single most important fact), or has a cloze/format problem. Provide the fixed card in `rewrite` using the same JSON shape (basic: front+back; cloze: cloze_text+extra with {{c1::...}}). Keep math as \\( ... \\).
- drop: unsupported by the source, trivially guessable, meta/filler, or a duplicate of another card in this batch (drop the later one). Give a specific reason.

Set `rewrite` to null unless verdict is rewrite. Return only JSON."""


def cloze_prompt_text(cloze_text):
    return re.sub(r"\{\{c\d+::(.+?)(::[^}]*)?\}\}", "[...]", cloze_text or "")


def card_prompt(card):
    if card["type"] == "basic":
        return card.get("front") or ""
    return cloze_prompt_text(card.get("cloze_text"))


def card_answer(card):
    if card["type"] == "basic":
        return card.get("back") or ""
    answers = re.findall(r"\{\{c\d+::(.+?)(?:::[^}]*)?\}\}", card.get("cloze_text") or "")
    return " / ".join(answers)


def _card_block(i, card, cold=None):
    lines = [f"[{i}] type={card['type']}"]
    if card["type"] == "basic":
        lines.append(f"front: {card.get('front')}")
        lines.append(f"back: {card.get('back')}")
    else:
        lines.append(f"cloze_text: {card.get('cloze_text')}")
        if card.get("extra"):
            lines.append(f"extra: {card.get('extra')}")
    if card.get("source_quote"):
        lines.append(f"source_quote: {card['source_quote']}")
    if cold is not None:
        lines.append(f"cold answer: {cold}")
    return "\n".join(lines)


def cold_messages(cards):
    prompts = "\n".join(f"[{i}] {card_prompt(c)}" for i, c in enumerate(cards))
    return [
        {"role": "system", "content": COLD_SYSTEM},
        {"role": "user", "content": f"Prompts:\n{prompts}"},
    ]


def judge_messages(cards, cold_answers, source_text):
    source = source_text if len(source_text) <= SOURCE_CAP else source_text[:SOURCE_CAP] + "\n...[source truncated]"
    blocks = "\n\n".join(_card_block(i, c, cold_answers.get(i, "unknown")) for i, c in enumerate(cards))
    return [
        {"role": "system", "content": JUDGE_SYSTEM},
        {"role": "user", "content": f"SOURCE:\n{source}\n\nCARDS:\n{blocks}"},
    ]


def _merge_usage(*usages):
    out = {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
    for u in usages:
        if not u:
            continue
        out["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
        out["completion_tokens"] += int(u.get("completion_tokens") or 0)
        cost = u.get("cost")
        if cost is None:
            cost = u.get("total_cost")
        try:
            out["cost"] += float(cost or 0)
        except (TypeError, ValueError):
            pass
    return out


def run_critic_batch(client, cards, source_text, cold_pass=True):
    """Pure: returns {"verdicts": {index: verdict_dict}, "usage": {...}}."""
    cold_answers = {}
    usages = []
    if cold_pass and cards:
        try:
            cold = client.chat("critic", cold_messages(cards), response_format=COLD_SCHEMA, max_tokens=4000)
            usages.append(cold.usage)
            data = extract_json(cold.content)
            for item in data.get("answers") or []:
                try:
                    cold_answers[int(item.get("index"))] = str(item.get("answer") or "")[:300]
                except (TypeError, ValueError):
                    continue
        except Exception as exc:
            # The cold pass is a signal, not a gate; the judge still runs without it.
            logger.warning("Cold pass failed: %s", exc)
    judge = client.chat("critic", judge_messages(cards, cold_answers, source_text), response_format=JUDGE_SCHEMA)
    usages.append(judge.usage)
    data = extract_json(judge.content)
    verdicts = {}
    for item in data.get("verdicts") or []:
        try:
            idx = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(cards):
            item["cold_answer"] = cold_answers.get(idx)
            verdicts[idx] = item
    return {"verdicts": verdicts, "usage": _merge_usage(*usages), "model": judge.model}


def apply_verdict(card, verdict):
    """Return (card_dict, status, tags_to_add). Rewrites are re-validated; a broken
    rewrite falls back to keeping the original flagged for review."""
    tags = []
    if not verdict:
        return card, "ok", tags
    decision = verdict.get("verdict") or "keep"
    reason = (verdict.get("reason") or "").strip()
    difficulty = verdict.get("difficulty")
    try:
        difficulty = max(1, min(3, int(difficulty)))
    except (TypeError, ValueError):
        difficulty = None
    card = dict(card)
    card["difficulty"] = difficulty
    if decision == "drop":
        tags.append("critic:dropped")
        if not verdict.get("supported", True):
            tags.append("critic:unsupported")
        if verdict.get("leaks_answer"):
            tags.append("critic:leaks_answer")
        return card, "deleted", tags
    if decision == "rewrite":
        rewrite = verdict.get("rewrite") or {}
        new_type = rewrite.get("type") or card["type"]
        if new_type == "basic" and rewrite.get("front") and rewrite.get("back"):
            card.update(
                type="basic",
                front=normalize_math(normalize_text(rewrite["front"])),
                back=normalize_math(normalize_text(rewrite["back"])),
                cloze_text=None, extra=None,
            )
            tags.append("critic:rewritten")
            return card, "ok", tags
        if new_type == "cloze" and rewrite.get("cloze_text") and is_valid_cloze(rewrite["cloze_text"]):
            card.update(
                type="cloze",
                cloze_text=normalize_math(normalize_text(rewrite["cloze_text"])),
                extra=normalize_math(normalize_text(rewrite.get("extra") or "")),
                front=None, back=None,
            )
            tags.append("critic:rewritten")
            return card, "ok", tags
        tags.append("critic:needs_review")
        return card, "needs_review", tags
    return card, "ok", tags


# ------------------------------------------------------------ review-data feedback
DIAGNOSE_SCHEMA = json_schema_format(
    "diagnosed_cards",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "cards": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "index": {"type": "integer"},
                        "diagnosis": {"type": "string"},
                        "action": {"type": "string", "enum": ["rewrite", "split", "keep"]},
                        "replacements": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "type": {"type": "string", "enum": ["basic", "cloze"]},
                                    "front": {"type": ["string", "null"]},
                                    "back": {"type": ["string", "null"]},
                                    "cloze_text": {"type": ["string", "null"]},
                                    "extra": {"type": ["string", "null"]},
                                },
                                "required": ["type", "front", "back", "cloze_text", "extra"],
                            },
                        },
                    },
                    "required": ["index", "diagnosis", "action", "replacements"],
                },
            }
        },
        "required": ["cards"],
    },
)

DIAGNOSE_SYSTEM = """You are a spaced-repetition coach. These cards are FAILING for a real student: their review history (lapses, "again" rate) is given. Using the source, diagnose why each card is hard to retain and fix it.

Typical causes: two facts on one card, an ambiguous prompt, an answer that is too long to recall verbatim, a cloze that hides an unguessable word, a missing cue that would anchor the fact, an interference with a sibling card.

Actions:
- rewrite: one replacement card that fixes the problem.
- split: two to four replacement cards, each atomic.
- keep: the card is fine; the student just needs more repetitions (use sparingly).
Replacements use the card JSON shape (basic: front+back; cloze: cloze_text+extra with {{c1::...}}). Ground everything in the source. Return only JSON."""


def diagnose_messages(cards_with_stats, source_text):
    source = source_text if len(source_text) <= SOURCE_CAP else source_text[:SOURCE_CAP] + "\n...[truncated]"
    blocks = []
    for i, (card, stats) in enumerate(cards_with_stats):
        block = _card_block(i, card)
        block += (
            f"\nreview stats: reps={stats.get('reps', 0)}, lapses={stats.get('lapses', 0)}, "
            f"again_rate={stats.get('again_rate', 0):.0%}"
        )
        blocks.append(block)
    return [
        {"role": "system", "content": DIAGNOSE_SYSTEM},
        {"role": "user", "content": f"SOURCE:\n{source}\n\nFAILING CARDS:\n" + "\n\n".join(blocks)},
    ]
