"""Cloze deletions, read the way Anki reads them.

Anki ends a deletion at the first "}}" after it opens, whatever is inside it. So a
deletion whose answer ends in a LaTeX group, "{{c1::\\frac{a}{b}}}", is cut short: the
answer becomes "\\frac{a}{b" and a stray "}" is left on the card. Everything here works
from what the author meant (braces inside a deletion pair up before it closes) and
`repair_cloze` rewrites the text so that Anki's reading matches it, by putting a space
inside any "}}" that is not the end of a deletion.

`cloze_prompts` gives the cards Anki makes from a note: one per deletion number, with
the other numbers' answers in view.
"""

import re
from dataclasses import dataclass

_OPEN = re.compile(r"\{\{c(\d+)::")
_BRACE_PAIR = re.compile(r"\}(?=\})")

BLANK = "[...]"
# Deletions nest, and the parser follows them by recursion. No real note comes near this
# many; text with more is not read as a cloze at all, so that pasted junk cannot run deep.
MAX_DELETIONS = 60
MAX_NESTING = 10  # Anki ignores a deletion opened inside ten others


@dataclass
class Deletion:
    number: str  # as written, so that text which needs no repair is given back unchanged
    parts: list  # str | Deletion

    @property
    def num(self):
        return int(self.number)

    def _split(self):
        """(parts of the answer, hint). As in Anki, the hint is what follows "::" in a run of
        the deletion's own text, and it ends where that run does."""
        answer, hint = [], ""
        for part in self.parts:
            if isinstance(part, str) and "::" in part:
                part, hint = part.split("::", 1)
            answer.append(part)
        return answer, hint

    @property
    def answer_parts(self):
        return self._split()[0]

    @property
    def answer(self):
        return _plain(self.answer_parts)

    @property
    def hint(self):
        return self._split()[1].strip()


def _plain(parts):
    return "".join(p if isinstance(p, str) else p.answer for p in parts)


def _depth(parts):
    return max((1 + _depth(p.parts) for p in parts if isinstance(p, Deletion)), default=0)


@dataclass
class Cloze:
    parts: list  # str | Deletion
    unclosed: int  # "{{c1::" markers that nothing closes; Anki shows them as plain text

    @property
    def deletions(self):
        """Every deletion, nested ones included, in the order they open."""
        found = []

        def walk(parts):
            for part in parts:
                if isinstance(part, Deletion):
                    found.append(part)
                    walk(part.parts)

        walk(self.parts)
        return found

    @property
    def numbers(self):
        return sorted({d.num for d in self.deletions})


class _Parser:
    def __init__(self, text):
        self.text = text
        self.memo = {}

    def deletion(self, start):
        """(Deletion, index after its close) for the marker at `start`, or None if it never closes."""
        if start not in self.memo:
            match = _OPEN.match(self.text, start)
            # First as the author meant it. If the braces inside never pair up, the text is
            # not LaTeX and Anki's own rule (the first "}}" closes) is what was meant.
            found = self._body(match.end(), True) or self._body(match.end(), False)
            self.memo[start] = (Deletion(match.group(1), found[0]), found[1]) if found else None
        return self.memo[start]

    def _body(self, i, paired):
        text, parts, buf, depth = self.text, [], [], 0
        while i < len(text):
            ch = text[i]
            if ch == "{":
                match = _OPEN.match(text, i)
                if match:
                    child = self.deletion(i)
                    if child:
                        if buf:
                            parts.append("".join(buf))
                            buf = []
                        parts.append(child[0])
                        i = child[1]
                    else:
                        buf.append(match.group(0))
                        i = match.end()
                    continue
                if paired:
                    depth += 1
            elif ch == "\\" and paired and i + 1 < len(text):
                # An escaped character, "\{" and "\}" among them, opens and closes nothing.
                buf.append(text[i:i + 2])
                i += 2
                continue
            elif ch == "}":
                if paired and depth:
                    depth -= 1
                elif text.startswith("}}", i):
                    if buf:
                        parts.append("".join(buf))
                    return parts, i + 2
            buf.append(ch)
            i += 1
        return None


def _too_many(text):
    return len(_OPEN.findall(text or "")) > MAX_DELETIONS


