from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from unipod_bot.config import Settings, load_settings
from unipod_bot.incoming import (
    IncomingGroupMessage,
    IncomingUserDm,
    classify_message,
)
from unipod_bot.ingest import _detect_team_intent, batched_episode_body, episode_body
from unipod_bot.runtime import Runtime
from unipod_bot.text_sanitizer import sanitize_for_chat, sanitize_for_tts
from unipod_bot.tts import PiperTTSClient, TTSError, make_tts_client


# Helper to build mock WhatsApp event
def _make_event(
    text: str = "",
    chat_user: str = "120363430416381566",
    server: str = "g.us",
    sender: str = "254711111111",
    mid: str = "MSG123",
) -> SimpleNamespace:
    chat = SimpleNamespace(User=chat_user, Server=server)
    sender_jid = SimpleNamespace(User=sender, Server="s.whatsapp.net")
    source = SimpleNamespace(
        Chat=chat,
        Sender=sender_jid,
        SenderAlt=None,
        RecipientAlt=None,
        IsFromMe=False,
    )
    info = SimpleNamespace(MessageSource=source, ID=mid, Timestamp=1700000000)
    msg_payload = SimpleNamespace(conversation=text) if text else None
    return SimpleNamespace(Info=info, Message=msg_payload)


# =============================================================================
# Feature 5: Output Sanitization Tests
# =============================================================================

def test_sanitize_for_chat_strips_markdown():
    text = (
        "## Important Update\n\n"
        "Here is **bold** text and *italic* text and `code`.\n"
        "> Quote block\n\n"
        "- Bullet item 1\n"
        "- Bullet item 2\n"
        "1. Numbered item\n"
        "---\n"
        "Contact @254102838378 or visit https://example.com 🚀!"
    )
    sanitized = sanitize_for_chat(text)
    assert "**" not in sanitized
    assert "##" not in sanitized
    assert ">" not in sanitized
    assert "---" not in sanitized
    assert "- Bullet" not in sanitized
    assert "• Bullet item 1" in sanitized
    assert "1. Numbered item" in sanitized
    assert "bold text and italic text and code." in sanitized
    assert "Contact @254102838378" in sanitized
    assert "https://example.com" in sanitized
    assert "🚀" in sanitized


def test_sanitize_for_chat_strips_tables():
    text = (
        "| Event | Date |\n"
        "| --- | --- |\n"
        "| Hackathon | Oct 15 |\n"
    )
    sanitized = sanitize_for_chat(text)
    assert "---" not in sanitized
    assert "Hackathon" in sanitized


def test_sanitize_for_tts_normalizes_for_speech():
    text = (
        "Hello team! 🎯 The hackathon is on Oct 15.\n"
        "Check https://unipod.org for info (official website).\n"
        "Contact @254102838378 if you have questions"
    )
    tts_text = sanitize_for_tts(text)
    # Emojis removed
    assert "🎯" not in tts_text
    # URL replaced with spoken reference
    assert "https://" not in tts_text
    assert "a shared link" in tts_text
    # @mention converted to user <phone>
    assert "@254102838378" not in tts_text
    assert "user 254102838378" in tts_text
    # Parenthetical aside removed
    assert "(official website)" not in tts_text
    # Punctuation at end
    assert tts_text.endswith(".")


# =============================================================================
# Feature 1: Team-Finding Intent Tests
# =============================================================================

def test_detect_team_intent():
    assert _detect_team_intent("Hey all, I'm looking for a team for the hackathon") == " [team-seeking]"
    assert _detect_team_intent("Anyone want to join my team?") == " [team-seeking]"
    assert _detect_team_intent("Seeking teammates who know React") == " [team-seeking]"
    assert _detect_team_intent("Looking for teammates for the upcoming hackathon") == " [team-seeking]"
    assert _detect_team_intent("Open for collaboration on AI projects") == " [team-seeking]"
    assert _detect_team_intent("What time does the event start?") == ""
    assert _detect_team_intent("") == ""


def test_episode_body_team_intent_annotation():
    msg = MagicMock(
        sender_jid="254700000000@s.whatsapp.net",
        text="Looking for a team to build an IoT prototype",
        urls=(),
    )
    body = episode_body(msg)
    assert "[team-seeking]" in body
    assert "254700000000@s.whatsapp.net: Looking for a team" in body


def test_batched_episode_body_team_intent():
    msg1 = MagicMock(
        sender_jid="user1@s.whatsapp.net",
        text="Looking for teammates for the upcoming hackathon",
        urls=(),
    )
    msg2 = MagicMock(
        sender_jid="user2@s.whatsapp.net",
        text="The venue is ready.",
        urls=(),
    )
    body = batched_episode_body([msg1, msg2])
    lines = body.splitlines()
    assert len(lines) == 2
    assert "[team-seeking]" in lines[0]
    assert "[team-seeking]" not in lines[1]


# =============================================================================
# Feature 2: Reply-To Quoting Tests
# =============================================================================

