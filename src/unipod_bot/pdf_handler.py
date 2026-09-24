"""PDF text extraction and French translation support.

Extracts text from PDF bytes using PyMuPDF (fitz). Supports chunking for
large documents and optional French translation for the community's
French-speaking members.

The bot workflow:
1. PDF shared in group → extract text → ingest into Graphiti
2. User asks a question → bot mentions the PDF exists in its answer
3. User requests the PDF → bot sends the original file

Translation creates a parallel French version of the extracted text
that is also ingested into Graphiti so French-speaking users get
search hits in their language.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("unipod_bot")

PDF_CHUNK_SIZE = 2000  # Max chars per Graphiti episode


@dataclass(frozen=True)
class PdfExtraction:
    """Result of PDF text extraction."""

    text: str
    page_count: int
    filename: str
    language: str  # detected or "en" default
    chunks: tuple[str, ...]  # text split into Graphiti-sized pieces


def extract_pdf_text(pdf_bytes: bytes, filename: str = "") -> PdfExtraction | None:
    """Extract text from PDF bytes using PyMuPDF.

    Returns None if extraction fails or the PDF has no readable text.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        logger.error("event=pdf_extract_failed reason=PyMuPDF_not_installed")
        return None

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        logger.exception("event=pdf_open_failed filename=%s", filename)
        return None

    pages: list[str] = []
    for page in doc:
        text = page.get_text("text") or ""
        cleaned = text.strip()
        if cleaned:
            pages.append(cleaned)
    doc.close()

    if not pages:
        logger.info("event=pdf_no_text filename=%s", filename)
        return None

    full_text = "\n\n".join(pages)
    chunks = _chunk_text(full_text, PDF_CHUNK_SIZE)
    language = _detect_language(full_text)

    return PdfExtraction(
        text=full_text,
        page_count=len(pages),
        filename=filename,
        language=language,
        chunks=tuple(chunks),
    )


def _chunk_text(text: str, max_chars: int) -> list[str]:
    """Split text into chunks of approximately max_chars, breaking at paragraph boundaries."""
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for paragraph in text.split("\n\n"):
        para = paragraph.strip()
        if not para:
            continue
        if current_len + len(para) + 2 > max_chars and current:
            chunks.append("\n\n".join(current))
            current = []
            current_len = 0
        current.append(para)
        current_len += len(para) + 2

    if current:
        chunks.append("\n\n".join(current))

    return chunks or [text[:max_chars]]


def _detect_language(text: str) -> str:
    """Simple heuristic to detect French vs English text."""
    french_markers = (
        " le ", " la ", " les ", " des ", " du ", " un ", " une ",
        " est ", " sont ", " dans ", " pour ", " avec ", " sur ",
        " ce ", " cette ", " nous ", " vous ", " ils ", " elles ",
        " et ", " ou ", " mais ", " donc ", " car ",
    )
    sample = (" " + text[:2000].lower() + " ")
    french_count = sum(1 for marker in french_markers if marker in sample)
    # If 5+ French markers found, likely French
    return "fr" if french_count >= 5 else "en"


def translate_to_french(text: str) -> str | None:
    """Translate English text to French using Groq.

    This is a placeholder that will be called from the runtime with the
    actual Groq client. Returns None if translation fails.
    """
    # Translation is done via the Groq client in runtime.py
    # This function signature exists for the translation prompt assembly
    return None


def assemble_translation_prompt(text: str, target_lang: str = "fr") -> str:
    """Assemble a prompt for translating document text."""
    lang_name = "French" if target_lang == "fr" else target_lang
    return (
        f"Translate the following document text to {lang_name}. "
        f"Preserve all formatting, dates, numbers, and proper nouns. "
        f"Do not add commentary — output only the translation.\n\n"
        f"---\n{text}\n---"
    )


def format_pdf_mention(filename: str, page_count: int, sender_jid: str) -> str:
    """Format a brief mention of a PDF for the LLM to use in replies.

    The bot should mention the PDF exists but NOT send it unsolicited.
    """
    return (
        f"📄 A document \"{filename}\" ({page_count} pages) was shared by @{sender_jid}. "
        f"If you'd like the full document, just ask and I'll send it to you."
    )
