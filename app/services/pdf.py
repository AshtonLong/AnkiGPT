"""PDF text extraction that keeps page boundaries.

Returns Markdown (via pymupdf4llm when available, cleaned plain text otherwise) plus a
list of [page_number, char_offset] pairs so the document map can tell which pages a
unit spans and figures can be attached to the unit they sit in.
"""

import inspect
import re

from pypdf import PdfReader

from .chunking import clean_text


LIGATURE_MAP = str.maketrans(
    {
        "ﬀ": "ff",
        "ﬁ": "fi",
        "ﬂ": "fl",
        "ﬃ": "ffi",
        "ﬄ": "ffl",
    }
)


def _normalize_pdf_text(text):
    return clean_text((text or "").translate(LIGATURE_MAP))


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


def _chunk_text(raw):
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        for key in ("text", "markdown", "md"):
            value = raw.get(key)
            if isinstance(value, str):
                return value
    return ""


def _pages_with_pymupdf4llm(file_path, start, end):
    """Per-page Markdown strings for pages [start, end), or None if unavailable."""
    try:
        import pymupdf.layout  # noqa: F401  (optional layout analysis)
    except Exception:
        pass
    try:
        import pymupdf4llm
        import pymupdf
    except ImportError:
        return None
    to_markdown = getattr(pymupdf4llm, "to_markdown", None)
    if to_markdown is None:
        return None
    try:
        params = set(inspect.signature(to_markdown).parameters)
    except (TypeError, ValueError):
        params = set()
    page_indexes = list(range(start, end))
    if not page_indexes:
        return []
    try:
        with pymupdf.open(file_path) as doc:
            kwargs = {}
            if "page_chunks" in params:
                kwargs["page_chunks"] = True
            if "pages" in params:
                kwargs["pages"] = page_indexes
            raw = to_markdown(doc, **kwargs)
    except Exception:
        return None
    if isinstance(raw, list):
        pages = [_chunk_text(item) for item in raw]
        if "pages" not in params:
            pages = pages[start:end]
        return [_normalize_pdf_text(p) for p in pages]
    if isinstance(raw, str):
        # No page chunking available: one blob, page offsets unknown beyond the first.
        return [_normalize_pdf_text(raw)]
    return None


def _join_pages(pages, first_page_number):
    """Join per-page texts, recording [page_number, char_offset] for each page."""
    parts = []
    offsets = []
    pos = 0
    for i, page_text in enumerate(pages):
        page_text = (page_text or "").strip()
        if not page_text:
            continue
        if parts:
            pos += 2  # the "\n\n" separator
        offsets.append([first_page_number + i, pos])
        parts.append(page_text)
        pos += len(page_text)
    return "\n\n".join(parts), offsets


def extract_pdf_text(file_path, page_start=None, page_end=None):
    """Returns (markdown_text, total_pages, page_offsets)."""
    with open(file_path, "rb") as fh:
        reader = PdfReader(fh)
        pages = reader.pages
        total = len(pages)
        start = max(0, (page_start or 1) - 1)
        end = min(total, page_end or total)

        md_pages = _pages_with_pymupdf4llm(file_path, start, end)
        if md_pages:
            text, offsets = _join_pages(md_pages, start + 1)
            if text.strip():
                # clean_text may trim leading whitespace; offsets stay valid because
                # each page string was already stripped before joining.
                return clean_text(text), total, offsets

        plain_pages = []
        for page in pages[start:end]:
            plain_pages.append(_format_plain_text_as_markdown(page.extract_text() or ""))

    text, offsets = _join_pages(plain_pages, start + 1)
    return clean_text(text), total, offsets
