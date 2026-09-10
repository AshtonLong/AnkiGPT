"""Phase 0 — map the document instead of chunking it.

1. Deterministic skeleton: split on headings (Markdown from the PDF extractor, ALL-CAPS
   lines, "Title:" lines); paragraph-pack when a document has no structure at all.
2. One cheap model call sees only the skeleton (titles, sizes, previews) and groups the
   candidates into semantic *units* with a kind, density, prerequisites, and skip
   verdicts for front matter, references, exercises, and recaps.
3. Oversized units are split deterministically so a worker never sees more than
   `max_unit_chars` at once.

Short sources skip the model entirely and become a single unit.
"""

import logging
import re
from dataclasses import dataclass, field

from ..chunking import chunk_text, clean_text
from ..llm import extract_json, json_schema_format

logger = logging.getLogger(__name__)

MAP_PROMPT_VERSION = "map-v1"

UNIT_KINDS = (
    "prose",
    "definitions",
    "formulas",
    "procedure",
    "comparison",
    "worked_example",
    "figure_heavy",
    "argument",
    "front_matter",
    "summary",
    "exercises",
    "references",
)

HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
SINGLE_UNIT_MAX_CHARS = 3000
FALLBACK_BLOCK_CHARS = 3000


@dataclass
class Candidate:
    id: int
    title: str
    text: str
    char_start: int
    char_end: int
    level: int = 2

    @property
    def chars(self):
        return len(self.text)

    def preview(self, n=260):
        body = re.sub(r"\s+", " ", self.text).strip()
        return body[:n]


@dataclass
class Unit:
    idx: int
    title: str
    text: str
    char_start: int
    char_end: int
    kind: str = "prose"
    density: int = 3
    depends_on: list = field(default_factory=list)
    skipped: bool = False
    skip_reason: str = None
    summary: str = None
    page_start: int = None
    page_end: int = None
    candidate_ids: list = field(default_factory=list)

    @property
    def chars(self):
        return len(self.text)

    def to_dict(self):
        return {
            "idx": self.idx,
            "title": self.title,
            "kind": self.kind,
            "density": self.density,
            "chars": self.chars,
            "depends_on": list(self.depends_on),
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "summary": self.summary,
            "page_start": self.page_start,
            "page_end": self.page_end,
        }


def _looks_like_heading(line):
    stripped = line.strip()
    if not stripped or len(stripped) > 90:
        return None
    m = HEADING_RE.match(stripped)
    if m:
        return len(m.group(1)), m.group(2).strip()
    bold = re.match(r"^\*\*(.+?)\*\*$", stripped)
    if bold and len(bold.group(1)) <= 80:
        return 3, bold.group(1).strip()
    letters = [c for c in stripped if c.isalpha()]
    if len(letters) >= 3 and all(c.isupper() for c in letters) and len(stripped.split()) <= 12:
        return 2, stripped.title()
    if stripped.endswith(":") and len(stripped.split()) <= 10 and stripped[0].isupper():
        return 3, stripped.rstrip(":")
    return None


def skeleton(text, max_unit_chars=14000):
    """Split cleaned text into heading-delimited candidates with char offsets."""
    text = clean_text(text)
    if not text:
        return []
    lines = text.split("\n")
    candidates = []
    cur_title, cur_level, cur_lines, cur_start = None, 2, [], 0
    pos = 0

    def flush(end_pos):
        nonlocal cur_lines
        body = "\n".join(cur_lines).strip()
        if body:
            start = cur_start
            candidates.append(Candidate(len(candidates), cur_title or "", body, start, end_pos, cur_level))
        cur_lines = []

    for line in lines:
        line_start = pos
        pos += len(line) + 1
        heading = _looks_like_heading(line)
        if heading is not None:
            flush(line_start)
            cur_level, cur_title = heading
            cur_start = pos
            continue
        if not cur_lines:
            cur_start = line_start
        cur_lines.append(line)
    flush(len(text))

    # Untitled first block is fine; a candidate with no title after a heading gets one
    # from its first line so the planner has something to read.
    for c in candidates:
        if not c.title:
            first = re.sub(r"\s+", " ", c.text.strip().split("\n")[0])[:70]
            c.title = first or "Untitled"

    # No structure at all: paragraph-pack so the mapper still has parts to group.
    if len(candidates) <= 1 and text and len(text) > FALLBACK_BLOCK_CHARS * 1.5:
        base_title = candidates[0].title if candidates else ""
        body = candidates[0].text if candidates else text
        offset = candidates[0].char_start if candidates else 0
        candidates = _pack_blocks(body, min(FALLBACK_BLOCK_CHARS, max_unit_chars), base_title=base_title, offset=offset)

    # Bound candidate size so a single monster section can't overflow a unit.
    bounded = []
    for c in candidates:
        if c.chars <= max_unit_chars:
            bounded.append(c)
            continue
        parts = chunk_text(c.text, max_chars=max_unit_chars)
        offset = c.char_start
        for i, (_t, body) in enumerate(parts):
            start = text.find(body[:80], offset) if body else offset
            if start < 0:
                start = offset
            end = start + len(body)
            bounded.append(Candidate(0, f"{c.title} ({i + 1}/{len(parts)})", body, start, end, c.level))
            offset = end
    for i, c in enumerate(bounded):
        c.id = i
    return bounded


