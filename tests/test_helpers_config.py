"""Tests for topic helpers config parsing, formatting, and prompt integration."""
from __future__ import annotations

import pytest

from unipod_bot.config import (
    TopicHelper,
    format_helpers_str,
    parse_helpers_config,
)
from unipod_bot.groq_client import assemble_reply_prompt


# ---------------------------------------------------------------------------
# parse_helpers_config
# ---------------------------------------------------------------------------


def test_parse_helpers_config_basic():
    result = parse_helpers_config("bot:254102838378:Admin,programs:254142340147:Ayuen")
    assert len(result) == 2
    assert result[0] == TopicHelper(topic="bot", phone="254102838378", name="Admin")
    assert result[1] == TopicHelper(topic="programs", phone="254142340147", name="Ayuen")


def test_parse_helpers_config_no_name_falls_back_to_phone():
    result = parse_helpers_config("bot:254102838378")
    assert len(result) == 1
    assert result[0].phone == "254102838378"
    assert result[0].name == "254102838378"


def test_parse_helpers_config_empty_returns_empty():
    assert parse_helpers_config("") == ()
    assert parse_helpers_config(None) == ()


def test_parse_helpers_config_skips_invalid_entries():
    # Missing phone digits
    result = parse_helpers_config("bot:,programs:254142340147:Ayuen")
    assert len(result) == 1
    assert result[0].topic == "programs"


def test_parse_helpers_config_lowercases_topic():
    result = parse_helpers_config("BOT:254102838378:Admin")
    assert result[0].topic == "bot"


def test_parse_helpers_config_semicolon_separator():
    result = parse_helpers_config("bot:254102838378:Admin;programs:254142340147:Ayuen")
    assert len(result) == 2


def test_parse_helpers_config_name_with_colon_preserved():
    """Third segment split: 'admin:254:Foo:Bar' → name='Foo:Bar'."""
    result = parse_helpers_config("admin:254102838378:Foo:Bar")
    assert result[0].name == "Foo:Bar"


def test_parse_helpers_config_strips_whitespace():
    result = parse_helpers_config(" bot : 254102838378 : Admin , programs : 254142340147 : Ayuen ")
    assert len(result) == 2
    assert result[0].phone == "254102838378"
    assert result[0].name == "Admin"


# ---------------------------------------------------------------------------
# format_helpers_str
# ---------------------------------------------------------------------------


def test_format_helpers_str_basic():
    helpers = (
        TopicHelper(topic="bot", phone="254102838378", name="Admin"),
        TopicHelper(topic="programs", phone="254142340147", name="Ayuen"),
    )
    result = format_helpers_str(helpers)
    assert "Bot: @254102838378 (Admin)" in result
    assert "Programs: @254142340147 (Ayuen)" in result


def test_format_helpers_str_empty():
    assert format_helpers_str(()) == ""


def test_format_helpers_str_phone_only_no_parens():
    helpers = (TopicHelper(topic="events", phone="254102838378", name="254102838378"),)
    result = format_helpers_str(helpers)
    # When name == phone, no extra parentheses
    assert "@254102838378" in result
    assert "(@254102838378)" not in result


# ---------------------------------------------------------------------------
# assemble_reply_prompt helpers_text integration
# ---------------------------------------------------------------------------


def test_assemble_reply_prompt_includes_helpers_block():
    prompt = assemble_reply_prompt(
        facts=["Hackathon is Oct 5 [from @254102838378]"],
        history="",
        sender_jid="254999000000@s.whatsapp.net",
        question="When is the hackathon?",
        helpers_text="  Bot: @254102838378 (Admin)\n  Programs: @254142340147 (Ayuen)",
    )
    assert "Domain helpers" in prompt
    assert "@254102838378 (Admin)" in prompt
    assert "@254142340147 (Ayuen)" in prompt


def test_assemble_reply_prompt_no_helpers_block_when_empty():
    prompt = assemble_reply_prompt(
        facts=[],
        history="",
        sender_jid="254999000000@s.whatsapp.net",
        question="Hello",
        helpers_text="",
    )
    assert "Domain helpers" not in prompt


def test_assemble_reply_prompt_helpers_after_feedback_contacts():
    """Helpers block should appear in the prompt (order check)."""
    prompt = assemble_reply_prompt(
        facts=["Some fact"],
        history="",
        sender_jid="254999000000@s.whatsapp.net",
        question="Test question",
        feedback_contacts="@254102838378 (Admin)",
        helpers_text="  Bot: @254102838378 (Admin)",
    )
    fc_pos = prompt.find("Feedback / verification contacts")
    helpers_pos = prompt.find("Domain helpers")
    assert fc_pos != -1
    assert helpers_pos != -1
    # Both are present; helpers comes after feedback contacts
    assert helpers_pos > fc_pos
