"""
tests/test_doc_store.py
Unit tests for the chunking and text-cleanup helpers in agents/doc_store.py.
These do not load the embedding model or touch ChromaDB.
"""

from agents.doc_store import _chunk_text, _clean_pdf_text, _CHUNK_SIZE


def test_empty_text_gives_no_chunks():
    assert _chunk_text("") == []
    assert _chunk_text("   \n\n  ") == []


def test_short_text_is_one_chunk():
    assert _chunk_text("Hello world.") == ["Hello world."]


def test_text_under_chunk_size_is_one_chunk():
    text = "A short document. " * 10
    chunks = _chunk_text(text)
    assert len(chunks) == 1


def test_chunks_never_exceed_size():
    text = " ".join(f"word{i}." for i in range(1000))
    chunks = _chunk_text(text, 200, 40)
    assert len(chunks) > 1
    assert all(len(c) <= 200 for c in chunks)


def test_chunks_do_not_cut_words():
    text = " ".join(f"word{i}." for i in range(400))
    original_words = set(text.split())
    for chunk in _chunk_text(text, 200, 40):
        assert set(chunk.split()) <= original_words


def test_neighbouring_chunks_overlap():
    text = " ".join(f"word{i}." for i in range(400))
    chunks = _chunk_text(text, 200, 40)
    assert chunks[1].split()[0] in chunks[0].split()


def test_no_text_is_lost():
    text = " ".join(f"word{i}." for i in range(400))
    chunks = _chunk_text(text, 200, 40)
    seen = set()
    for chunk in chunks:
        seen.update(chunk.split())
    assert seen == set(text.split())


def test_long_text_without_separators_is_still_split():
    text = "x" * 2000
    chunks = _chunk_text(text, 500, 50)
    assert len(chunks) >= 4
    assert all(len(c) <= 500 for c in chunks)


def test_paragraphs_are_preferred_boundaries():
    para_a = "First paragraph sentence. " * 20
    para_b = "Second paragraph sentence. " * 20
    chunks = _chunk_text(para_a.strip() + "\n\n" + para_b.strip(), 600, 50)
    assert chunks[0].startswith("First")
    assert any(c.startswith("Second") for c in chunks)


def test_default_chunk_size_is_respected():
    text = ("This is a sentence about retrieval. " * 200).strip()
    assert all(len(c) <= _CHUNK_SIZE for c in _chunk_text(text))


def test_pdf_hyphenated_line_break_is_rejoined():
    assert "information" in _clean_pdf_text("infor-\nmation retrieval")


def test_pdf_single_newlines_become_spaces():
    assert _clean_pdf_text("line one\nline two") == "line one line two"


def test_pdf_paragraph_breaks_are_kept():
    out = _clean_pdf_text("Para one.\n\nPara two.")
    assert "\n\n" in out


def test_pdf_page_number_lines_are_removed():
    out = _clean_pdf_text("Intro text\n12\nMore text")
    assert "12" not in out
    assert "Intro text" in out and "More text" in out
