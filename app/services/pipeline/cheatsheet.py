"""Optional phase between map and plan — boil each unit down to an exam cheat sheet.

Off unless the deck's `cheat_sheet` setting is on. Each live unit is rewritten as the
section of a cheat sheet a student would be allowed to bring into the exam: every
examinable concept in its barest form, in plain language, built up from the first
principles the source states. The orchestrator then
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

High-yield mode (the deck's `high_yield` setting, on top of `cheat_sheet`) writes a
different sheet: the writer sorts every point into core (the exam asks the student to do
something with it), supporting (a core point needs it) and context (left off). Each line
on the sheet starts with a tag, `[calculate]` ... `[recall]` for core and `[supporting]`,
and that tag is what the rest of the run reads: the planner sizes a unit by its core
lines, the workers shape each card by its line's tag, and the coverage audit checks only
the core lines. The context points are kept beside the sheet (`left_off`), so the plan
review can bring one back (`retier`).

One call per unit and pure, so units are condensed in parallel and cached like workers.
"""

import logging
import re

from ..chunking import clean_text
from ..llm import OpenRouterError, extract_json, json_schema_format

logger = logging.getLogger(__name__)

CHEATSHEET_PROMPT_VERSION = "cheatsheet-v4"

EMPTY_REASON = "Nothing exam-critical to put on the cheat sheet."

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

CHEATSHEET_SYSTEM = """Some professors let students bring a cheat sheet into the exam room: a page or two of notes, prepared in advance, that they may consult while they write the exam. Paper is limited, so nothing goes on it at length. A good sheet is the course with everything but its bare bones removed: every concept that could be examined is there, each one is cut down to the fewest words that still say it correctly, and the fundamentals the course is built on are all there with what follows from them.

You are writing that cheat sheet for a student, one section of their study material at a time. You are given one section of the source; return the part of the cheat sheet that covers it.

FIRST PRINCIPLES
The sheet is built from first principles. A first principle is something the rest of the section rests on and that the section does not derive from anything else: a fundamental law, an axiom or postulate, a defining equation, a core assumption or model, a conservation rule or other constraint, the underlying cause or mechanism of what happens. A student who has the first principles in front of them can rebuild a result they have forgotten and work a question they have never seen; a student who has only the results cannot.
- Every first principle the section states goes on the sheet, without exception, whether the source gives it a whole slide or mentions it in passing.
- State each one in full: the principle itself in one plain statement, with the conditions or assumptions under which it holds.
- Put a principle before what follows from it, and tie each result, formula, rule and step to the principle it comes from wherever the source makes that link. A few words do it ("because ...", "follows from ..."), in the same bullet as the result.
- The reason a rule holds or a thing happens, as the source gives it, is a first principle and not background. It stays on.
- A derivation keeps the principle it starts from and the step that turns that principle into the result. The algebra in between stays off.
- Only the source's first principles. Do not supply a principle, a reason or a derivation the source does not state, however well you know it.

EVERYTHING, IN ITS BARE FORM
The sheet is complete in breadth and minimal in depth: every examinable concept in the section gets its line, and none gets a paragraph. Ask of every concept what a student would lose marks for not having in front of them, and write only that. It usually comes to:
- what the concept is: each term the student must know by name, defined in one plain statement
- the first principle behind it: why it is true or why it happens, where the source says
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
- background, history, motivation and scene-setting. A first principle is none of these, even when the source presents it as an introduction
- anecdotes, asides, author commentary and "interesting to note" details
- repetition, recaps and signposting ("as we saw", "in the next section")
- the intermediate steps of a derivation, unless reproducing the derivation is itself examinable. Where it starts and the step that gets to the result stay on
- citations, references and administrative text
- difficulty for its own sake: formal phrasing, hedging, jargon used as style, and notation heavier than the idea needs. None of it earns marks

DIAGRAMS
A section may come with diagrams. They are listed before the section text, each with a marker such as [[Figure 3]] and a note of what it shows. They were read from the page images, and the student has each diagram itself on the sheet.
- Give every listed diagram a place: write its marker, exactly as given, alone on its own line where the diagram belongs among the bullets.
- Do not turn what a diagram shows into bullets; the diagram carries that. Bullets are for what the text says.
- Use only the markers you were given.

HOW TO WRITE IT
- Use the plainest wording that is still exactly right. Keep the technical terms the student has to know, and say what each one means.
- Short headings with terse bullets beneath them. One fact per bullet; a result and the principle it comes from count as one.
- Every bullet must stand on its own as a complete statement: name its subject, spell out an abbreviation the first time it appears, and never lean on "it", "this", "the above" or on anything that is not on the sheet. The student's flashcards are written from this sheet and from nothing else, so a line that is cryptic here becomes a card nobody can answer.
- Reproduce formulas, symbols, numbers, units and names exactly as the source gives them.
- Math: \\( ... \\) inline and \\[ ... \\] for a formula on a line of its own. Never $...$.
- Use only what the source states. Add nothing from outside knowledge, and do not correct or extend the source. A line the source does not support has no place on the sheet, however true it is.

HOW MUCH
The whole course has to fit on a page or two, and this section gets only its share. Get short by cutting the words around an idea, never by cutting the idea: wordy prose comes down to a small fraction of itself, and a dense table of definitions or formulas keeps nearly all of its content while losing its sentences. Do not drop a concept, a first principle, a condition or an edge case to save space, and do not pad. Whatever is left off this sheet will not be studied. If the section holds nothing a student would be examined on, return an empty string.

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


# ------------------------------------------------------------- high-yield sheet
HIGH_YIELD_PROMPT_VERSION = "cheatsheet-hy-v1"

# What the exam asks a student to do with a core line, in the order the plan review lists
# them. A line tagged with one of these gets a card; a `supporting` line is on the sheet
# only because a core line needs it.
ASKS = ("calculate", "apply", "explain", "distinguish", "define", "recall")
SUPPORTING = "supporting"
LEFT_OFF = "off"
TIERS = ("core", "supporting", "context")

LINE_TAG_RE = re.compile(r"^(\s*[-*•]\s+)\[(" + "|".join((*ASKS, SUPPORTING)) + r")\]\s*", re.IGNORECASE)
ITEM_TAG_RE = re.compile(r"^\[(" + "|".join((*ASKS, SUPPORTING)) + r")\]\s*", re.IGNORECASE)

HIGH_YIELD_SCHEMA = json_schema_format(
    "high_yield_sheet",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "sections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "heading": {"type": "string"},
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "text": {"type": "string"},
                                    "tier": {"type": "string", "enum": list(TIERS)},
                                    "asks": {"type": "string", "enum": list(ASKS)},
                                },
                                "required": ["text", "tier", "asks"],
                            },
                        },
                    },
                    "required": ["heading", "items"],
                },
            }
        },
        "required": ["sections"],
    },
)

HIGH_YIELD_SYSTEM = """You are writing a high-yield exam sheet for a student, one section of their study material at a time. You are given one section of the source; return the part of the sheet that covers it.

