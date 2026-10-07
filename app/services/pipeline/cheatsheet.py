"""Optional phase between map and plan — boil each unit down to an exam cheat sheet.

Off unless the deck's `cheat_sheet` setting is on. Each live unit is rewritten as the
section of a cheat sheet a student would be allowed to bring into the exam: every
examinable concept in its barest form, in plain language. The orchestrator then
*replaces* the unit's text with that section, so the planner sizes, the workers write
from, the critic judges against and the coverage audit back-fills from the cheat sheet,
never the full source. That is the point: nothing later in the run can re-inflate what
the cheat sheet left out.

The sheet keeps the source's diagrams. Figures are read by the vision pass first, the
writer is told what each one shows, and a kept diagram is one line of the section: a
`[[Figure N]]` marker and its caption. `sheet_blocks` parses a section for the HTML page,
which puts the image itself where the marker sits. Once the planner has ruled on the
figures, the diagrams it gave no cards come off again (`remove_figures`), so what is on
the sheet and what gets image cards are the same set.

One call per unit and pure, so units are condensed in parallel and cached like workers.
"""

import logging
import re

from ..chunking import clean_text
from ..llm import OpenRouterError, extract_json, json_schema_format

logger = logging.getLogger(__name__)

CHEATSHEET_PROMPT_VERSION = "cheatsheet-v3"

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

CHEATSHEET_SYSTEM = """Some professors let students bring a cheat sheet into the exam room: a page or two of notes, prepared in advance, that they may consult while they write the exam. Paper is limited, so nothing goes on it at length. A good sheet is the course with everything but its bare bones removed: every concept that could be examined is there, and each one is cut down to the fewest words that still say it correctly.

You are writing that cheat sheet for a student, one section of their study material at a time. You are given one section of the source; return the part of the cheat sheet that covers it.

EVERYTHING, IN ITS BARE FORM
The sheet is complete in breadth and minimal in depth: every examinable concept in the section gets its line, and none gets a paragraph. Ask of every concept what a student would lose marks for not having in front of them, and write only that. It usually comes to:
- what the concept is: each term the student must know by name, defined in one plain statement
- formulas and laws, with what each symbol means, its units, and the conditions under which the formula holds
- the steps of a process, mechanism or method, in order, one short line each
- the difference between things that are easy to confuse, stated as the difference itself
- edge cases: the exceptions to a rule, its limits and boundary conditions, special cases, and the mistakes the source warns about. Marks are lost here, so an edge case keeps its line even when space is tight
- the specific numbers, dates, names and thresholds the source stresses

EXAMPLES
A concept may keep one example, and only an example the source itself gives.
- If the source gives an example of a concept, keep one. If it gives several, keep the one that shows the concept most directly and leave the others out.
- Cut the example to its bare form too: what is given, the step that matters, the result. One or two lines, written directly under its concept and opening with "Example:".
- If the source gives no example of a concept, the sheet has none. Never make one up, and never alter, extend or combine the source's examples.

WHAT STAYS OFF
- every example beyond the one a concept keeps: further worked problems, repeated cases, practice questions
- background, history, motivation and scene-setting
- anecdotes, asides, author commentary and "interesting to note" details
- repetition, recaps and signposting ("as we saw", "in the next section")
- the intermediate steps of a derivation, unless reproducing the derivation is itself examinable
- citations, references and administrative text
- difficulty for its own sake: formal phrasing, hedging, jargon used as style, and notation heavier than the idea needs. None of it earns marks

DIAGRAMS
A section may come with diagrams. They are listed before the section text, each with a marker such as [[Figure 3]] and a note of what it shows. They were read from the page images, and the student has each diagram itself on the sheet.
- Give every listed diagram a place: write its marker, exactly as given, alone on its own line where the diagram belongs among the bullets.
- Do not turn what a diagram shows into bullets; the diagram carries that. Bullets are for what the text says.
- Use only the markers you were given.

HOW TO WRITE IT
- Use the plainest wording that is still exactly right. Keep the technical terms the student has to know, and say what each one means.
- Short headings with terse bullets beneath them. One fact per bullet.
- Every bullet must stand on its own as a complete statement: name its subject, spell out an abbreviation the first time it appears, and never lean on "it", "this", "the above" or on anything that is not on the sheet. The student's flashcards are written from this sheet and from nothing else, so a line that is cryptic here becomes a card nobody can answer.
- Reproduce formulas, symbols, numbers, units and names exactly as the source gives them.
- Math: \\( ... \\) inline and \\[ ... \\] for a formula on a line of its own. Never $...$.
- Use only what the source states. Add nothing from outside knowledge, and do not correct or extend the source. A line the source does not support has no place on the sheet, however true it is.

HOW MUCH
The whole course has to fit on a page or two, and this section gets only its share. Get short by cutting the words around an idea, never by cutting the idea: wordy prose comes down to a small fraction of itself, and a dense table of definitions or formulas keeps nearly all of its content while losing its sentences. Do not drop a concept, a condition or an edge case to save space, and do not pad. Whatever is left off this sheet will not be studied. If the section holds nothing a student would be examined on, return an empty string.

Return only JSON matching the schema."""

