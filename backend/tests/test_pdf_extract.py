"""PDF -> Markdown with PyMuPDF: page markers, section headings, running footers removed."""

from __future__ import annotations

import pymupdf
import pytest

from app.vault.store import VaultError, extract_text

FOOTER = "Downloaded from journal.example.org on 19 November 2026"


def _paper() -> bytes:
    doc = pymupdf.open()
    bodies = ["ATM phosphorylates TDP1 at Ser81 after DNA damage, which stabilises TDP1 and promotes "
              "repair in human cells.", "We confirmed the site by mutagenesis and mass spectrometry.",
              "Smith J, et al. Nature (2020)."]
    for n in range(1, 4):
        body = bodies[n - 1]
        page = doc.new_page()
        y = 72
        if n == 1:
            page.insert_text((72, y), "Results", fontsize=16, fontname="helv")
            y += 30
        if n == 3:
            page.insert_text((72, y), "References", fontsize=16, fontname="helv")
            y += 30
        assert page.insert_textbox(pymupdf.Rect(72, y, 520, y + 200), body, fontsize=10, fontname="helv") >= 0
        page.insert_text((72, 800), FOOTER, fontsize=8, fontname="helv")
    return doc.tobytes()


def test_pdf_becomes_markdown_with_pages_and_headings():
    text, kind = extract_text("paper.pdf", _paper())
    assert kind == "paper"
    assert "<!-- page 1 -->" in text and "<!-- page 3 -->" in text
    assert "## Results" in text and "## References" in text
    assert "ATM phosphorylates TDP1 at Ser81" in text
    assert FOOTER not in text  # repeated on every page: page furniture


def test_hyphenated_line_breaks_are_joined():
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(pymupdf.Rect(72, 72, 200, 300), "The phosphoryl-\nation of TDP1 was measured.",
                        fontsize=10, fontname="helv")
    text, _ = extract_text("p.pdf", doc.tobytes())
    assert "phosphorylation of TDP1" in text


def test_a_pdf_without_text_is_refused():
    doc = pymupdf.open()
    doc.new_page()
    with pytest.raises(VaultError, match="No text could be extracted"):
        extract_text("blank.pdf", doc.tobytes())


def test_a_file_that_is_not_a_pdf_is_refused():
    with pytest.raises(VaultError, match="can't be opened"):
        extract_text("broken.pdf", b"not a pdf at all")