WHAT AN EXAM ACTUALLY ASKS
Most of a textbook chapter or a lecture deck is not what the exam asks about. The exam asks the student to do things: work a calculation, apply a method to a situation they have not seen, explain why something happens, tell two things apart, use the terms of the course correctly, and recall the few facts the course insists on. Case studies, stories, history, motivation and repeated examples are there to teach those things; they are rarely asked about themselves. A student who is asked in the exam how to set up a case-study analysis needs the method of setting it up, not the details of the case the lecture used.

Every point you keep becomes a flashcard the student reviews for weeks, so a point has to earn its place. Go through the section and sort every examinable point into one of three tiers.

CORE: an examiner would plausibly set a question that asks the student to do something with it. Give each core point the thing the exam asks (`asks`):
- calculate: a formula or quantitative method the student must use. Write the formula, what each symbol means with its units, and when it applies.
- apply: a method, procedure, framework or decision rule the student must carry out on a new situation: how to set up an analysis, how to choose an approach or test, the steps of a process. Write the steps in order, or the rule for choosing and its reason, as one point.
- explain: a cause, mechanism or reason the student must be able to give: why something holds or happens.
- distinguish: the difference between things that are easy to confuse, stated as the difference itself.
- define: a term the student must know and use, in one plain statement.
- recall: a specific fact, number, threshold, name or exception that the source stresses as one to know.

SUPPORTING: not asked on its own, but a core point cannot be answered without it: a constant, a unit, an assumption, a term used inside a core point. Keep supporting points few. Give each one the `asks` it would have if it were core.

CONTEXT: everything else the section says that a student might be tempted to memorise: the particulars of case studies and worked examples (the company, the patient, the figures of one exercise), history, background, motivation, anecdotes, further examples of something already covered, and points the source mentions without stressing. Context points are left off the sheet. Give each one the `asks` it would have if it were core.

