"""Tests for sender display formatting and admin notification content."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from unipod_bot.incoming import IncomingGroupMessage, IncomingUserDm
from unipod_bot.notify import (
    format_sender_display,
    format_unknown_dm,
    format_unknown_group,
    format_unverified_info,
)


# ---------------------------------------------------------------------------
# format_sender_display
# ---------------------------------------------------------------------------


def test_format_sender_display_with_phone_and_name():
    result = format_sender_display("254142340147@s.whatsapp.net", "Ayuen")
    assert result == "@254142340147 (Ayuen)"


def test_format_sender_display_with_phone_only():
    result = format_sender_display("254102838378@s.whatsapp.net", "")
    assert result == "@254102838378"


def test_format_sender_display_no_push_name_kwarg():
    result = format_sender_display("254102838378@s.whatsapp.net")
    assert result == "@254102838378"


def test_format_sender_display_fallback_raw_jid():
    """When no phone digits can be extracted, return the raw JID."""
    result = format_sender_display("unknownformat")
    assert result == "unknownformat"


def test_format_sender_display_name_without_phone():
    """Name present but no extractable digits — show name + raw JID."""
    result = format_sender_display("nodigitshere", "Alice")
    assert result == "Alice (nodigitshere)"


def test_format_sender_display_lid_still_extracts_digits():
    """LID-format JIDs have digits — phone_from_jid strips all digits including the device suffix."""
    result = format_sender_display("123456789012345:0@lid", "Bob")
    # phone_from_jid extracts ALL digits from the local part: '123456789012345:0' -> '1234567890123450'
    assert result == "@1234567890123450 (Bob)"


# ---------------------------------------------------------------------------
# format_unknown_group
# ---------------------------------------------------------------------------


def _group_msg(sender_jid: str = "254142340147@s.whatsapp.net", push_name: str = "Ayuen") -> IncomingGroupMessage:
    return IncomingGroupMessage(
        chat_jid="120363430416381566@g.us",
        sender_jid=sender_jid,
        message_id="MSG001",
        text="When is the hackathon?",
        is_reply_group=True,
        iso_time="2026-09-23T10:00:00+00:00",
        push_name=push_name,
    )


def test_format_unknown_group_sender_formatted():
    msg = _group_msg()
    body = format_unknown_group(msg, "mention")
    assert "@254142340147 (Ayuen)" in body
    # raw JID should NOT appear as the sender value
    assert "254142340147@s.whatsapp.net" not in body


def test_format_unknown_group_sender_no_name():
    msg = _group_msg(push_name="")
    body = format_unknown_group(msg, "question")
    assert "@254142340147" in body
    assert "()" not in body  # no empty parens


def test_format_unknown_group_contains_required_fields():
    msg = _group_msg()
    body = format_unknown_group(msg, "mention")
    assert "120363430416381566@g.us" in body
    assert "MSG001" in body
    assert "When is the hackathon?" in body
    assert "mention" in body


# ---------------------------------------------------------------------------
# format_unknown_dm
# ---------------------------------------------------------------------------


def _dm_msg(sender_jid: str = "254142340147@s.whatsapp.net", push_name: str = "Ayuen") -> IncomingUserDm:
    return IncomingUserDm(
        chat_jid="254142340147@s.whatsapp.net",
        sender_jid=sender_jid,
        message_id="DM001",
        text="What programs are running?",
        iso_time="2026-09-23T10:00:00+00:00",
        push_name=push_name,
    )


def test_format_unknown_dm_sender_formatted():
    msg = _dm_msg()
    body = format_unknown_dm(msg)
    assert "@254142340147 (Ayuen)" in body
    assert "254142340147@s.whatsapp.net" not in body.split("*Sender:*")[1].split("\n")[0]


def test_format_unknown_dm_sender_no_name():
    msg = _dm_msg(push_name="")
    body = format_unknown_dm(msg)
    assert "@254142340147" in body
    assert "()" not in body


def test_format_unknown_dm_contains_required_fields():
    msg = _dm_msg()
    body = format_unknown_dm(msg)
    assert "DM001" in body
    assert "What programs are running?" in body
    assert "Direct message" in body


# ---------------------------------------------------------------------------
# format_unverified_info
# ---------------------------------------------------------------------------


def test_format_unverified_info_group_sender_formatted():
    msg = _group_msg()
    body = format_unverified_info(msg, "Bot reply text", ["Fact A", "Fact B"])
    assert "@254142340147 (Ayuen)" in body
    assert "Fact A" in body
    assert "Fact B" in body
    assert "Bot reply text" in body


def test_format_unverified_info_dm_sender_formatted():
    msg = _dm_msg()
    body = format_unverified_info(msg, "Bot reply text", ["Fact X"])
    assert "@254142340147 (Ayuen)" in body
    assert "Fact X" in body


# ---------------------------------------------------------------------------
# Pushname extraction in classify_message
# ---------------------------------------------------------------------------


def _make_event(push_name: str = "Ayuen", chat_user: str = "120363test", sender: str = "254142340147") -> Any:
    chat = SimpleNamespace(User=chat_user, Server="g.us")
    sender_jid = SimpleNamespace(User=sender, Server="s.whatsapp.net")
    source = SimpleNamespace(
        Chat=chat,
        Sender=sender_jid,
        SenderAlt=None,
        IsFromMe=False,
    )
    info = SimpleNamespace(
        MessageSource=source,
        ID="MSGX",
        Pushname=push_name,
        VerifiedName=None,
        Timestamp=0,
    )
    return SimpleNamespace(Info=info, Message=SimpleNamespace(conversation="Hello?"))


def _test_settings():
    from pathlib import Path
    from unipod_bot.config import Settings

    return Settings(
        whatsapp_session_path=Path("data/session.db"),
        test_group_jid="120363test@g.us",
        test_group_jids=("120363test@g.us",),
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


def test_classify_message_captures_push_name():
    from unipod_bot.incoming import classify_message, IncomingGroupMessage

    settings = _test_settings()
    event = _make_event(push_name="Ayuen")
    result = classify_message(event, settings)
    assert isinstance(result, IncomingGroupMessage)
    assert result.push_name == "Ayuen"


def test_classify_message_empty_push_name():
    from unipod_bot.incoming import classify_message, IncomingGroupMessage

    settings = _test_settings()
    event = _make_event(push_name="")
    result = classify_message(event, settings)
    assert isinstance(result, IncomingGroupMessage)
    assert result.push_name == ""