def _pack_blocks(text, block_chars, base_title="", offset=0):
    parts = chunk_text(text, max_chars=block_chars)
    out = []
    cursor = 0
    for i, (title, body) in enumerate(parts):
        start = text.find(body[:80], cursor) if body else cursor
        if start < 0:
            start = cursor
        end = start + len(body)
        if base_title:
            label = f"{base_title} ({i + 1}/{len(parts)})"
        else:
            label = title or re.sub(r"\s+", " ", body.strip().split("\n")[0])[:70] or f"Part {i + 1}"
        out.append(Candidate(i, label, body, offset + start, offset + end, 2))
        cursor = end
    return out


MAP_SCHEMA = json_schema_format(
    "document_map",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "subject": {"type": "string"},
            "document_summary": {"type": "string"},
            "units": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "title": {"type": "string"},
                        "candidate_ids": {"type": "array", "items": {"type": "integer"}},
                        "kind": {"type": "string", "enum": list(UNIT_KINDS)},
                        "density": {"type": "integer"},
                        "depends_on": {"type": "array", "items": {"type": "integer"}},
                        "skip": {"type": "boolean"},
                        "skip_reason": {"type": ["string", "null"]},
                        "summary": {"type": "string"},
                    },
                    "required": [
                        "title", "candidate_ids", "kind", "density", "depends_on", "skip", "skip_reason", "summary",
                    ],
                },
            },
        },
        "required": ["subject", "document_summary", "units"],
    },
)

MAP_SYSTEM = """You are the document mapper for a flashcard generator. You see the SKELETON of a study document — section titles, sizes and short previews — and you organise it into study units.

Rules
- Every candidate id must appear in exactly one unit. Do not invent ids.
- Group ADJACENT candidates that cover one topic into a single unit; split nothing (candidates are already bounded).
- Keep units focused: a unit should be one teachable topic. Prefer several medium units over one giant one; a unit should stay under roughly {max_chars} characters (sizes are given).
- `kind`: prose | definitions | formulas | procedure | comparison | worked_example | figure_heavy | argument | front_matter | summary | exercises | references.
- `density` 1-5: how much exam-testable content per character (5 = dense definitions/formulas, 1 = filler).
- `depends_on`: indexes (0-based, in the order you list units) of units a student must know first.
- `skip`: true for front matter, tables of contents, prefaces, acknowledgements, references, bibliographies, exercise lists without answers, and chapter recaps that only repeat earlier units. Give a one-line `skip_reason`.
- `summary`: one or two sentences on what the unit teaches, written for a planner who has not read it.
Return only JSON matching the schema."""


def _mapper_messages(candidates, settings, max_unit_chars):
    lines = []
    for c in candidates:
        lines.append(f"[{c.id}] {c.title!r} · {c.chars} chars · preview: {c.preview()}")
    focus = settings.get("focus") or ""
    exclude = settings.get("exclude") or ""
    context = settings.get("exam_context") or ""
    user = "\n".join(
        [
            f"Student context: {context or 'not given'}",
            f"Focus: {focus or 'all exam-useful material'}",
            f"Exclude: {exclude or 'none'}",
            "",
            f"Skeleton ({len(candidates)} candidates):",
            *lines,
        ]
    )
    return [
        {"role": "system", "content": MAP_SYSTEM.replace("{max_chars}", str(max_unit_chars))},
        {"role": "user", "content": user},
    ]


def _units_from_candidates_only(candidates):
    return [
        Unit(
            idx=i, title=c.title, text=c.text, char_start=c.char_start, char_end=c.char_end,
            candidate_ids=[c.id],
        )
        for i, c in enumerate(candidates)
    ]