def test_classify_message_attaches_event_obj(monkeypatch):
    monkeypatch.setenv("TEST_GROUP_JID", "120363430416381566@g.us")
    settings = load_settings(dotenv=False)
    fake_event = _make_event(text="Is the workshop tomorrow?")

    classified = classify_message(fake_event, settings)
    assert isinstance(classified, IncomingGroupMessage)
    assert classified.event_obj is fake_event
    assert classified.is_voice_request is False


def test_send_or_reply_uses_reply_message():
    settings = load_settings(dotenv=False)
    mock_client = MagicMock()
    mock_client.reply_message = AsyncMock()
    mock_client.send_message = AsyncMock()

    runtime = Runtime(settings, mock_client)
    fake_event = MagicMock()

    async def run():
        await runtime._send_or_reply("chat123", "Here is your answer", event_obj=fake_event)

    asyncio.run(run())
    mock_client.reply_message.assert_awaited_once_with("Here is your answer", fake_event, to="chat123")
    mock_client.send_message.assert_not_called()


def test_send_or_reply_falls_back_when_no_event_obj():
    settings = load_settings(dotenv=False)
    mock_client = MagicMock()
    mock_client.reply_message = AsyncMock()
    mock_client.send_message = AsyncMock()

    runtime = Runtime(settings, mock_client)

    async def run():
        await runtime._send_or_reply("chat123", "Plain send", event_obj=None)

    asyncio.run(run())
    mock_client.reply_message.assert_not_called()
    mock_client.send_message.assert_awaited_once_with("chat123", "Plain send")


# =============================================================================
# Feature 3: Better Memory Configuration Tests
# =============================================================================

def test_settings_retrieval_num_results(monkeypatch):
    monkeypatch.setenv("RETRIEVAL_NUM_RESULTS", "25")
    s = load_settings(dotenv=False)
    assert s.retrieval_num_results == 25


def test_settings_tts_configuration(monkeypatch):
    monkeypatch.setenv("TTS_ENABLED", "true")
    monkeypatch.setenv("PIPER_TTS_URL", "http://15.240.69.248.nip.io")
    monkeypatch.setenv("PIPER_API_KEY", "test_key_123")
    s = load_settings(dotenv=False)
    assert s.tts_configured is True
    assert s.piper_tts_url == "http://15.240.69.248.nip.io"
    assert s.piper_tts_api_key == "test_key_123"


# =============================================================================
# Feature 4 & 6: Voice Notes (/voice command) Tests
# =============================================================================

def test_classify_message_detects_voice_prefix(monkeypatch):
    monkeypatch.setenv("TEST_GROUP_JID", "120363430416381566@g.us")
    settings = load_settings(dotenv=False)
    fake_event = _make_event(text="/voice When is the hackathon pitch day?")

    classified = classify_message(fake_event, settings)
    assert isinstance(classified, IncomingGroupMessage)
    assert classified.is_voice_request is True
    assert classified.text == "When is the hackathon pitch day?"


def test_send_voice_or_fallback_success():
    settings = load_settings(dotenv=False)
    mock_client = MagicMock()
    mock_client.send_audio = AsyncMock()
    mock_client.send_message = AsyncMock()

    runtime = Runtime(settings, mock_client)
    mock_tts = MagicMock()
    mock_tts.synthesize = AsyncMock(return_value=b"RIFF_FAKE_WAV_BYTES")
    runtime.tts_client = mock_tts

    fake_event = MagicMock()

    async def run():
        return await runtime._send_voice_or_fallback("chat123", "Here is your speech response.", fake_event)

    sent_voice = asyncio.run(run())

    assert sent_voice is True
    mock_tts.synthesize.assert_awaited_once()
    mock_client.send_audio.assert_awaited_once_with(
        "chat123",
        b"RIFF_FAKE_WAV_BYTES",
        ptt=True,
        quoted=fake_event,
    )
    mock_client.send_message.assert_not_called()


def test_send_voice_or_fallback_graceful_fallback_on_tts_failure():
    settings = load_settings(dotenv=False)
    mock_client = MagicMock()
    mock_client.send_audio = AsyncMock()
    mock_client.send_message = AsyncMock()

    runtime = Runtime(settings, mock_client)
    mock_tts = MagicMock()
    mock_tts.synthesize = AsyncMock(side_effect=TTSError("TTS server down"))
    runtime.tts_client = mock_tts

    async def run():
        return await runtime._send_voice_or_fallback("chat123", "**Important** fallback reply.", None)

    sent_voice = asyncio.run(run())

    assert sent_voice is False
    # Verified it fell back to sanitized plain text reply
    mock_client.send_message.assert_awaited_once_with("chat123", "Important fallback reply.")


# =============================================================================
# Typing Presence & Quoting Improvements Tests
# =============================================================================

