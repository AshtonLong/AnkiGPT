import hashlib


def clean_text(text):
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    return "\n".join(lines)


def guess_title(paragraph):
    if len(paragraph) <= 80 and paragraph.isupper():
        return paragraph.title()
    if len(paragraph) <= 80 and paragraph.endswith(":"):
        return paragraph.rstrip(":")
    return None


def chunk_text(text, max_chars=3500):
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
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
            chunk_text = "\n".join(current)
            chunks.append((current_title, chunk_text))
            current = []
            current_len = 0
            current_title = title
            if title:
                continue
        current.append(para)
        current_len += len(para) + 1
    if current:
        chunk_text = "\n".join(current)
        chunks.append((current_title, chunk_text))
    return chunks


def hash_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
