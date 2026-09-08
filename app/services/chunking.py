import hashlib
import re


def clean_text(text):
    """Normalize newlines, strip trailing spaces, and cap blank-line runs at two.

    Shared by the PDF extractor and the deck pipeline so both clean text the same way.
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\x0c", "\n")
    cleaned = []
    blank_run = 0
    for line in text.split("\n"):
        line = line.rstrip()
        if not line.strip():
            blank_run += 1
            if blank_run <= 2 and cleaned:
                cleaned.append("")
            continue
        blank_run = 0
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def guess_title(paragraph):
    md_heading = re.match(r"^\s*#{1,6}\s+(.*?)\s*$", paragraph)
    if md_heading:
        return md_heading.group(1).strip()
    if len(paragraph) <= 80 and paragraph.isupper():
        return paragraph.title()
    if len(paragraph) <= 80 and paragraph.endswith(":"):
        return paragraph.rstrip(":")
    return None


def chunk_text(text, max_chars=3500):
    """Split text into (title, body) chunks of at most `max_chars`, breaking on paragraphs.

    A heading paragraph becomes the title of the chunk that follows it.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]
    chunks = []
    current = []
    current_len = 0
    current_title = None
    for para in paragraphs:
        title = guess_title(para)
        if title and not current:
            current_title = title
            continue
        if current_len + len(para) + 1 > max_chars and current:
            chunks.append((current_title, "\n".join(current)))
            current = []
            current_len = 0
            current_title = title
            if title:
                continue
        current.append(para)
        current_len += len(para) + 1
    if current:
        chunks.append((current_title, "\n".join(current)))
    return chunks


def hash_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
