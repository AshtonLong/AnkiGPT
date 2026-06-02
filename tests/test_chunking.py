from app.services.chunking import chunk_text, clean_text, hash_text


def test_clean_text_collapses_blank_runs():
    # clean_text caps consecutive blank lines at 2 (i.e. at most 3 newlines in a row).
    text = "a\n\n\n\n\nb"
    cleaned = clean_text(text)
    assert "a" in cleaned and "b" in cleaned
    assert "\n\n\n\n" not in cleaned


def test_hash_text_is_deterministic():
    assert hash_text("hello") == hash_text("hello")
    assert hash_text("hello") != hash_text("world")


def test_chunk_text_respects_max_chars():
    paras = "\n\n".join(["word " * 50 for _ in range(10)])
    chunks = chunk_text(paras, max_chars=400)
    assert len(chunks) > 1
    for _title, body in chunks:
        # Allow a single oversized paragraph, but multi-paragraph chunks stay bounded.
        assert len(body) <= 600


def test_chunk_text_returns_titles():
    text = "# Heading One\n\nSome body content about a topic.\n\nMore content here."
    chunks = chunk_text(text, max_chars=3500)
    assert chunks
    assert chunks[0][0] == "Heading One"


def test_chunk_text_empty():
    assert chunk_text("", max_chars=3500) == []
