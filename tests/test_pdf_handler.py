"""Tests for PDF text extraction, chunking, and translation support."""

from unipod_bot.pdf_handler import (
    _chunk_text,
    _detect_language,
    assemble_translation_prompt,
    format_pdf_mention,
)
from unipod_bot.ingest import pdf_episode_body, pdf_episode_name, pdf_source_description


def test_chunk_text_small():
    """Small text should return a single chunk."""
    chunks = _chunk_text("Short text here", 2000)
    assert len(chunks) == 1
    assert chunks[0] == "Short text here"


def test_chunk_text_large():
    """Large text should be split into multiple chunks."""
    # Create text > 2000 chars
    paragraphs = [f"Paragraph {i}. " * 20 for i in range(10)]
    text = "\n\n".join(paragraphs)
    assert len(text) > 2000
    chunks = _chunk_text(text, 2000)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 2200  # Allow some overflow for paragraph boundary


def test_chunk_text_empty():
    chunks = _chunk_text("", 2000)
    assert len(chunks) == 1


def test_detect_language_english():
    text = "The hackathon will be held on Friday. Registration is open."
    assert _detect_language(text) == "en"


def test_detect_language_french():
    text = (
        "Le hackathon aura lieu vendredi. Les inscriptions sont ouvertes. "
        "Nous vous invitons dans cette salle pour une réunion importante. "
        "Les participants doivent se présenter avec une pièce d'identité."
    )
    assert _detect_language(text) == "fr"


def test_assemble_translation_prompt():
    prompt = assemble_translation_prompt("Hello world", "fr")
    assert "French" in prompt
    assert "Hello world" in prompt
    assert "translate" in prompt.lower()


def test_format_pdf_mention():
    mention = format_pdf_mention("report.pdf", 5, "2348011111111")
    assert "report.pdf" in mention
    assert "5 pages" in mention
    assert "@2348011111111" in mention
    assert "ask" in mention.lower()  # Should tell user to ask for it


def test_pdf_episode_body():
    body = pdf_episode_body("sender@s.whatsapp.net", "doc.pdf", "Some content", 0)
    assert "[PDF: doc.pdf]" in body
    assert "Some content" in body
    assert "sender@s.whatsapp.net" in body


def test_pdf_episode_body_chunk():
    body = pdf_episode_body("sender@s.whatsapp.net", "doc.pdf", "Part 2 content", 1)
    assert "(part 2)" in body


def test_pdf_episode_name():
    assert pdf_episode_name("MSG123") == "pdf-MSG123"
    assert pdf_episode_name("MSG123", 2) == "pdf-MSG123-2"
    assert pdf_episode_name("") == "pdf-unknown"


def test_pdf_source_description():
    desc = pdf_source_description(
        "group@g.us", "sender@s.whatsapp.net", "MSG1", "report.pdf"
    )
    assert "whatsapp_pdf" in desc
    assert "group@g.us" in desc
    assert "report.pdf" in desc