def test_typing_presence_text():
    from neonize.client import ChatPresence, ChatPresenceMedia

    settings = load_settings(dotenv=False)
    mock_client = MagicMock()
    mock_client.send_chat_presence = AsyncMock()

    runtime = Runtime(settings, mock_client)
    fake_chat = MagicMock()

    async def run():
        async with runtime._typing_presence(fake_chat, is_voice=False):
            # Verify composing was sent upon entering context
            mock_client.send_chat_presence.assert_awaited_once_with(
                fake_chat, ChatPresence.CHAT_PRESENCE_COMPOSING, ChatPresenceMedia.CHAT_PRESENCE_MEDIA_TEXT
            )

    asyncio.run(run())
    # Verify paused was sent upon exiting context
    assert mock_client.send_chat_presence.await_count == 2
    mock_client.send_chat_presence.assert_awaited_with(
        fake_chat, ChatPresence.CHAT_PRESENCE_PAUSED, ChatPresenceMedia.CHAT_PRESENCE_MEDIA_TEXT
    )


def test_typing_presence_voice():
    from neonize.client import ChatPresence, ChatPresenceMedia

    settings = load_settings(dotenv=False)
    mock_client = MagicMock()
    mock_client.send_chat_presence = AsyncMock()

    runtime = Runtime(settings, mock_client)
    fake_chat = MagicMock()

    async def run():
        async with runtime._typing_presence(fake_chat, is_voice=True):
            pass

    asyncio.run(run())
    assert mock_client.send_chat_presence.await_count == 2
    mock_client.send_chat_presence.assert_any_await(
        fake_chat, ChatPresence.CHAT_PRESENCE_COMPOSING, ChatPresenceMedia.CHAT_PRESENCE_MEDIA_AUDIO
    )
    mock_client.send_chat_presence.assert_awaited_with(
        fake_chat, ChatPresence.CHAT_PRESENCE_PAUSED, ChatPresenceMedia.CHAT_PRESENCE_MEDIA_AUDIO
    )


def test_typing_presence_exception_handling():
    from neonize.client import ChatPresence, ChatPresenceMedia

    settings = load_settings(dotenv=False)
    mock_client = MagicMock()
    mock_client.send_chat_presence = AsyncMock()

    runtime = Runtime(settings, mock_client)
    fake_chat = MagicMock()

    async def run():
        with pytest.raises(RuntimeError):
            async with runtime._typing_presence(fake_chat, is_voice=False):
                raise RuntimeError("Something failed during generation")

    asyncio.run(run())
    # Should still ensure PAUSED is called in finally block
    assert mock_client.send_chat_presence.await_count == 2
    mock_client.send_chat_presence.assert_awaited_with(
        fake_chat, ChatPresence.CHAT_PRESENCE_PAUSED, ChatPresenceMedia.CHAT_PRESENCE_MEDIA_TEXT
    )


def test_typing_presence_graceful_when_no_send_chat_presence():
    settings = load_settings(dotenv=False)
    mock_client = object()  # lacks send_chat_presence

    runtime = Runtime(settings, mock_client)

    async def run():
        async with runtime._typing_presence("chat123", is_voice=False):
            pass

    # Must complete cleanly without error
    asyncio.run(run())


def test_admin_dm_uses_quoted_reply():
    from unipod_bot.incoming import IncomingAdminDm

    settings = replace(load_settings(dotenv=False), admin_jids=("admin@s.whatsapp.net",))
    mock_client = MagicMock()
    mock_client.reply_message = AsyncMock()
    mock_client.send_message = AsyncMock()
    mock_client.send_chat_presence = AsyncMock()

    runtime = Runtime(settings, mock_client)
    fake_event = MagicMock()
    msg = IncomingAdminDm(
        chat_jid="admin@s.whatsapp.net",
        sender_jid="admin@s.whatsapp.net",
        message_id="MID_ADM_1",
        text="ping",
        chat_obj="chat_obj_admin",
        event_obj=fake_event,
    )

    async def run():
        await runtime._handle_admin_dm(msg)

    asyncio.run(run())
    mock_client.reply_message.assert_awaited_once_with(
        "🏓 *Pong* — Bot is online and listening.", fake_event, to="chat_obj_admin"
    )
    mock_client.send_message.assert_not_called()


def test_pdf_request_quotes_original_message():
    settings = replace(load_settings(dotenv=False), knowledge_library_enabled=False)
    mock_client = MagicMock()
    mock_client.send_document = AsyncMock()

    runtime = Runtime(settings, mock_client)
    runtime._pdf_store["MSG_DOC_1"] = (b"%PDF-1.4 mock", "sample.pdf")

    fake_event = MagicMock()
    msg = IncomingGroupMessage(
        chat_jid="group@g.us",
        sender_jid="user@s.whatsapp.net",
        message_id="MSG_REQ_1",
        text="send me the pdf",
        is_reply_group=True,
        iso_time="2026-09-24T12:00:00Z",
        chat_obj="group_chat_obj",
        event_obj=fake_event,
    )

    async def run():
        await runtime._handle_pdf_request(msg)

    asyncio.run(run())
    mock_client.send_document.assert_awaited_once_with(
        "group_chat_obj",
        b"%PDF-1.4 mock",
        filename="sample.pdf",
        mimetype="application/pdf",
        quoted=fake_event,
    )

