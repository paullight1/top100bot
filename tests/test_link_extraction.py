"""Tests for URL extraction and link handling in messages."""

from types import SimpleNamespace

from unipod_bot.incoming import extract_urls, extract_quoted_context, extract_document_info
from unipod_bot.ingest import episode_body


class _FakeMsg:
    """Minimal EpisodeSource with urls for testing."""
    def __init__(self, text, urls=None):
        self.chat_jid = "group@g.us"
        self.sender_jid = "user@s.whatsapp.net"
        self.message_id = "MSG1"
        self.text = text
        self.iso_time = "2026-09-20T10:00:00+00:00"
        self.urls = urls or ()


def test_extract_urls_basic():
    urls = extract_urls("Check out https://example.com/event for details")
    assert urls == ("https://example.com/event",)


def test_extract_urls_multiple():
    urls = extract_urls("Links: https://a.com and http://b.com/path?q=1")
    assert len(urls) == 2
    assert "https://a.com" in urls
    assert "http://b.com/path?q=1" in urls


def test_extract_urls_deduplicates():
    urls = extract_urls("https://a.com twice https://a.com")
    assert urls == ("https://a.com",)


def test_extract_urls_none():
    assert extract_urls("") == ()
    assert extract_urls("no links here") == ()


def test_extract_urls_from_complex_text():
    text = (
        "Registration form is at https://forms.google.com/d/1abc "
        "and the schedule is at https://docs.google.com/spreadsheet/xyz"
    )
    urls = extract_urls(text)
    assert len(urls) == 2


def test_episode_body_includes_urls():
    msg = _FakeMsg("Check this link", urls=("https://example.com",))
    body = episode_body(msg)
    assert "[links: https://example.com]" in body


def test_episode_body_no_urls():
    msg = _FakeMsg("Just text, no links")
    body = episode_body(msg)
    assert "[links:" not in body


def test_extract_quoted_context_basic():
    """Extract quoted message from extended text context."""
    quoted = SimpleNamespace(conversation="Original question here")
    ctx = SimpleNamespace(quotedMessage=quoted, participant="sender@s.whatsapp.net")
    extended = SimpleNamespace(contextInfo=ctx, text="Reply to this")
    payload = SimpleNamespace(extendedTextMessage=extended)

    text, sender = extract_quoted_context(payload)
    assert text == "Original question here"
    assert sender == "sender@s.whatsapp.net"


def test_extract_quoted_context_none():
    text, sender = extract_quoted_context(None)
    assert text == ""
    assert sender == ""


def test_extract_quoted_context_no_quote():
    extended = SimpleNamespace(contextInfo=None, text="Not a reply")
    payload = SimpleNamespace(extendedTextMessage=extended)
    text, sender = extract_quoted_context(payload)
    assert text == ""


def test_extract_document_info_pdf():
    doc = SimpleNamespace(fileName="report.pdf", mimetype="application/pdf", title="")
    payload = SimpleNamespace(documentMessage=doc)
    has_doc, filename, mime = extract_document_info(payload)
    assert has_doc is True
    assert filename == "report.pdf"
    assert mime == "application/pdf"


def test_extract_document_info_none():
    has_doc, filename, mime = extract_document_info(None)
    assert has_doc is False


def test_extract_document_info_no_doc():
    payload = SimpleNamespace(documentMessage=None)
    has_doc, filename, mime = extract_document_info(payload)
    assert has_doc is False