# How much of each part of a figure's analysis the writer is shown: enough to place the
# diagram and to know what it already covers, without the listing outweighing the section.
FIGURE_NOTE_CHARS = 600

FIGURE_LINE_RE = re.compile(r"^\s*(?:[-*•]\s*)?\[\[\s*figure\s+(\d+)\s*\]\]", re.IGNORECASE)
FIGURE_MENTION_RE = re.compile(r"\[\[\s*figure\s+(\d+)\s*\]\]", re.IGNORECASE)


def _outline(unit, units):
    lines = []
    for u in units:
        if u.skipped:
            continue
        marker = "  <- this section" if u.idx == unit.idx else ""
        lines.append(f"- {u.title}{marker}")
    return "\n".join(lines)


def figure_marker(number):
    return f"[[Figure {number}]]"


def _figure_listing(figures):
    lines = ["Diagrams in this section:"]
    for fig in figures:
        where = ", ".join(filter(None, [f"p.{fig['page']}" if fig.get("page") else "", fig.get("kind") or ""]))
        lines.append(f"{figure_marker(fig['number'])}{f' ({where})' if where else ''} {fig.get('caption') or ''}".rstrip())
        for label, value in (
            ("Shows", fig.get("description")),
            ("Labelled", "; ".join(fig.get("parts") or [])),
            ("Conveys", "; ".join(fig.get("facts") or [])),
        ):
            if value:
                lines.append(f"  {label}: {' '.join(value.split())[:FIGURE_NOTE_CHARS]}")
    return lines + [""]


def build_messages(unit, units, settings, doc_meta=None, figures=None):
    """`units` is the whole document map, so the model can see where this section sits
    and how much of the sheet it deserves. `figures` are the section's diagrams, each
    {number, page, kind, caption, description, parts, facts}."""
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
            *(_figure_listing(figures) if figures else []),
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


# ------------------------------------------------------------ diagrams on the sheet
def place_figures(text, figures):
    """Settle the diagrams of one section. The writer only chooses where a diagram sits:
    each of `figures` ends up on exactly one line of its own, as its marker and caption,
    and one the writer left out is added at the end. The vision pass saw the image and
    judged it examinable; the writer never saw it, so it does not get to cut it. Markers
    for figures the section does not have are removed."""
    by_number = {fig["number"]: fig for fig in figures}
    placed = set()
    lines = []
    for line in (text or "").split("\n"):
        match = FIGURE_LINE_RE.match(line)
        if not match:
            # A marker inside a sentence is a mention, not a placement.
            lines.append(FIGURE_MENTION_RE.sub(lambda m: f"Figure {m.group(1)}", line))
            continue
        number = int(match.group(1))
        if number in by_number and number not in placed:
            placed.add(number)
            lines.append(_figure_line(by_number[number]))
    missing = [fig for fig in figures if fig["number"] not in placed]
    if missing:
        lines += ["", *(_figure_line(fig) for fig in missing)]
    return clean_text("\n".join(lines))


def _figure_line(figure):
    caption = " ".join((figure.get("caption") or "").split())
    return f"{figure_marker(figure['number'])} {caption}".rstrip()


