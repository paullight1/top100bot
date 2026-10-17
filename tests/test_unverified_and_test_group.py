from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from unipod_bot.config import Settings, parse_group_jids
from unipod_bot.incoming import (
    IncomingGroupMessage,
    IncomingUserDm,
    is_reply_group,
)
from unipod_bot.notify import format_unverified_info
from unipod_bot.runtime import Runtime, _extract_non_admin_facts


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[Any, str]] = []

    async def send_message(self, target: Any, text: str) -> None:
        self.sent.append((target, text))


class FakeGroq:
    def __init__(self, reply: str = "Test response") -> None:
        self.reply = reply
        self.prompts: list[str] = []
        self.chat_calls: list[list[dict[str, str]]] = []

    async def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.reply

    async def chat_completion(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_completion_tokens: int = 1024,
        reasoning_effort: str | None = "low",
    ) -> str:
        self.chat_calls.append(messages)
        return self.reply


class RetrievingStore:
    def __init__(self, facts: list[str]) -> None:
        self.facts = facts

    async def search_facts(self, query: str) -> list[str]:
        return self.facts

    def is_grounded(self, facts: list[str]) -> bool:
        return bool(facts)


def _settings(**kwargs) -> Settings:
    defaults = dict(
        knowledge_library_enabled=False,
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
        admin_phones=("2348011111111",),
        admin_jids=("2348011111111@s.whatsapp.net",),
    )
    if "groq_api_key" in kwargs:
        key = kwargs.pop("groq_api_key")
        if key:
            kwargs["groq_api_keys"] = (key,)
    defaults.update(kwargs)
    return Settings(**defaults)


def _group(
    *,
    chat_jid: str = "120363test@g.us",
    sender_jid: str = "2348099999999@s.whatsapp.net",
    text: str = "When is hackathon?",
    message_id: str = "m1",
    chat_obj: Any = None,
    is_reply: bool = True,
) -> IncomingGroupMessage:
    return IncomingGroupMessage(
        chat_jid=chat_jid,
        sender_jid=sender_jid,
        message_id=message_id,
        text=text,
        is_reply_group=is_reply,
        iso_time="2026-09-20T10:00:00+00:00",
        chat_obj=chat_obj or SimpleNamespace(User=chat_jid.split("@")[0], Server="g.us"),
    )


# --- Multi-group & normalization tests ---


def test_parse_group_jids():
    assert parse_group_jids(None) == ()
    assert parse_group_jids("") == ()
    assert parse_group_jids("120363test@g.us") == ("120363test@g.us",)
    assert parse_group_jids("1203631@g.us, 1203632@g.us") == ("1203631@g.us", "1203632@g.us")
    assert parse_group_jids("1203631, 1203632@g.us") == ("1203631@g.us", "1203632@g.us")
    assert parse_group_jids("*") == ("*",)
    assert parse_group_jids("all") == ("*",)


def test_is_reply_group_multiple():
    s = _settings(test_group_jids=("1203631@g.us", "1203632@g.us"))
    assert is_reply_group("1203631@g.us", s)
    assert is_reply_group("1203632@g.us", s)
    assert not is_reply_group("1203633@g.us", s)


def test_is_reply_group_from_comma_string():
    s = _settings(test_group_jid="1203631@g.us, 1203632@g.us")
    assert is_reply_group("1203631@g.us", s)
    assert is_reply_group("1203632@g.us", s)
    assert not is_reply_group("1203633@g.us", s)


def test_is_reply_group_numeric_normalization():
    s = _settings(test_group_jid="120363429618850959")
    assert is_reply_group("120363429618850959@g.us", s)


def test_is_reply_group_wildcard():
    s = _settings(test_group_jid="*")
    assert is_reply_group("120363anything@g.us", s)


def test_is_reply_group_empty_denies_all():
    s = _settings(test_group_jid=None, test_group_jids=())
    assert not is_reply_group("120363anything@g.us", s)


# --- Unverified info extraction & notification tests ---


def test_extract_non_admin_facts():
    admin_phones = ("2348011111111",)
    facts = [
        "[from @2348011111111] Admin says event is Friday",
        "[from @2348099999999] Member says event is Saturday",
        "[from @2348099999999 in group 120363test@g.us] Venue is Room 4",
        "Untagged fact about rules",
    ]
    unverified = _extract_non_admin_facts(facts, admin_phones)
    assert len(unverified) == 2
    assert "[from @2348099999999] Member says event is Saturday" in unverified
    assert "[from @2348099999999 in group 120363test@g.us] Venue is Room 4" in unverified
    assert "[from @2348011111111] Admin says event is Friday" not in unverified


