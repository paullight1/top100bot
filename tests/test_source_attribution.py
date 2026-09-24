"""Tests for source attribution with admin vs non-admin verification status."""

from unipod_bot.groq_client import assemble_reply_prompt


def test_prompt_includes_admin_phones():
    """Admin phone numbers should appear in the prompt for source authority."""
    prompt = assemble_reply_prompt(
        facts=["[from @2348011111111] Hackathon is on Friday"],
        history="(none)",
        sender_jid="2348099999999@s.whatsapp.net",
        question="When is the hackathon?",
        admin_phones=("2348011111111", "2348022222222"),
    )
    assert "@2348011111111" in prompt
    assert "@2348022222222" in prompt
    assert "trusted sources" in prompt.lower() or "admin" in prompt.lower()


def test_prompt_includes_feedback_contacts():
    """Feedback contacts should appear in prompt for verification redirect."""
    prompt = assemble_reply_prompt(
        facts=["[from @2348099999999] Event is at 3pm"],
        history="(none)",
        sender_jid="2348088888888@s.whatsapp.net",
        question="What time is the event?",
        feedback_contacts="@254102838378 (Diane), @254142340147 (Ayuen)",
    )
    assert "Diane" in prompt
    assert "Ayuen" in prompt


def test_prompt_includes_cross_group_context():
    """Cross-group context should appear when provided."""
    prompt = assemble_reply_prompt(
        facts=["Some fact"],
        history="(none)",
        sender_jid="sender@s.whatsapp.net",
        question="What happened?",
        cross_group_context="[120363other@g.us] 2348011111111: Registration opens Monday",
    )
    assert "cross-group" in prompt.lower() or "other community" in prompt.lower()
    assert "Registration opens Monday" in prompt


def test_prompt_no_cross_group_when_empty():
    """No cross-group block when context is empty."""
    prompt = assemble_reply_prompt(
        facts=["Some fact"],
        history="(none)",
        sender_jid="sender@s.whatsapp.net",
        question="What happened?",
        cross_group_context="",
    )
    assert "cross-group" not in prompt.lower()


def test_fact_source_tag_format():
    """Facts with [from @phone] should be passed through to the prompt."""
    prompt = assemble_reply_prompt(
        facts=["[from @2348099999999 in group 120363abc@g.us] Event is Friday"],
        history="(none)",
        sender_jid="sender@s.whatsapp.net",
        question="When?",
    )
    assert "[from @2348099999999 in group 120363abc@g.us]" in prompt
    assert "Event is Friday" in prompt


def test_admin_phones_empty_no_block():
    """When no admin phones provided, the admin block should not appear."""
    prompt = assemble_reply_prompt(
        facts=["A fact"],
        history="(none)",
        sender_jid="sender@s.whatsapp.net",
        question="What?",
        admin_phones=(),
    )
    assert "trusted sources" not in prompt.lower()
