import asyncio
from types import SimpleNamespace

from unipod_bot.admin import handle_admin_dm
from unipod_bot.config import Settings
from unipod_bot.incoming import IncomingAdminDm, IncomingGroupMessage
from unipod_bot.recent import RecentGroupLog
from unipod_bot.runtime import Runtime, STUB_GROUP_REPLY
from pathlib import Path


def _settings(**kwargs) -> Settings:
    # Translate old groq_api_key= to new groq_api_keys= for backward compat
    if "groq_api_key" in kwargs:
        key = kwargs.pop("groq_api_key")
        kwargs.setdefault("groq_api_keys", (key,) if key else ())
    defaults = dict(
        knowledge_library_enabled=False,
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
        admin_phones=("2348011111111",),
    )
    defaults.update(kwargs)
    return Settings(**defaults)


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[object, str]] = []

    async def send_message(self, to, text):
        self.sent.append((to, text))


def _group(**kwargs) -> IncomingGroupMessage:
    base = dict(
        chat_jid="120363test@g.us",
        sender_jid="2348099999999@s.whatsapp.net",
        message_id="m1",
        text="when is the hackathon?",
        is_reply_group=True,
        iso_time="2026-09-19T18:00:00+00:00",
        mentioned_users=(),
        chat_obj=SimpleNamespace(User="120363test", Server="g.us"),
    )
    base.update(kwargs)
    return IncomingGroupMessage(**base)


def test_admin_recent_and_stub_commands():
    settings = _settings()
    log = RecentGroupLog()
    log.add(
        iso_time="t1",
        group_jid="120363test@g.us",
        sender_jid="2348@s.whatsapp.net",
        text="hello group",
    )
    result = handle_admin_dm(
        "recent 1",
        settings,
        stub_known=False,
        recent_formatter=log.format_lines,
        ingest_count=3,
        notify_count=1,
    )
    assert "hello group" in result.reply
    known = handle_admin_dm(
        "stub known",
        settings,
        stub_known=False,
        recent_formatter=log.format_lines,
        ingest_count=3,
        notify_count=1,
    )
    assert known.stub_known is True
    status = handle_admin_dm(
        "status",
        settings,
        stub_known=True,
        recent_formatter=log.format_lines,
        ingest_count=3,
        notify_count=1,
    )
    assert "Known" in status.reply
    assert "Messages logged: 3" in status.reply
    assert "Graphiti" in status.reply and "Off" in status.reply


def test_question_in_reply_group_notifies_admin():
    client = FakeClient()
    runtime = Runtime(_settings(), client)

    async def run() -> None:
        await runtime._handle_group(_group())

    asyncio.run(run())
    assert runtime.notify_count == 1
    assert len(client.sent) == 1
    body = client.sent[0][1]
    assert "when is the hackathon?" in body
    assert "Unanswered Question" in body


def test_banter_and_other_group_do_not_notify():
    client = FakeClient()
    runtime = Runtime(_settings(), client)

    async def run() -> None:
        await runtime._handle_group(_group(text="lol same"))
        await runtime._handle_group(
            _group(
                chat_jid="120363other@g.us",
                is_reply_group=False,
                text="when is lunch?",
            )
        )

    asyncio.run(run())
    assert client.sent == []
    assert runtime.notify_count == 0
    assert runtime.ingest_count == 2
    assert "lol same" in runtime.recent.format_lines(5)


def test_stub_known_replies_in_group():
    client = FakeClient()
    runtime = Runtime(_settings(), client)
    runtime.stub_known = True
    chat = SimpleNamespace(User="120363test", Server="g.us")

    async def run() -> None:
        await runtime._handle_group(_group(chat_obj=chat))

    asyncio.run(run())
    assert runtime.notify_count == 0
    assert client.sent == [(chat, STUB_GROUP_REPLY)]


class FakeGroq:
    def __init__(self, text: str) -> None:
        self.text = text
        self.prompts: list[str] = []

    async def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.text


def test_groq_answers_group_question_by_default_when_key_set():
    client = FakeClient()
    groq = FakeGroq("Friday at campus.")
    settings = _settings(groq_api_key="gsk_test")
    runtime = Runtime(settings, client, groq=groq)
    chat = SimpleNamespace(User="120363test", Server="g.us")

    async def run() -> None:
        await runtime._handle_group(_group(chat_obj=chat, text="when is the hackathon?"))

    asyncio.run(run())
    assert runtime.notify_count == 0
    assert client.sent == [(chat, "Friday at campus.")]


def test_admin_dm_question_uses_groq_recent_log():
    client = FakeClient()
    groq = FakeGroq("People said hello in the group.")
    runtime = Runtime(_settings(groq_api_key="gsk_test"), client, groq=groq)
    runtime.recent.add(
        iso_time="t1",
        group_jid="120363test@g.us",
        sender_jid="2541@s.whatsapp.net",
        text="Hello people",
    )
    dm = IncomingAdminDm(
        chat_jid="173@lid",
        sender_jid="2348011111111@s.whatsapp.net",
        message_id="d1",
        text="What is happening today",
        chat_obj="admin-chat",
    )

    async def run() -> None:
        await runtime._handle_admin_dm(dm)

    asyncio.run(run())
    assert client.sent[0][1] == "People said hello in the group."
    assert "Hello people" in groq.prompts[0]