def parse_cloze(text):
    text = text or ""
    if _too_many(text):
        return Cloze([text], 0)
    parser = _Parser(text)
    parts, pos, unclosed = [], 0, 0
    while True:
        match = _OPEN.search(text, pos)
        if not match:
            break
        found = parser.deletion(match.start())
        if found:
            parts.extend([text[pos:match.start()], found[0]])
            pos = found[1]
        else:
            parts.append(text[pos:match.end()])
            unclosed += 1
            pos = match.end()
    parts.append(text[pos:])
    return Cloze([p for p in parts if p != ""], unclosed)


def _write(parts, numbers, inside=False):
    out = ""
    for part in parts:
        if isinstance(part, Deletion):
            body = _write(part.parts, numbers, True)
            if body.endswith("}") and isinstance(part.parts[-1], str):
                body += " "
            out += "{{c%s::%s}}" % (numbers.get(part.num, part.number), body)
        elif inside:
            piece = _BRACE_PAIR.sub("} ", part)
            if piece.startswith("}") and out.endswith("}"):
                out += " "
            out += piece
        else:
            out += part
    return out


def repair_cloze(text, renumber=True):
    """`text` with its deletions written so Anki reads them as meant.

    Puts a space inside every "}}" that would end a deletion early. With `renumber`, the
    deletion numbers are also made to run 1, 2, ... in their existing order, since Anki
    makes no card from {{c0::...}}. Text that needs neither comes back unchanged.
    """
    if not text:
        return text or ""
    cloze = parse_cloze(text)
    numbers = {old: str(new) for new, old in enumerate(cloze.numbers, start=1) if old != new} if renumber else {}
    return _write(cloze.parts, numbers)


def cloze_problem(text):
    """Why Anki would not make sound cards from `text`, as a sentence for the student; None if it would."""
    if _too_many(text):
        return f"One card can hold at most {MAX_DELETIONS} deletions."
    cloze = parse_cloze(text)
    if cloze.unclosed:
        return "A deletion is opened but never closed. End it with }}."
    deletions = cloze.deletions
    if not deletions:
        return "No deletion yet. Wrap the answer like {{c1::answer}}."
    if any(d.num < 1 for d in deletions):
        return "Deletion numbers start at 1: use {{c1::...}}."
    if any(not d.answer.strip() for d in deletions):
        return "A deletion has no answer inside it."
    if _depth(cloze.parts) > MAX_NESTING:
        return f"Anki reads deletions nested at most {MAX_NESTING} deep."
    if repair_cloze(text, renumber=False) != text:
        return "Anki would end a deletion early at a }} inside it. Put a space between those two braces."
    return None


def is_valid_cloze(text):
    return cloze_problem(text) is None


def _show(parts, number, hint_label, segments, answers):
    for part in parts:
        if isinstance(part, str):
            segments.append(("text", part))
        elif number is None or part.num == number:
            hint = part.hint
            segments.append(("blank", hint_label.format(hint) if hint and hint_label else BLANK))
            answers.append(part.answer.strip())
        else:
            _show(part.answer_parts, number, hint_label, segments, answers)


def cloze_prompts(text, hint_label="[{}]"):
    """The cards Anki makes from a cloze note, one per deletion number.

    Each is {"number", "segments", "prompt", "answer"}: the deletions with that number are
    blanked (as their hint, where they have one) and every other deletion shows its
    answer, as on the card. `segments` is the prompt as ("text" | "blank", str) pairs.
    """
    cloze = parse_cloze(text)
    cards = []
    for number in cloze.numbers:
        segments, answers = [], []
        _show(cloze.parts, number, hint_label, segments, answers)
        cards.append({
            "number": number, "segments": segments, "prompt": "".join(s for _kind, s in segments),
            "answer": " / ".join(answers),
        })
    return cards


def cloze_blanked(text):
    """`text` with every deletion blanked at once: the note in one line, not a card Anki shows."""
    segments = []
    _show(parse_cloze(text).parts, None, "", segments, [])
    return "".join(s for _kind, s in segments)


def cloze_answers(text):
    """The answer of every deletion, in the order they open."""
    return [d.answer.strip() for d in parse_cloze(text).deletions]
