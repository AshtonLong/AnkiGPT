"""PDF text extraction keeps the page boundaries: the document map assigns pages to units
from them, and every figure is attached to its unit by page."""

import io
import os

import pytest

from app.models import Deck
from app.services import pdf
from app.services.pipeline import document_map

pymupdf = pytest.importorskip("pymupdf")
pytest.importorskip("pymupdf4llm")

from conftest import register  # noqa: E402

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


def _pdf_bytes(topics):
    doc = pymupdf.open()
    for topic in topics:
        doc.new_page().insert_text((72, 100), topic, fontsize=18)
    data = doc.tobytes()
    doc.close()
    return data


def test_combined_pdfs_keep_their_order_and_number_pages_straight_through(tmp_path):
    paths = []
    for name, topics in (("one.pdf", ["Sets", "Automata"]), ("two.pdf", ["Grammars"])):
        paths.append(tmp_path / name)
        paths[-1].write_bytes(_pdf_bytes(topics))
    out = tmp_path / "combined.pdf"
    assert pdf.combine_pdfs([str(paths[1]), str(paths[0])], str(out)) == [1, 2]
    text, total, offsets = pdf.extract_pdf_text(str(out))
    assert total == 3 and [page for page, _ in offsets] == [1, 2, 3]
    assert text.index("Grammars") < text.index("Sets") < text.index("Automata")


def test_combining_names_the_pdf_that_cannot_be_read(tmp_path):
    good, bad = tmp_path / "good.pdf", tmp_path / "bad.pdf"
    good.write_bytes(_pdf_bytes(["Sets"]))
    bad.write_bytes(b"not a pdf")
    with pytest.raises(pdf.UnreadablePdf) as caught:
        pdf.combine_pdfs([str(good), str(bad)], str(tmp_path / "out.pdf"))
    assert caught.value.index == 1


def _upload(client, files, **form):
    data = {"title": "Lectures", "source_type": "pdf", "card_style": "basic", **form}
    data["pdf_file"] = [(io.BytesIO(content), name) for name, content in files]
    return client.post("/decks/new", data=data, content_type="multipart/form-data")


def test_several_uploaded_pdfs_become_one_source_in_the_order_given(client, app):
    register(client)
    response = _upload(client, [("Lecture 2.pdf", _pdf_bytes(["Grammars"])),
                                ("Lecture 1.pdf", _pdf_bytes(["Sets", "Automata"]))])
    assert response.status_code == 302
    with app.app_context():
        deck = Deck.query.one()
        assert deck.source_text.index("Grammars") < deck.source_text.index("Sets")
        assert [page for page, _ in deck.settings_json["page_offsets"]] == [1, 2, 3]
        assert deck.settings_json["source_files"] == [{"name": "Lecture 2.pdf", "pages": 1},
                                                      {"name": "Lecture 1.pdf", "pages": 2}]
        assert os.listdir(app.config["UPLOAD_FOLDER"]) == []
    page = client.get(response.headers["Location"]).text
    assert "2 PDFs combined" in page and "pages 2–3" in page


def test_a_page_range_counts_across_the_combined_pdfs(client, app):
    register(client)
    _upload(client, [("a.pdf", _pdf_bytes(["Sets", "Automata"])), ("b.pdf", _pdf_bytes(["Grammars"]))],
            page_start="2", page_end="3")
    with app.app_context():
        text = Deck.query.one().source_text
        assert "Automata" in text and "Grammars" in text and "Sets" not in text


def test_one_pdf_still_uploads_on_its_own(client, app):
    register(client)
    assert _upload(client, [("a.pdf", _pdf_bytes(["Sets"]))]).status_code == 302
    with app.app_context():
        settings = Deck.query.one().settings_json
        assert "Sets" in Deck.query.one().source_text and "source_files" not in settings


def test_an_unreadable_pdf_among_several_is_named_and_nothing_is_kept(client, app):
    register(client)
    response = _upload(client, [("good.pdf", _pdf_bytes(["Sets"])), ("broken.pdf", b"not a pdf")])
    assert response.status_code == 200
    assert "broken.pdf" in response.text and "could not be read" in response.text
    with app.app_context():
        assert Deck.query.count() == 0
        assert os.listdir(app.config["UPLOAD_FOLDER"]) == []


def test_too_many_pdfs_are_refused(client, app):
    register(client)
    app.config["MAX_UPLOAD_FILES"] = 2
    response = _upload(client, [(f"{n}.pdf", _pdf_bytes(["Sets"])) for n in range(3)])
    assert "Upload up to 2 PDFs" in response.text
