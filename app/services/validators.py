import re

from .cloze import is_valid_cloze, repair_cloze  # noqa: F401  (is_valid_cloze is imported from here)

BLOCK_MATH_PATTERN = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
INLINE_MATH_PATTERN = re.compile(r"(?<!\\)\$(.+?)(?<!\\)\$")
EQUATION_ENV_PATTERN = re.compile(r"\\begin\{(equation\*?|align\*?)\}(.+?)\\end\{\1\}", re.DOTALL)


def normalize_math(text):
    if not text:
        return text
    text = EQUATION_ENV_PATTERN.sub(lambda m: f"\\[{m.group(2).strip()}\\]", text)
    text = BLOCK_MATH_PATTERN.sub(lambda m: f"\\[{m.group(1).strip()}\\]", text)

    def inline_repl(match):
        content = match.group(1).strip()
        return f"\\({content}\\)"

    text = INLINE_MATH_PATTERN.sub(inline_repl, text)
    # Remove leftover block delimiters but keep single "$" (e.g. currency like "$5").
    text = text.replace("$$", "")
    return text


def is_math_valid(text):
    if not text:
        return True
    # A leftover "$...$" pair means unconverted inline math; a lone "$" is currency, OK.
    if text.count("$") >= 2 or "\\begin{" in text or "\\end{" in text:
        return False
    if text.count("\\(") != text.count("\\)"):
        return False
    if text.count("\\[") != text.count("\\]"):
        return False
    return True


def _stem(word):
    """Crude suffix stripping so 'inhibition'/'inhibits' overlap reduces false drops."""
    for suffix in ("tions", "tion", "ing", "ies", "ied", "ers", "er", "ed", "es", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def is_in_scope(card_text, chunk_text):
    if not card_text:
        return True
    card_words = {_stem(w) for w in re.findall(r"[a-zA-Z]{4,}", card_text.lower())}
    if not card_words:
        return True
    chunk_words = {_stem(w) for w in re.findall(r"[a-zA-Z]{4,}", (chunk_text or "").lower())}
    if not chunk_words:
        return True
    overlap = len(card_words & chunk_words) / max(len(card_words), 1)
    long_words = {w for w in card_words if len(w) >= 7}
    # Only drop cards that are clearly off-topic (very low overlap), to avoid
    # silently deleting good paraphrased or symbol-heavy cards.
    if len(long_words) >= 5 and overlap < 0.15:
        return False
    if len(card_words) >= 10 and overlap < 0.06:
        return False
    return True


def normalize_text(text):
    if not text:
        return ""
    return " ".join(text.strip().split())


def normalize_cloze(text):
    """A model-written cloze, cleaned up and with its deletions made safe for Anki."""
    return repair_cloze(normalize_math(normalize_text(text)))
