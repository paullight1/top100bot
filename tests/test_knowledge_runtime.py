import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from unipod_bot.config import Settings, load_settings
from unipod_bot.incoming import classify_message, SkipReason, IncomingAdminDm, IncomingUserDm
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
        self.documents = []

    async def send_message(self, chat, text, **kw):
        self.sent.append((chat, text))

    async def send_document(self, chat, data, **kw):
        self.documents.append((chat, data, kw))


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


def test_pdf_downloads_never_expose_private_or_retired_sources(tmp_path):
    async def run():
        runtime = Runtime(settings(tmp_path), Client())
        lib = runtime.knowledge
        for mid, audience in (("private", "admin_only"), ("public", "members")):
            entry = lib.create_draft(title="Mentoring guide", content="Mentoring guidance.",
                actor="admin", source_chat="admin", source_message=mid, audience=audience,
                attachment=b"%PDF-" + mid.encode(), filename=mid + ".pdf")
            lib.publish(entry.id, actor="admin")
            if mid == "private":
                private_id = entry.id
            else:
                public_id = entry.id
        member = IncomingUserDm(chat_jid="user@s.whatsapp.net", sender_jid="user@s.whatsapp.net",
            message_id="request", text=f"document {private_id}", iso_time="", chat_obj="user")
        await runtime._handle_pdf_request(member)
        assert not runtime.client.documents
        assert private_id not in runtime.client.sent[-1][1]
        await runtime._handle_pdf_request(replace(member, text=f"document {public_id}"))
        assert runtime.client.documents[0][1] == b"%PDF-public"
        lib.retire(public_id, actor="admin")
        await runtime._handle_pdf_request(replace(member, text=f"document {public_id}"))
        assert len(runtime.client.documents) == 1
        admin = IncomingAdminDm(chat_jid="admin", sender_jid="2348011111111@s.whatsapp.net",
            message_id="admin-request", text=f"document {private_id}", chat_obj="admin")
        await runtime._handle_admin_dm(admin)
        assert runtime.client.documents[-1][1] == b"%PDF-private"
    asyncio.run(run())


def test_published_library_answer_ignores_unapproved_graph_and_recent_claims(tmp_path):
    import json
    async def run():
        runtime = Runtime(settings(tmp_path), Client())
        entry = runtime.knowledge.create_draft(title="Mentoring", content="Mentoring is on Tuesdays.",
            actor="admin", source_chat="admin", source_message="approved", audience="members")
        runtime.knowledge.publish(entry.id, actor="admin")
        runtime.recent.add(iso_time="", group_jid="test@g.us", sender_jid="member", text="Mentoring is on Friday.")
        class Model:
            async def chat_completion(self, messages, **kw):
                payload = json.loads(messages[-1]["content"])
                source = payload["sources"][0]
                assert "Friday" not in source["text"]
                return json.dumps({"answers_question": True, "conflict": False,
                    "evidence": [{"source": source["id"], "quote": source["text"]}]})
        runtime.groq = Model()
        reply, _ = await runtime._generate_known_reply(chat_jid="test@g.us", sender_jid="member",
            message_id="q", text="When is mentoring?", allow_stub=False)
        assert "Tuesdays" in reply and "Friday" not in reply
    asyncio.run(run())