CASE STUDIES AND WORKED EXAMPLES
- A case study or worked example usually teaches a method. When it does, the method is a core point (usually apply or calculate), stated in general terms a student can carry to a new case: the steps it takes and the decisions it makes, with their reasons, in the order the source takes them. Only a method the source shows; do not invent steps.
- The particulars of the case are context. Keep a case's particulars as core only when the source presents the case itself as something to know, such as a landmark case or a named experiment the course builds on; then it is a recall point.

FIRST PRINCIPLES
A first principle the section states (a law, a defining equation, a core assumption, the cause behind a result) is what lets the student rebuild a result and work a question they have not seen. It is core (usually explain or calculate), with the conditions under which it holds.

HOW TO JUDGE
- Judge by the student's exam context when it is given. If the exam is calculations and applied questions, a definition or a recall fact is core only when a question cannot be done without it.
- Be strict. A typical section has a handful of core points; a section that is mostly narrative or case study may have one or none. When you are unsure whether an examiner would ask a point, make it supporting rather than core.
- One point is one thing the exam could ask, and it becomes one card. Do not split one point into several, and do not join two independent points into one.
- Edge cases that cost marks (the exception to a rule, a limit, a common mistake the source warns about) are core.

HOW TO WRITE A POINT
- Every point, in every tier, is a complete statement that stands on its own: name its subject, spell out an abbreviation the first time, and never lean on "it", "this" or "the above". A context point may be moved back onto the sheet by the student, and then it is all a card is written from.
- Use the plainest wording that is still exactly right, and as few words as that takes. Keep the technical terms the student has to know.
- Reproduce formulas, symbols, numbers, units and names exactly as the source gives them.
- Math: \\( ... \\) inline. Never $...$.
- Group points under short headings, in the order the source takes them.
- Use only what the source states. Add nothing from outside knowledge, and do not correct or extend the source.
- A long passage left off can be one or two context points; you need not list every detail of it.

DIAGRAMS
A section may come with diagrams, listed before the section text, each with a marker such as [[Figure 3]] and a note of what it shows. Give every listed diagram a place: one point whose text is its marker alone, exactly as given, with tier core, under the heading where it belongs. Do not turn what a diagram shows into points; the diagram carries that.

If the section holds nothing a student would be examined on, return the context points alone, or no sections at all.

Return only JSON matching the schema."""


def high_yield(settings):
    """Whether a deck's cheat sheet is the high-yield kind."""
    settings = settings or {}
    return bool(settings.get("cheat_sheet") and settings.get("high_yield"))


def build_high_yield_messages(unit, units, settings, doc_meta=None, figures=None):
    """The same brief as `build_messages`, for the high-yield writer."""
    messages = build_messages(unit, units, settings, doc_meta, figures)
    messages[0] = {"role": "system", "content": HIGH_YIELD_SYSTEM}
    return messages


def write_high_yield_sheet(client, messages):
    """Pure: one call. Returns {"cheat_sheet": str, "left_off": [...], "usage", "content",
    "model"}. `cheat_sheet` holds the core and supporting lines, each tagged; `left_off`
    the context points, each {"heading", "text", "asks"}."""
    result = client.chat("cheatsheet", messages, response_format=HIGH_YIELD_SCHEMA)
    if result.finish_reason == "length":
        raise OpenRouterError("The cheat sheet was cut off before it finished.")
    data = extract_json(result.content)
    if not isinstance(data, dict):
        raise ValueError("The cheat sheet response was not a JSON object.")
    sheet, left_off = render_high_yield(data)
    return {"cheat_sheet": sheet, "left_off": left_off, "usage": dict(result.usage), "content": result.content,
            "model": result.model}


def _one_line(value):
    return " ".join(str(value or "").split())


def _asks(value):
    value = str(value or "").strip().lower()
    return value if value in ASKS else "recall"


def render_high_yield(data):
    """The writer's sections as sheet text, plus the points left off. A sheet line is
    `- [tag] text` under a `### heading`; a diagram is its marker on a line of its own."""
    lines, left_off = [], []
    for section in data.get("sections") or []:
        if not isinstance(section, dict):
            continue
        heading = _one_line(section.get("heading"))
        body = []
        for item in section.get("items") or []:
            if not isinstance(item, dict):
                continue
            text = _one_line(item.get("text"))
            if not text:
                continue
            if FIGURE_LINE_RE.match(text):
                body.append(text)
                continue
            tier = str(item.get("tier") or "").strip().lower()
            if tier == "context":
                left_off.append({"heading": heading, "text": text, "asks": _asks(item.get("asks"))})
            else:
                body.append(f"- [{SUPPORTING if tier == 'supporting' else _asks(item.get('asks'))}] {text}")
        if body:
            lines += ["", f"### {heading}", *body] if heading else ["", *body]
    return clean_text("\n".join(lines)), left_off


