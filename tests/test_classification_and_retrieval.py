"""Tests for semantic classification, query intent boosting, and enhanced ingestion."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from unipod_bot.groq_client import detect_query_boost, retrieval_query
from unipod_bot.incoming import (
    IncomingUserDm,
    extract_document_info,
    extract_message_text,
)
from unipod_bot.ingest import (
    classify_message_content,
    detect_categories_tag,
    episode_source_description,
    batched_source_description,
)
from unipod_bot.runtime import Runtime


# --- Category Classification Tests ---

def test_classify_event():
    cats = classify_message_content("Join us for the AI Hackathon this weekend!")
    assert "event" in cats


def test_classify_deadline():
    cats = classify_message_content("Please submit before 11:59 pm on Friday.")
    assert "deadline" in cats


def test_classify_event_and_deadline():
    cats = classify_message_content("Hackathon registration closes tomorrow at midnight!")
    assert "event" in cats
    assert "deadline" in cats


def test_classify_team_seeking():
    cats = classify_message_content("Need a teammate with frontend experience")
    assert "team-seeking" in cats


def test_classify_resource():
    cats = classify_message_content(
        "Check out the guidelines on https://github.com/example/repo",
        urls=("https://github.com/example/repo",),
    )
    assert "resource" in cats


def test_classify_announcement():
    cats = classify_message_content("Official update: the venue has moved to Room B.")
    assert "announcement" in cats


def test_detect_categories_tag():
    tag = detect_categories_tag("Hackathon submission deadline is tomorrow")
    assert "[categories:" in tag
    assert "event" in tag
    assert "deadline" in tag


def test_detect_categories_tag_empty_on_generic():
    tag = detect_categories_tag("Good morning everyone!")
    assert tag == ""


def test_episode_source_description_includes_categories():
    msg = SimpleNamespace(
        chat_jid="120363@g.us",
        sender_jid="254700@s.whatsapp.net",
        message_id="M1",
        text="Hackathon deadline is Monday",
        iso_time="2026-09-23T12:00:00Z",
        urls=(),
        is_group=True,
    )
    desc = episode_source_description(msg)
    assert "categories=" in desc
    assert "event" in desc
    assert "deadline" in desc


def test_batched_source_description_includes_categories():
    m1 = SimpleNamespace(
        chat_jid="120363@g.us",
        sender_jid="254701@s.whatsapp.net",
        message_id="M1",
        text="Hackathon orientation today",
        iso_time="2026-09-23T12:00:00Z",
        urls=(),
    )
    m2 = SimpleNamespace(
        chat_jid="120363@g.us",
        sender_jid="254702@s.whatsapp.net",
        message_id="M2",
        text="Submit by 5pm",
        iso_time="2026-09-23T12:05:00Z",
        urls=(),
    )
    desc = batched_source_description([m1, m2])
    assert "categories=" in desc
    assert "event" in desc
    assert "deadline" in desc


# --- Query Intent Boosting Tests ---

def test_detect_query_boost_deadline():
    boost = detect_query_boost("When is the submission deadline?")
    assert "deadline" in boost
    assert "schedule" in boost


def test_detect_query_boost_team():
    boost = detect_query_boost("Who is looking for teammates?")
    assert "team" in boost
    assert "collaboration" in boost


def test_detect_query_boost_document():
    boost = detect_query_boost("Where can I find the pdf guidelines?")
    assert "document" in boost
    assert "pdf" in boost


def test_retrieval_query_appends_boost():
    q = retrieval_query("When does registration close?", "")
    assert "deadline" in q
    assert "close" in q.lower()


# --- Protobuf documentWithCaptionMessage Detection Tests ---

def test_extract_document_info_with_caption_message():
    doc_inner = SimpleNamespace(fileName="rules.pdf", mimetype="application/pdf", caption="Rules of hackathon")
    sub_msg = SimpleNamespace(documentMessage=doc_inner)
    caption_wrapper = SimpleNamespace(message=sub_msg)
    payload = SimpleNamespace(documentWithCaptionMessage=caption_wrapper)

    has_doc, filename, mimetype = extract_document_info(payload)
    assert has_doc is True
    assert filename == "rules.pdf"
    assert mimetype == "application/pdf"

    text = extract_message_text(payload)
    assert text == "Rules of hackathon"


# --- DM PDF Request Tests ---

from pathlib import Path
from unipod_bot.config import Settings


@pytest.mark.anyio
async def test_handle_user_dm_serves_pdf():
    settings = Settings(
        whatsapp_session_path=Path("data/session.db"),
        test_group_jid="120363test@g.us",
        debug=False,
        log_level=20,
        falkordb_host="localhost",
        falkordb_port=6379,
        falkordb_database="default_db",
        falkordb_username=None,
        falkordb_password=None,
        graphiti_group_id="top100_afl",
        admin_phones=("254700000000",),
        groq_api_keys=(),
        graphiti_wanted=False,
    )
    client = MagicMock()
    client.send_document = AsyncMock()

    runtime = Runtime(settings, client)
    # Seed PDF store
    runtime._pdf_store["PDF_MSG_1"] = (b"%PDF-fake", "hackathon_guide.pdf")

    dm = IncomingUserDm(
        chat_jid="254711111111@s.whatsapp.net",
        sender_jid="254711111111@s.whatsapp.net",
        message_id="DM_1",
        text="Can you send me the pdf please?",
        iso_time="2026-09-23T12:00:00Z",
        chat_obj=SimpleNamespace(User="254711111111", Server="s.whatsapp.net"),
    )

    await runtime._handle_user_dm(dm)
    client.send_document.assert_awaited_once()
    args, kwargs = client.send_document.call_args
    assert kwargs.get("filename") == "hackathon_guide.pdf"
    assert kwargs.get("mimetype") == "application/pdf"