def _assemble_units(candidates, mapping, max_unit_chars):
    """Turn the mapper's grouping into Units, repairing anything inconsistent."""
    by_id = {c.id: c for c in candidates}
    seen = set()
    raw_units = []
    for entry in mapping.get("units") or []:
        ids = []
        for i in entry.get("candidate_ids") or []:
            if isinstance(i, int) and i in by_id and i not in seen and i not in ids:
                ids.append(i)
        if not ids:
            continue
        seen.update(ids)
        ids.sort()
        raw_units.append((entry, ids))
    # Any candidate the model forgot becomes its own unit, in document order.
    for c in candidates:
        if c.id not in seen:
            raw_units.append(({"title": c.title, "kind": "prose", "density": 3, "summary": ""}, [c.id]))
            seen.add(c.id)
    raw_units.sort(key=lambda ru: ru[1][0])

    # Build units, splitting any that exceed the size cap.
    units = []
    origin_index = {}  # mapper unit position -> list of new idx values (for depends_on remap)
    for pos, (entry, ids) in enumerate(raw_units):
        members = [by_id[i] for i in ids]
        text = "\n\n".join(m.text for m in members)
        title = (entry.get("title") or members[0].title or "Untitled")[:200]
        kind = entry.get("kind") if entry.get("kind") in UNIT_KINDS else "prose"
        try:
            density = max(1, min(5, int(entry.get("density") or 3)))
        except (TypeError, ValueError):
            density = 3
        skipped = bool(entry.get("skip"))
        skip_reason = (entry.get("skip_reason") or None) if skipped else None
        summary = (entry.get("summary") or "")[:600] or None
        pieces = [(title, text, members[0].char_start, members[-1].char_end)]
        if len(text) > max_unit_chars:
            parts = chunk_text(text, max_chars=max_unit_chars)
            pieces = []
            offset = members[0].char_start
            for i, (_t, body) in enumerate(parts):
                pieces.append((f"{title} ({i + 1}/{len(parts)})", body, offset, offset + len(body)))
                offset += len(body) + 2
        new_idxs = []
        for (ptitle, ptext, cs, ce) in pieces:
            unit = Unit(
                idx=len(units), title=ptitle, text=ptext, char_start=cs, char_end=ce, kind=kind,
                density=density, skipped=skipped, skip_reason=skip_reason, summary=summary,
                candidate_ids=list(ids),
            )
            unit._raw_depends = [d for d in (entry.get("depends_on") or []) if isinstance(d, int)]
            units.append(unit)
            new_idxs.append(unit.idx)
        origin_index[pos] = new_idxs

    # Remap depends_on from mapper positions to final idx values (first piece of each).
    for unit in units:
        deps = []
        for d in getattr(unit, "_raw_depends", []):
            for new in origin_index.get(d, []):
                if new != unit.idx and new not in deps:
                    deps.append(new)
                    break
        unit.depends_on = deps
    return units


def assign_pages(units, page_offsets):
    """page_offsets: list of [page_number, char_start] in ascending char order."""
    if not page_offsets:
        return
    offsets = sorted(((int(c), int(p)) for p, c in page_offsets), key=lambda t: t[0])

    def page_at(char):
        page = offsets[0][1]
        for start, p in offsets:
            if start <= char:
                page = p
            else:
                break
        return page

    for unit in units:
        if unit.char_start is None:
            continue
        unit.page_start = page_at(unit.char_start)
        unit.page_end = page_at(max(unit.char_start, (unit.char_end or unit.char_start) - 1))


def build_document_map(client, text, settings, max_unit_chars=14000, page_offsets=None):
    """Returns (units, meta, chat_result_or_None, messages_or_None)."""
    text = clean_text(text)
    candidates = skeleton(text, max_unit_chars=max_unit_chars)
    meta = {"candidates": len(candidates), "subject": None, "document_summary": None, "mapped_by": "heuristic"}
    if not candidates:
        return [], meta, None, None

    if len(candidates) == 1 or len(text) <= SINGLE_UNIT_MAX_CHARS:
        units = _units_from_candidates_only(candidates)
        if len(units) > 1:
            # Tiny document with a couple of headings: keep them as separate units but
            # don't spend a model call.
            pass
        assign_pages(units, page_offsets)
        return units, meta, None, None

    messages = _mapper_messages(candidates, settings, max_unit_chars)
    result = client.chat("mapper", messages, response_format=MAP_SCHEMA)
    try:
        mapping = extract_json(result.content)
    except Exception as exc:
        logger.warning("Mapper returned unparseable JSON (%s); falling back to skeleton units.", exc)
        units = _units_from_candidates_only(candidates)
        assign_pages(units, page_offsets)
        meta["mapped_by"] = "heuristic-fallback"
        return units, meta, result, messages
    units = _assemble_units(candidates, mapping, max_unit_chars)
    assign_pages(units, page_offsets)
    meta.update(
        {
            "subject": (mapping.get("subject") or "")[:200] or None,
            "document_summary": (mapping.get("document_summary") or "")[:1200] or None,
            "mapped_by": "model",
        }
    )
    return units, meta, result, messages