def line_tag(line):
    """The tag a sheet line starts with, lowercased, or None."""
    match = LINE_TAG_RE.match(line or "")
    return match.group(2).lower() if match else None


def yield_counts(text):
    """(core lines, supporting lines) of a high-yield section."""
    core = supporting = 0
    for line in (text or "").split("\n"):
        tag = line_tag(line)
        if tag == SUPPORTING:
            supporting += 1
        elif tag:
            core += 1
    return core, supporting


def yield_target(text):
    """The cards a high-yield section is worth: one per core line, and one for every three
    supporting lines, since a core card sometimes cannot be asked without one. None for
    text that is not a high-yield section."""
    core, supporting = yield_counts(text)
    if not core and not supporting:
        return None
    return core + supporting // 3


def core_text(text):
    """A high-yield section cut to its headings and core lines: what the coverage audit
    checks the cards against. Supporting lines and diagrams are not owed a card."""
    kept = []
    for line in (text or "").split("\n"):
        tag = line_tag(line)
        if HEADING_RE.match(line.strip()) or (tag and tag != SUPPORTING):
            kept.append(line)
    # A heading left with no core line under it goes too.
    out = [line for i, line in enumerate(kept) if line_tag(line) or (i + 1 < len(kept) and line_tag(kept[i + 1]))]
    return "\n".join(out)


def sheet_lines(text):
    """[(line number, tag, text without its tag)] for the tagged lines of a section."""
    rows = []
    for n, line in enumerate((text or "").split("\n")):
        match = LINE_TAG_RE.match(line)
        if match:
            rows.append((n, match.group(2).lower(), line[match.end():]))
    return rows


def retier(text, left_off, line_choices, left_choices):
    """A section after the student re-sorted it on the plan review. `line_choices` maps a
    tagged line's number to its new tag, `left_choices` an index of `left_off` to one; a
    tag of LEFT_OFF takes a line off the sheet. Returns (text, left_off)."""
    blocks = [[None, []]]  # [heading line, [lines]] in order
    heading_text = {}
    for n, line in enumerate((text or "").split("\n")):
        heading = HEADING_RE.match(line.strip())
        if heading:
            blocks.append([line, []])
            heading_text[_one_line(heading.group(1) or heading.group(2)).lower()] = blocks[-1]
            continue
        match = LINE_TAG_RE.match(line)
        current = blocks[-1]
        choice = line_choices.get(n) if match else None
        if choice is None or choice == match.group(2).lower():
            current[1].append(line)
        elif choice == LEFT_OFF:
            name = HEADING_RE.match(current[0].strip()) if current[0] else None
            old = match.group(2).lower()
            left = _one_line(name.group(1) or name.group(2)) if name else ""
            left_off = [*left_off, {"heading": left, "text": line[match.end():].strip(), "asks": _asks(old)}]
        else:
            current[1].append(f"{match.group(1)}[{choice}] {line[match.end():]}")
    kept_left = []
    for i, item in enumerate(left_off):
        choice = left_choices.get(i)
        if not choice or choice == LEFT_OFF:
            kept_left.append(item)
            continue
        block = heading_text.get(_one_line(item.get("heading")).lower())
        if block is None:
            block = [f"### {item['heading']}", []] if item.get("heading") else blocks[0]
            if block is not blocks[0]:
                blocks.append(block)
                heading_text[_one_line(item["heading"]).lower()] = block
        block[1].append(f"- [{choice}] {item['text']}")
    out = []
    for heading, lines in blocks:
        body = [line for line in lines if line.strip()]
        if not body:
            continue
        if heading is not None:
            out += ["", heading]
        out += body
    return clean_text("\n".join(out)), kept_left

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
            # A high-yield line opens with its tag; the page shows it as a label.
            tag = ITEM_TAG_RE.match(item.group(3))
            body = item.group(3)[tag.end():] if tag else item.group(3)
            blocks[-1]["items"].append({
                "text": body, "label": item.group(2), "sub": len(item.group(1)) >= 2,
                "example": bool(EXAMPLE_RE.match(body)), "tag": tag.group(1).lower() if tag else None,
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
