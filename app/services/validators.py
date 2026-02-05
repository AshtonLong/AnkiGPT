import re


CLOZE_PATTERN = re.compile(r"\{\{c\d+::.+?\}\}")
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
    text = text.replace("$$", "")
    text = text.replace("$", "")
    return text


def is_math_valid(text):
    if not text:
        return True
    if "$" in text or "\\begin{" in text or "\\end{" in text:
        return False
    if text.count("\\(") != text.count("\\)"):
        return False
    if text.count("\\[") != text.count("\\]"):
        return False
    return True


def is_in_scope(card_text, chunk_text):
    if not card_text:
        return True
    card_words = set(re.findall(r"[a-zA-Z]{4,}", card_text.lower()))
    if not card_words:
        return True
    chunk_words = set(re.findall(r"[a-zA-Z]{4,}", (chunk_text or "").lower()))
    if not chunk_words:
        return True
    overlap = len(card_words & chunk_words) / max(len(card_words), 1)
    long_words = {w for w in card_words if len(w) >= 7}
    if len(long_words) >= 4 and overlap < 0.2:
        return False
    if len(card_words) >= 8 and overlap < 0.08:
        return False
    return True


def is_valid_cloze(text):
    if not text:
        return False
    if text.count("{{") != text.count("}}"):
        return False
    return bool(CLOZE_PATTERN.search(text))


def normalize_text(text):
    if not text:
        return ""
    return " ".join(text.strip().split())
