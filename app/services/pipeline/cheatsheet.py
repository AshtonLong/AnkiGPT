"""Optional phase between map and plan — boil each unit down to an exam cheat sheet.

Off unless the deck's `cheat_sheet` setting is on. Each live unit is rewritten as the
section of a cheat sheet a student would be allowed to bring into the exam: only what
earns marks, in plain language. The orchestrator then *replaces* the unit's text with
that section, so the planner sizes, the workers write from, the critic judges against
and the coverage audit back-fills from the cheat sheet, never the full source. That is
the point: nothing later in the run can re-inflate what the cheat sheet left out.

One call per unit and pure, so units are condensed in parallel and cached like workers.
"""

import logging

from ..chunking import clean_text
from ..llm import OpenRouterError, extract_json, json_schema_format

logger = logging.getLogger(__name__)

CHEATSHEET_PROMPT_VERSION = "cheatsheet-v1"

# Every line of a cheat sheet is examinable, whatever the mapper thought of the prose it
# came from, so a condensed unit sits at the top of the density scale. Leaving the
# original rating would have the planner budget a card for only a fraction of its lines.
CHEATSHEET_DENSITY = 5

CHEATSHEET_SCHEMA = json_schema_format(
    "cheat_sheet",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {"cheat_sheet": {"type": "string"}},
        "required": ["cheat_sheet"],
    },
)

CHEATSHEET_SYSTEM = """Some professors let students bring one cheat sheet into the exam room: a single page of notes, prepared in advance, that they may consult while they write the exam. Space on it is scarce, so a good one holds only what earns marks and leaves everything else out.

You are writing that cheat sheet for a student, one section of their study material at a time. You are given one section of the source; return the part of the cheat sheet that covers it.

WHAT EARNS A PLACE
Ask of every candidate line: would this student plausibly lose marks in the exam for not having it in front of them? Keep it only if the answer is yes. That usually means:
- definitions of the terms the student must know by name
- formulas and laws, with what each symbol means, its units, and the conditions under which the formula holds
- the steps of a process, mechanism or method, in order
- the distinctions between things that are easy to confuse
- rules together with their exceptions, limits and common mistakes
- the specific numbers, dates, names and thresholds the source stresses

WHAT STAYS OFF
- background, history, motivation and scene-setting
- anecdotes, asides, author commentary and "interesting to note" details
- examples that only illustrate a point already on the sheet (keep an example only when it is itself the thing to recognise in the exam)
- repetition, recaps and signposting ("as we saw", "in the next section")
- the intermediate steps of a derivation, unless reproducing the derivation is itself examinable
- citations, references and administrative text

HOW TO WRITE IT
- Plain, direct language. Strip the academic padding and the jargon that is only there as style. Keep the technical terms the student has to know, and say what each one means.
- Short headings with terse bullets beneath them. One fact per bullet.
- Every bullet must stand on its own as a complete statement: name its subject, spell out an abbreviation the first time it appears, and never lean on "it", "this", "the above" or on anything that is not on the sheet. The student's flashcards are written from this sheet and from nothing else, so a line that is cryptic here becomes a card nobody can answer.
- Reproduce formulas, symbols, numbers, units and names exactly as the source gives them.
- Use only what the source states. Add nothing from outside knowledge, and do not correct or extend the source.

HOW MUCH
Let the content decide the length. Wordy prose often comes down to a small fraction of itself; a dense table of definitions or formulas may keep most of itself. Do not pad, and do not drop something examinable just to be brief. Whatever is left off this sheet will not be studied, so leave a fact off only because it would not earn marks. If the section holds nothing a student would be examined on, return an empty string.

Return only JSON matching the schema."""


def _outline(unit, units):
    lines = []
    for u in units:
        if u.skipped:
            continue
        marker = "  <- this section" if u.idx == unit.idx else ""
        lines.append(f"- {u.title}{marker}")
    return "\n".join(lines)


def build_messages(unit, units, settings, doc_meta=None):
    """`units` is the whole document map, so the model can see where this section sits
    and how much of the sheet it deserves."""
    doc_meta = doc_meta or {}
    user = "\n".join(
        [
            f"Student context / exam: {settings.get('exam_context') or 'not given'}",
            f"Focus: {settings.get('focus') or 'all exam-useful material'}",
            f"Exclude: {settings.get('exclude') or 'none'}",
            f"Must-include terms (keep every one that this section covers): {settings.get('glossary') or 'none'}",
            f"Subject: {doc_meta.get('subject') or 'unknown'}",
            "",
            "The whole document, section by section:",
            _outline(unit, units),
            "",
            f"SECTION: {unit.title} (kind: {unit.kind})",
            "",
            unit.text,
        ]
    )
    return [
        {"role": "system", "content": CHEATSHEET_SYSTEM},
        {"role": "user", "content": user},
    ]


def write_cheat_sheet(client, messages):
    """Pure: one call. Returns {"cheat_sheet": str, "usage": {...}, "content": str, "model": str}.
    An empty `cheat_sheet` means the model found nothing examinable in the section."""
    result = client.chat("cheatsheet", messages, response_format=CHEATSHEET_SCHEMA)
    if result.finish_reason == "length":
        # Half a cheat sheet would silently drop the end of the unit; the caller keeps
        # the full text instead.
        raise OpenRouterError("The cheat sheet was cut off before it finished.")
    data = extract_json(result.content)
    if not isinstance(data, dict):
        raise ValueError("The cheat sheet response was not a JSON object.")
    return {
        "cheat_sheet": clean_text(str(data.get("cheat_sheet") or "")),
        "usage": dict(result.usage),
        "content": result.content,
        "model": result.model,
    }
