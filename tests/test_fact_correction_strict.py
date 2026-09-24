"""Tests for stricter fact-correction behavior."""

import time

from unipod_bot.fact_check import is_no_correction, looks_like_factual_claim
from unipod_bot.groq_client import CORRECTION_PROMPT, assemble_correction_prompt


def test_correction_prompt_requires_absolute_certainty():
    """CORRECTION_PROMPT should instruct the LLM to only correct when absolutely certain."""
    prompt_lower = CORRECTION_PROMPT.lower()
    assert "absolutely certain" in prompt_lower or "absolutely sure" in prompt_lower


def test_correction_prompt_instructs_back_off():
    """CORRECTION_PROMPT should instruct the LLM to back off when users push back."""
    prompt_lower = CORRECTION_PROMPT.lower()
    assert "back" in prompt_lower  # "backs off" / "pushed back"
    assert "no_correction" in prompt_lower


def test_is_no_correction_basic():
    assert is_no_correction("NO_CORRECTION")
    assert is_no_correction("no_correction")
    assert is_no_correction("NO_CORRECTION — nothing to fix")
    assert is_no_correction("")
    assert is_no_correction(None)
    assert not is_no_correction("Just a small note — the event is actually on Friday 📅")


def test_assemble_correction_prompt_includes_facts():
    prompt = assemble_correction_prompt(
        statement="The hackathon is on Monday",
        sender_jid="user@s.whatsapp.net",
        facts=["Hackathon is confirmed for Friday September 26", "Registration closes Thursday"],
        feedback_contacts="@254102838378 (Diane)",
    )
    assert "Friday September 26" in prompt
    assert "Registration closes Thursday" in prompt
    assert "Monday" in prompt
    assert "Diane" in prompt


def test_looks_like_factual_claim_dates():
    assert looks_like_factual_claim("The event is on 25/09/2026")
    assert looks_like_factual_claim("Hackathon starts at 10:00 AM")
    assert looks_like_factual_claim("The registration fee costs $50")


def test_looks_like_factual_claim_rejects_casual():
    assert not looks_like_factual_claim("lol nice")
    assert not looks_like_factual_claim("haha")
    assert not looks_like_factual_claim("yes")
    assert not looks_like_factual_claim("")


def test_correction_prompt_no_confrontation():
    """CORRECTION_PROMPT should explicitly forbid confrontational tone."""
    prompt_lower = CORRECTION_PROMPT.lower()
    assert "confrontational" in prompt_lower
    assert "polite" in prompt_lower
