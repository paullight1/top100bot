import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from unipod_bot.config import Settings, load_settings
from unipod_bot.incoming import classify_message, SkipReason, IncomingAdminDm
from unipod_bot.runtime import Runtime


def settings(tmp_path, **kwargs):
    value = Settings(whatsapp_session_path=tmp_path / "session.db", test_group_jid="test@g.us",
        debug=False, log_level=20, falkordb_host="localhost", falkordb_port=6379,
        falkordb_database="top100_afl", falkordb_username=None, falkordb_password=None,
        graphiti_group_id="top100_afl", admin_phones=("2348011111111",))
    return replace(value, **kwargs)


def event(chat="knowledge", sender="2348011111111", text="Mentoring has moved to Friday.", pdf=False):
    return NS(Info=NS(ID="source-1", Timestamp=1792231200,
        MessageSource=NS(IsFromMe=False, Chat=NS(User=chat, Server="g.us"),
            Sender=NS(User=sender, Server="s.whatsapp.net"), SenderAlt=None)),
        Message=NS(conversation=text, documentMessage=(NS(fileName="guide.pdf", mimetype="application/pdf") if pdf else None)))


def test_knowledge_feed_drops_nonadmins_even_when_test_group_overlaps(tmp_path):
    config = settings(tmp_path, knowledge_group_jid="knowledge@g.us", test_group_jid="knowledge@g.us")
    assert classify_message(event(sender="2348999999999"), config) == SkipReason.GROUP_NOT_ALLOWED
    job = classify_message(event(), config)
    assert job.__class__.__name__ == "IncomingKnowledgeMessage"
    assert classify_message(event(chat="other"), config) == SkipReason.GROUP_NOT_ALLOWED


def test_feed_group_config_is_exact_and_default_library_enabled(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_GROUP_JID", "*")
    with pytest.raises(ValueError):
        load_settings(dotenv=False)
    monkeypatch.setenv("KNOWLEDGE_GROUP_JID", "")
    assert load_settings(dotenv=False).knowledge_library_enabled


class Client:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat, text, **kw):
        self.sent.append((chat, text))


def test_admin_submission_is_draft_and_feed_does_not_enter_recent_or_graphiti(tmp_path):
    async def run():
        config = settings(tmp_path, knowledge_group_jid="knowledge@g.us")
        client = Client()
        runtime = Runtime(config, client)
        job = classify_message(event(), config)
        await runtime._handle_knowledge_message(job)
        entries = runtime.knowledge.list_entries()
        assert len(entries) == 1 and entries[0].status == "draft"
        assert entries[0].audience == "admin_only"
        assert runtime.recent.format_chat("knowledge@g.us") == "(none)"
        assert runtime.ingest_queue.empty()
        assert client.sent and "knowledge@g.us" not in str(client.sent[0][0])
        assert runtime.knowledge.search("mentoring") == []
        await runtime._handle_knowledge_message(job)
        assert len(runtime.knowledge.list_entries()) == 1
    asyncio.run(run())


def test_admin_commands_preview_publish_and_retire(tmp_path):
    async def run():
        runtime = Runtime(settings(tmp_path), Client())
        def msg(text, sender="2348011111111@s.whatsapp.net", mid="m1"):
            return IncomingAdminDm(chat_jid=sender, sender_jid=sender, message_id=mid,
                text=text, chat_obj=sender)
        await runtime._handle_admin_dm(msg("knowledge draft Mentoring | audience=members\nMentoring is on Tuesdays."))
        entry = runtime.knowledge.list_entries()[0]
        assert entry.status == "draft" and entry.audience == "members"
        await runtime._handle_admin_dm(msg(f"knowledge publish {entry.id}", sender="2348999999999@s.whatsapp.net"))
        assert runtime.knowledge.get(entry.id).status == "draft"
        await runtime._handle_admin_dm(msg(f"knowledge publish {entry.id}"))
        assert runtime.knowledge.active(entry.id)
        await runtime._handle_admin_dm(msg(f"knowledge retire {entry.id}"))
        assert not runtime.knowledge.active(entry.id)
    asyncio.run(run())


def test_legacy_ingest_creates_private_draft_and_bad_options_are_visible(tmp_path):
    async def run():
        runtime = Runtime(settings(tmp_path), Client())
        for mid, text in (("m1", "ingest Mentoring is on Tuesdays."),
                          ("m2", "knowledge draft Bad | audence=members\nMust not be stored.")):
            await runtime._handle_admin_dm(IncomingAdminDm(chat_jid="2348011111111@s.whatsapp.net",
                sender_jid="2348011111111@s.whatsapp.net", message_id=mid, text=text,
                chat_obj="2348011111111@s.whatsapp.net"))
        entries = runtime.knowledge.list_entries()
        assert len(entries) == 1 and entries[0].audience == "admin_only"
        assert "unknown option" in runtime.client.sent[-1][1].lower()
        assert runtime.ingest_queue.empty()
    asyncio.run(run())


def test_admin_automatic_drafts_work_without_model_credentials(tmp_path):
    async def run():
        runtime = Runtime(settings(tmp_path), Client())
        await runtime._handle_admin_dm(IncomingAdminDm(chat_jid="2348011111111@s.whatsapp.net",
            sender_jid="2348011111111@s.whatsapp.net", message_id="automatic",
            text="The mentoring contact is the programme office.", chat_obj="admin"))
        assert runtime.knowledge.list_entries()[0].status == "draft"
        assert runtime.recent.format_chat("test@g.us") == "(none)"
    asyncio.run(run())


def test_pdf_feed_creates_private_draft_and_rejects_unreadable_attachment(tmp_path):
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Mentoring sessions take place on Tuesdays.")
    payload = doc.tobytes()
    doc.close()

    async def run():
        client = Client()
        async def download(_target):
            return payload
        client.download_any = download
        config = settings(tmp_path, knowledge_group_jid="knowledge@g.us")
        runtime = Runtime(config, client)
        job = classify_message(event(text="", pdf=True), config)
        await runtime._handle_knowledge_message(job)
        entry = runtime.knowledge.list_entries()[0]
        assert entry.status == "draft" and "Tuesdays" in entry.content
        assert runtime.knowledge.document(entry.id, allow_admin=True) is None
        assert not runtime._pdf_store
        async def unreadable(_target):
            return b"%PDF-invalid"
        client.download_any = unreadable
        await runtime._handle_knowledge_message(replace(job, message_id="bad-pdf"))
        assert len(runtime.knowledge.list_entries()) == 1
        assert "readable" in client.sent[-1][1]
    asyncio.run(run())