def remove_figures(text, numbers):
    """A section without the diagrams numbered `numbers`. The sheet is written before the
    plan with every diagram on it; the ones the planner then gives no cards come off."""
    kept = []
    for line in (text or "").split("\n"):
        match = FIGURE_LINE_RE.match(line)
        if not (match and int(match.group(1)) in numbers):
            kept.append(line)
    return clean_text("\n".join(kept))


# ------------------------------------------------------------------ the HTML page
HEADING_RE = re.compile(r"^(?:#{1,6}\s+(.+?)\s*#*|\*\*([^*]+)\*\*:?)$")
ITEM_RE = re.compile(r"^(\s*)(?:[-*•]|(\d+[.)]))\s+(.*)$")
TABLE_RULE_RE = re.compile(r"^\|?\s*:?-{2,}[-:|\s]*$")
EXAMPLE_RE = re.compile(r"^\**(?:worked\s+)?example\b", re.IGNORECASE)
# What a line may hold besides plain text: a code span, maths, bold. The scan runs left to
# right and code is tried first, so a `$` or `|` inside backticks stays code. A lone `$`
# opens maths only the way Pandoc reads it (no space inside the pair, nothing joined on
# outside it), which leaves prices alone.
INLINE_RE = re.compile(
    r"`(?P<code>[^`]+)`"
    r"|\\\((?P<paren>.+?)\\\)"
    r"|\\\[(?P<bracket>.+?)\\\]"
    r"|\$\$(?P<dollars>.+?)\$\$"
    r"|(?<![\\$\w])\$(?P<dollar>[^\s$](?:[^$]*?[^\s$\\])?)\$(?![\w$])"
    r"|\*\*(?P<bold>.+?)\*\*"
)
# The maths groups of INLINE_RE, and whether each one is a displayed formula.
MATH_GROUPS = {"paren": False, "bracket": True, "dollars": True, "dollar": False}


def sheet_blocks(text):
    """One section of the sheet as blocks for the page template: heading, list, table,
    figure and text. The writer is only asked for headings and bullets, so this reads
    what models commonly produce and treats the rest as plain text (a unit whose cheat
    sheet failed still holds its full source)."""
    blocks = []

    def last(kind):
        return blocks[-1] if blocks and blocks[-1]["type"] == kind else None

    after_blank = True
    for raw in (text or "").split("\n"):
        line = raw.strip()
        blank, after_blank = after_blank, not line
        if not line:
            continue
        figure = FIGURE_LINE_RE.match(raw)
        heading = HEADING_RE.match(line)
        item = ITEM_RE.match(raw)
        if figure:
            blocks.append({"type": "figure", "number": int(figure.group(1))})
        elif heading:
            blocks.append({"type": "heading", "text": heading.group(1) or heading.group(2)})
        elif item:
            if blank or not last("list"):
                blocks.append({"type": "list", "items": []})
            blocks[-1]["items"].append({
                "text": item.group(3), "label": item.group(2), "sub": len(item.group(1)) >= 2,
                "example": bool(EXAMPLE_RE.match(item.group(3))),
            })
        elif line.startswith("|"):
            if TABLE_RULE_RE.match(line):
                continue
            if blank or not last("table"):
                blocks.append({"type": "table", "rows": []})
            blocks[-1]["rows"].append(_cells(line))
        elif not blank and last("list") and raw[:1].isspace():
            # An indented line under a bullet continues that bullet.
            blocks[-1]["items"][-1]["text"] += " " + line
        elif not blank and last("text"):
            blocks[-1]["text"] += " " + line
        else:
            blocks.append({"type": "text", "text": line, "example": bool(EXAMPLE_RE.match(line))})
    return blocks


def _cells(row):
    """The cells of one table row. A `|` inside maths or a code span belongs to its cell."""
    row = row.strip("|")
    kept = [m.span() for m in INLINE_RE.finditer(row) if m.lastgroup != "bold"]
    cells, start = [], 0
    for pos, char in enumerate(row):
        if char == "|" and not any(a <= pos < b for a, b in kept):
            cells.append(row[start:pos].strip())
            start = pos + 1
    return cells + [row[start:].strip()]