def test_format_unverified_info_group_and_dm():
    group_msg = _group(text="What venue?")
    body_group = format_unverified_info(
        group_msg,
        "It is in Room 4.",
        ["[from @2348099999999] Venue is Room 4"],
    )
    assert "*Group:*" in body_group
    assert "Room 4" in body_group
    assert "in the group" in body_group

    dm_msg = IncomingUserDm(
        chat_jid="2348099999999@s.whatsapp.net",
        sender_jid="2348099999999@s.whatsapp.net",
        message_id="dm1",
        text="What venue?",
        iso_time="2026-09-20T10:00:00+00:00",
    )
    body_dm = format_unverified_info(
        dm_msg,
        "It is in Room 4.",
        ["[from @2348099999999] Venue is Room 4"],
    )
    assert "*DM Chat:*" in body_dm
    assert "Room 4" in body_dm


def test_group_reply_forwards_unverified_facts_to_admin():
    client = FakeClient()
    groq = FakeGroq("Venue is Room 4 as shared earlier.")
    runtime = Runtime(_settings(groq_api_key="gsk_test"), client, groq=groq)
    runtime.store = RetrievingStore(["[from @2348099999999] Venue is Room 4"])  # type: ignore[assignment]
    chat = SimpleNamespace(User="120363test", Server="g.us")

    async def run() -> None:
        await runtime._handle_group(_group(chat_obj=chat, text="What venue?"))

    asyncio.run(run())

    # 1st send: reply to group
    # 2nd send: unverified notification to admin
    assert len(client.sent) == 2
    assert client.sent[0][0] == chat
    assert client.sent[0][1] == "Venue is Room 4 as shared earlier."
    assert "⚠️ *Unverified Info Used in Reply*" in client.sent[1][1]
    assert "Venue is Room 4" in client.sent[1][1]


def test_user_dm_reply_forwards_unverified_facts_to_admin():
    client = FakeClient()
    groq = FakeGroq("Venue is Room 4.")
    runtime = Runtime(_settings(groq_api_key="gsk_test"), client, groq=groq)
    runtime.store = RetrievingStore(["[from @2348099999999] Venue is Room 4"])  # type: ignore[assignment]
    chat = SimpleNamespace(User="2348099999999", Server="s.whatsapp.net")
    dm = IncomingUserDm(
        chat_jid="2348099999999@s.whatsapp.net",
        sender_jid="2348099999999@s.whatsapp.net",
        message_id="dm1",
        text="What venue?",
        iso_time="2026-09-20T10:00:00+00:00",
        chat_obj=chat,
    )

    async def run() -> None:
        await runtime._handle_user_dm(dm)

    asyncio.run(run())

    assert len(client.sent) == 2
    assert client.sent[0][0] == chat
    assert client.sent[0][1] == "Venue is Room 4."
    assert "⚠️ *Unverified Info Used in Reply*" in client.sent[1][1]


def test_admin_question_in_group_does_not_notify_admin_on_unknown():
    """An unanswerable question is not an implicit request for admin escalation."""
    client = FakeClient()
    groq = FakeGroq("UNKNOWN")
    runtime = Runtime(_settings(groq_api_key="gsk_test"), client, groq=groq)
    runtime.store = RetrievingStore([])  # type: ignore[assignment]
    chat = SimpleNamespace(User="120363test", Server="g.us")

    # Sender is admin 2348011111111
    admin_group_msg = _group(
        chat_obj=chat,
        sender_jid="2348011111111@s.whatsapp.net",
        text="When is secret hackathon?",
    )

    async def run() -> None:
        await runtime._handle_group(admin_group_msg)

    asyncio.run(run())

    assert client.sent == []
    assert runtime.notify_count == 0


def test_bot_phone_registered_in_own_users():
    s = _settings(bot_phone="254735779584")
    client = FakeClient()
    runtime = Runtime(s, client)
    assert "254735779584" in runtime.own_users
    assert "254735779584@s.whatsapp.net" in runtime.own_users
