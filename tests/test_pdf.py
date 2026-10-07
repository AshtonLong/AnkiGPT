"""PDF text extraction keeps the page boundaries: the document map assigns pages to units
from them, and every figure is attached to its unit by page."""

import pytest

from app.services import pdf
from app.services.pipeline import document_map

pymupdf = pytest.importorskip("pymupdf")
pytest.importorskip("pymupdf4llm")

TOPICS = ["Sets", "Automata", "Grammars", "Regular expressions"]


@pytest.fixture
def lecture(tmp_path):
    """One topic per page, with a blank page in the middle."""
    path = tmp_path / "lecture.pdf"
    doc = pymupdf.open()
    for number, topic in enumerate(TOPICS, start=1):
        page = doc.new_page()
        if number != 3:
            page.insert_text((72, 100), topic, fontsize=18)
            page.insert_text((72, 140), f"Body text about {topic.lower()} on page {number}.", fontsize=11)
    doc.save(path)
    doc.close()
    return str(path)


def test_every_page_gets_its_own_offset(lecture):
    text, total, offsets = pdf.extract_pdf_text(lecture)
    assert total == 4
    # The blank page has no text and so no offset; the pages around it keep their numbers.
    assert [page for page, _ in offsets] == [1, 2, 4]
    for page, start in offsets:
        assert text[start:].lstrip("# ").startswith(TOPICS[page - 1])


def test_units_and_figures_land_on_the_page_they_came_from(lecture):
    text, _total, offsets = pdf.extract_pdf_text(lecture)
    units = document_map._units_from_candidates_only(document_map.skeleton(text))
    document_map.assign_pages(units, offsets)
    assert [(u.title, u.page_start) for u in units] == [("Sets", 1), ("Automata", 2), ("Regular expressions", 4)]


def test_a_page_range_limits_the_text_too(lecture):
    text, total, offsets = pdf.extract_pdf_text(lecture, 2, 2)
    assert total == 4 and offsets == [[2, 0]]
    assert "Automata" in text and "Sets" not in text and "Regular expressions" not in text


def test_extraction_without_per_page_text_falls_back_to_the_plain_reader(lecture, monkeypatch):
    # A blob for the whole document has no page boundaries to offer; the plain reader does.
    monkeypatch.setattr("pymupdf4llm.to_markdown", lambda doc, **kwargs: "one blob for every page")
    assert pdf._pages_with_pymupdf4llm(lecture, 0, 4) is None
    text, _total, offsets = pdf.extract_pdf_text(lecture)
    assert [page for page, _ in offsets] == [1, 2, 4] and "one blob" not in text
