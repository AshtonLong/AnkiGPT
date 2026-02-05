from pypdf import PdfReader


def extract_pdf_text(file_path, page_start=None, page_end=None):
    reader = PdfReader(file_path)
    pages = reader.pages
    total = len(pages)
    start = max(0, (page_start or 1) - 1)
    end = min(total, page_end or total)
    chunks = []
    for page in pages[start:end]:
        text = page.extract_text() or ""
        chunks.append(text)
    return "\n\n".join(chunks), total
