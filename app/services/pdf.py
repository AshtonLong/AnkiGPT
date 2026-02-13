import inspect
import re
from pypdf import PdfReader


LIGATURE_MAP = str.maketrans(
    {
        "\ufb00": "ff",
        "\ufb01": "fi",
        "\ufb02": "fl",
        "\ufb03": "ffi",
        "\ufb04": "ffl",
    }
)


def _normalize_pdf_text(text):
    text = (text or "").translate(LIGATURE_MAP)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x0c", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    compacted = []
    blank_run = 0
    for line in lines:
        if not line.strip():
            blank_run += 1
            if blank_run <= 2 and compacted:
                compacted.append("")
            continue
        blank_run = 0
        compacted.append(line)
    return "\n".join(compacted).strip()


def _looks_like_list_item(line):
    return bool(re.match(r"^\s*([-*+]\s+|\d+[.)]\s+)", line))


def _looks_like_heading(line):
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith("#"):
        return True
    if len(stripped) > 100:
        return False
    if stripped.endswith(":"):
        return True
    letters = [c for c in stripped if c.isalpha()]
    return bool(letters) and all(c.isupper() for c in letters)


def _merge_wrapped_lines(text):
    lines = [line.strip() for line in text.split("\n")]
    merged = []
    for line in lines:
        if not line:
            if merged and merged[-1] != "":
                merged.append("")
            continue
        if not merged or merged[-1] == "":
            merged.append(line)
            continue

        prev = merged[-1]
        if _looks_like_list_item(line) or _looks_like_heading(line):
            merged.append(line)
            continue
        if _looks_like_list_item(prev) or _looks_like_heading(prev):
            merged.append(line)
            continue

        if prev.endswith("-") and line and line[0].islower():
            merged[-1] = prev[:-1] + line
            continue

        if prev[-1] in ".!?;:" and line and line[0].isupper():
            merged.append(line)
            continue

        merged[-1] = f"{prev} {line}"

    return "\n".join(merged).strip()


def _format_plain_text_as_markdown(text):
    text = _merge_wrapped_lines(_normalize_pdf_text(text))
    if not text:
        return ""

    md_lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            if md_lines and md_lines[-1] != "":
                md_lines.append("")
            continue
        if _looks_like_list_item(stripped):
            md_lines.append(re.sub(r"^\s*([*+]|-)\s+", "- ", stripped))
            continue
        if _looks_like_heading(stripped) and not stripped.startswith("#"):
            md_lines.append(f"## {stripped.rstrip(':')}")
            md_lines.append("")
            continue
        md_lines.append(stripped)
    return "\n".join(md_lines).strip()


def _result_to_markdown(raw):
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        for key in ("text", "markdown", "md"):
            value = raw.get(key)
            if isinstance(value, str):
                return value
        return ""
    if isinstance(raw, list):
        lines = []
        for item in raw:
            if isinstance(item, str):
                lines.append(item)
            elif isinstance(item, dict):
                for key in ("text", "markdown", "md"):
                    value = item.get(key)
                    if isinstance(value, str) and value.strip():
                        lines.append(value)
                        break
        return "\n\n".join(lines)
    return ""


def _extract_markdown_with_pymupdf4llm(file_path, start, end, total_pages):
    try:
        import pymupdf4llm
    except ImportError:
        return ""
    try:
        import pymupdf
    except ImportError:
        pymupdf = None

    to_markdown = getattr(pymupdf4llm, "to_markdown", None)
    if to_markdown is None:
        return ""

    page_indexes = list(range(start, end))
    if not page_indexes:
        return ""

    try:
        params = set(inspect.signature(to_markdown).parameters)
    except (TypeError, ValueError):
        params = set()

    def run_to_markdown(**kwargs):
        if pymupdf is None:
            return to_markdown(file_path, **kwargs)
        with pymupdf.open(file_path) as doc:
            return to_markdown(doc, **kwargs)

    try:
        if "pages" in params:
            return _normalize_pdf_text(_result_to_markdown(run_to_markdown(pages=page_indexes)))

        if "page_chunks" in params:
            raw = run_to_markdown(page_chunks=True)
            if isinstance(raw, list):
                selected = raw[start:end]
                return _normalize_pdf_text(_result_to_markdown(selected))

        if start == 0 and end == total_pages:
            return _normalize_pdf_text(_result_to_markdown(run_to_markdown()))
    except Exception:
        return ""

    return ""


def extract_pdf_text(file_path, page_start=None, page_end=None):
    with open(file_path, "rb") as fh:
        reader = PdfReader(fh)
        pages = reader.pages
        total = len(pages)
        start = max(0, (page_start or 1) - 1)
        end = min(total, page_end or total)

        markdown = _extract_markdown_with_pymupdf4llm(file_path, start, end, total)
        if markdown:
            return markdown, total

        chunks = []
        for page in pages[start:end]:
            text = page.extract_text() or ""
            chunks.append(text)

    fallback_text = "\n\n".join(chunks)
    return _format_plain_text_as_markdown(fallback_text), total
