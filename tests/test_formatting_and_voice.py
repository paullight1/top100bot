"""Tests for message formatting (bullets, numbered lists, hyphens) and /voice mode."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from unipod_bot.groq_client import assemble_reply_prompt
from unipod_bot.text_sanitizer import sanitize_for_chat, sanitize_for_tts


# ---------------------------------------------------------------------------
# Chat formatting tests
# ---------------------------------------------------------------------------


def test_sanitize_for_chat_preserves_numbered_lists():
    text = (
        "Here are the registration steps:\n"
        "1. Create an account on the portal\n"
        "2. Fill in your team details\n"
        "3. Submit your proposal before midnight"
    )
    result = sanitize_for_chat(text)
    assert "1. Create an account" in result
    assert "2. Fill in your team" in result
    assert "3. Submit your proposal" in result
    # Numbering should NOT be replaced with bullet points
    assert "• Create an account" not in result


def test_sanitize_for_chat_normalizes_parenthesis_numbers():
    text = "1) First step\n2) Second step"
    result = sanitize_for_chat(text)
    assert "1. First step" in result
    assert "2. Second step" in result


def test_sanitize_for_chat_normalizes_unordered_bullets():
    text = (
        "- Robotics Workshop\n"
        "* AI Hackathon\n"
        "+ Web3 Meetup"
    )
    result = sanitize_for_chat(text)
    assert "• Robotics Workshop" in result
    assert "• AI Hackathon" in result
    assert "• Web3 Meetup" in result
    assert "- Robotics" not in result
    assert "* AI" not in result


def test_sanitize_for_chat_strips_lone_bullet_on_single_answer():
    text = "• The hackathon starts at 10:00 AM on Friday."
    result = sanitize_for_chat(text)
    assert result == "The hackathon starts at 10:00 AM on Friday."
    assert "•" not in result


def test_sanitize_for_chat_preserves_multiple_bullets():
    text = "• Hackathon 1\n• Hackathon 2"
    result = sanitize_for_chat(text)
    assert "• Hackathon 1" in result
    assert "• Hackathon 2" in result


def test_sanitize_for_chat_preserves_inline_hyphens_and_ranges():
    text = (
        "• AI Hackathon - Oct 15 - Main Lab\n"
        "• Schedule: 9:00 AM - 5:00 PM\n"
        "• Pre-requisites: Python and Docker"
    )
    result = sanitize_for_chat(text)
    assert "AI Hackathon - Oct 15 - Main Lab" in result
    assert "9:00 AM - 5:00 PM" in result
    assert "Pre-requisites: Python and Docker" in result


# ---------------------------------------------------------------------------
# TTS / Voice note sanitization tests
# ---------------------------------------------------------------------------


def test_sanitize_for_tts_converts_numbered_lists_to_spoken_transitions():
    text = (
        "Here are the instructions:\n"
        "1. Open the Top100 AFL portal\n"
        "2. Join a team or create a new one\n"
        "3. Pitch your solution on demo day"
    )
    tts_text = sanitize_for_tts(text)
    assert "First, Open the Top100 AFL portal." in tts_text
    assert "Second, Join a team or create a new one." in tts_text
    assert "Third, Pitch your solution on demo day." in tts_text
    assert "1." not in tts_text
    assert "2." not in tts_text


def test_sanitize_for_tts_converts_time_date_hyphen_ranges_to_to():
    text = "The workshop runs 9:00 AM - 5:00 PM from Monday - Friday."
    tts_text = sanitize_for_tts(text)
    assert "9:00 AM to 5:00 PM" in tts_text
    assert "Monday to Friday" in tts_text
    assert " - " not in tts_text


def test_sanitize_for_tts_converts_separator_hyphens_to_commas():
    text = "• Top100 AFL Workshop - Oct 15 - Main Lab"
    tts_text = sanitize_for_tts(text)
    # Bullet stripped, separator hyphens replaced with comma for natural speech pause
    assert "•" not in tts_text
    assert "Top100 AFL Workshop, Oct 15, Main Lab." in tts_text


def test_sanitize_for_tts_strips_all_bullet_markers():
    text = (
        "• First item\n"
        "- Second item\n"
        "* Third item"
    )
    tts_text = sanitize_for_tts(text)
    assert "•" not in tts_text
    assert "First item." in tts_text
    assert "Second item." in tts_text
    assert "Third item." in tts_text


# ---------------------------------------------------------------------------
# Groq voice mode prompt assembly tests
# ---------------------------------------------------------------------------


def test_assemble_reply_prompt_voice_mode_flag():
    prompt_chat = assemble_reply_prompt(
        facts=["Fact A"],
        history="User: Hello",
        sender_jid="254712345678@s.whatsapp.net",
        question="When is hackathon?",
        is_voice=False,
    )
    assert "[VOICE NOTE MODE (/voice)]" not in prompt_chat

    prompt_voice = assemble_reply_prompt(
        facts=["Fact A"],
        history="User: Hello",
        sender_jid="254712345678@s.whatsapp.net",
        question="When is hackathon?",
        is_voice=True,
    )
    assert "[VOICE NOTE MODE (/voice)]" in prompt_voice
    assert "synthesized into spoken speech" in prompt_voice
    assert "Do NOT use bullet points" in prompt_voice


# ---------------------------------------------------------------------------
# Runtime voice request propagation tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_generate_known_reply_propagates_is_voice():
    from pathlib import Path
    from unipod_bot.config import Settings
    from unipod_bot.runtime import Runtime

    settings = Settings(
        whatsapp_session_path=Path("data/session.db"),
        test_group_jid=None,
        test_group_jids=(),
        debug=False,
        log_level=20,
        falkordb_host="localhost",
        falkordb_port=6379,
        falkordb_database="default_db",
        falkordb_username=None,
        falkordb_password=None,
        graphiti_group_id="top100_afl",
        admin_phones=("254102838378",),
        admin_jids=(),
    )
    mock_client = AsyncMock()
    runtime = Runtime(settings, mock_client)
    runtime.groq = AsyncMock()
    runtime.groq.generate = AsyncMock(return_value="The hackathon is on Friday.")

    # With is_voice=True
    with patch("unipod_bot.runtime.assemble_reply_prompt", wraps=assemble_reply_prompt) as mock_assemble:
        reply, _ = await runtime._generate_known_reply(
            chat_jid="12345@g.us",
            sender_jid="254712345678@s.whatsapp.net",
            message_id="MSG1",
            text="When is hackathon?",
            allow_stub=True,
            is_voice=True,
        )
        assert reply == "The hackathon is on Friday."
        mock_assemble.assert_called_once()
        _, kwargs = mock_assemble.call_args
        assert kwargs.get("is_voice") is True


def test_strip_voice_prefix_matches_aliases():
    from unipod_bot.incoming import _strip_voice_prefix

    text1, is_v1 = _strip_voice_prefix("/voice when is hackathon?")
    assert is_v1 is True
    assert text1 == "when is hackathon?"

    text2, is_v2 = _strip_voice_prefix("/voicemode what are the deadlines?")
    assert is_v2 is True
    assert text2 == "what are the deadlines?"

    text3, is_v3 = _strip_voice_prefix("voicemode give me updates")
    assert is_v3 is True
    assert text3 == "give me updates"

    text4, is_v4 = _strip_voice_prefix("/voice")
    assert is_v4 is True
    assert text4 == ""

    text5, is_v5 = _strip_voice_prefix("I heard a voice in the hallway")
    assert is_v5 is False
    assert text5 == "I heard a voice in the hallway"


@pytest.mark.anyio
async def test_known_reply_empty_voice_sends_guidance():
    from pathlib import Path
    from unipod_bot.config import Settings
    from unipod_bot.runtime import Runtime
    from unipod_bot.incoming import IncomingGroupMessage

    settings = Settings(
        whatsapp_session_path=Path("data/session.db"),
        test_group_jid=None,
        test_group_jids=(),
        debug=False,
        log_level=20,
        falkordb_host="localhost",
        falkordb_port=6379,
        falkordb_database="default_db",
        falkordb_username=None,
        falkordb_password=None,
        graphiti_group_id="top100_afl",
        admin_phones=("254102838378",),
        admin_jids=(),
    )
    mock_client = AsyncMock()
    runtime = Runtime(settings, mock_client)
    runtime._send_voice_or_fallback = AsyncMock()

    msg = IncomingGroupMessage(
        chat_jid="12345@g.us",
        sender_jid="254712345678@s.whatsapp.net",
        message_id="MSG_EMPTY_VOICE",
        text="",
        is_reply_group=True,
        iso_time="2026-09-24T18:00:00Z",
        mentioned_users=(),
        chat_obj="12345@g.us",
        is_voice_request=True,
    )

    await runtime._known_reply(msg, trigger="voice")
    runtime._send_voice_or_fallback.assert_called_once()
    _, call_text, _ = runtime._send_voice_or_fallback.call_args[0]
    assert "Voice Mode Active" in call_text
