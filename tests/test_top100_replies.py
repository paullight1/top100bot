from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from unipod_bot.config import Settings
from unipod_bot.runtime import Runtime
from unipod_bot.top100_activities import Top100Activity


def _settings() -> Settings:
    return Settings(
        knowledge_library_enabled=False,
        whatsapp_session_path=Path("/tmp/top100-afl-replies-session.db"),
        test_group_jid="test@g.us",
        debug=False,
        log_level=20,
        falkordb_host="localhost",
        falkordb_port=6379,
        falkordb_database="top100_afl",
        falkordb_username=None,
        falkordb_password=None,
        graphiti_group_id="top100_afl",
        admin_phones=("2348011111111",),
    )


def _activity() -> Top100Activity:
    return Top100Activity(
        id="event-1",
        slug="afl-leadership-workshop",
        title="AFL Leadership Workshop",
        summary="A practical leadership workshop",
        description="For Top100 awardees",
        start_at=datetime.fromisoformat("2026-11-12T09:00:00+01:00"),
        end_at=None,
        timezone="Africa/Lagos",
        location="Lagos, Nigeria",
        registration_url="https://register.example/afl",
        activity_url="https://register.example/afl",
        tags=("leadership",),
        programme_label="AFL",
        session_number=2,
    )


class FakeActivities:
    def __init__(self, activities=None, error=None):
        self.activities = activities or []
        self.error = error
        self.calls = []

    async def fetch_relevant(self, question, history=""):
        self.calls.append((question, history))
        if self.error:
            raise self.error
        return self.activities


class FakeStore:
    def __init__(self, facts=None):
        self.facts = facts or []

    async def search_facts(self, _query):
        return self.facts

    def is_grounded(self, facts):
        return bool(facts)


@pytest.mark.anyio
async def test_activity_facts_and_follow_up_history_reach_reply_prompt():
    runtime = Runtime(_settings(), SimpleNamespace())
    runtime.store = FakeStore([])
    runtime.recent.add(
        iso_time="2026-10-15T10:00:00+01:00",
        group_jid="test@g.us",
        sender_jid="2348999999999@s.whatsapp.net",
        text="We were discussing the AFL leadership workshop.",
    )
    runtime.top100_activities = FakeActivities([_activity()])
    runtime._retrieve_facts = AsyncMock(return_value=[])
    runtime.groq = AsyncMock()
    runtime.groq.generate = AsyncMock(return_value="It starts on 12 November at 9:00 AM. https://register.example/afl")

    reply, forwarded = await runtime._generate_known_reply(
        chat_jid="test@g.us",
        sender_jid="2348999999999@s.whatsapp.net",
        message_id="m-1",
        text="When does it start?",
        allow_stub=True,
    )

    assert reply and "12 November" in reply
    assert forwarded == []
    assert runtime.top100_activities.calls[0][1].find("AFL leadership workshop") >= 0
    prompt = runtime.groq.generate.await_args.args[0]
    assert "[Top100 public events API]" in prompt
    assert "https://register.example/afl" in prompt


@pytest.mark.anyio
async def test_irrelevant_question_does_not_call_top100_api():
    runtime = Runtime(_settings(), SimpleNamespace())
    activities = FakeActivities([_activity()])
    runtime.top100_activities = activities
    runtime.store = FakeStore(["[from @2348011111111] The test group meets on Tuesdays."])
    runtime.groq = AsyncMock()
    runtime.groq.generate = AsyncMock(return_value="The group meets on Tuesdays.")

    await runtime._generate_known_reply(
        chat_jid="test@g.us",
        sender_jid="2348999999999@s.whatsapp.net",
        message_id="m-2",
        text="When does our group meet?",
        allow_stub=True,
    )

    assert activities.calls == []


@pytest.mark.anyio
async def test_empty_or_failed_activity_api_keeps_unknown_fallback():
    for activities in (FakeActivities([]), FakeActivities(error=RuntimeError("offline"))):
        runtime = Runtime(_settings(), SimpleNamespace())
        runtime.store = FakeStore([])
        runtime.top100_activities = activities
        runtime._retrieve_facts = AsyncMock(return_value=[])
        runtime.groq = AsyncMock()
        runtime.groq.generate = AsyncMock(return_value="A grounded response.")

        reply, forwarded = await runtime._generate_known_reply(
            chat_jid="test@g.us",
            sender_jid="2348999999999@s.whatsapp.net",
            message_id="m-3",
            text="When is the AFL leadership workshop?",
            allow_stub=True,
        )

        assert reply is None
        assert forwarded == []


@pytest.mark.anyio
async def test_activity_api_failure_preserves_other_graphiti_grounded_reply():
    runtime = Runtime(_settings(), SimpleNamespace())
    runtime.store = FakeStore(["[from @2348011111111] The test group meets on Tuesdays."])
    runtime.top100_activities = FakeActivities(error=RuntimeError("offline"))
    runtime.groq = AsyncMock()
    runtime.groq.generate = AsyncMock(return_value="The group meets on Tuesdays.")

    reply, forwarded = await runtime._generate_known_reply(
        chat_jid="test@g.us",
        sender_jid="2348999999999@s.whatsapp.net",
        message_id="m-graphiti-fallback",
        text="When is the AFL workshop? Also when does our group meet?",
        allow_stub=True,
    )

    assert reply == "The group meets on Tuesdays."
    assert forwarded == []


@pytest.mark.anyio
async def test_admin_free_form_reply_uses_public_activity_facts():
    runtime = Runtime(_settings(), SimpleNamespace())
    runtime.store = FakeStore([])
    runtime.top100_activities = FakeActivities([_activity()])
    runtime._retrieve_facts = AsyncMock(return_value=[])
    runtime.groq = AsyncMock()
    runtime.groq.generate = AsyncMock(return_value="The workshop is on 12 November. https://register.example/afl")

    reply = await runtime._admin_groq_reply("When is the AFL leadership workshop?")

    assert "12 November" in reply
    assert "https://register.example/afl" in runtime.groq.generate.await_args.args[0]


@pytest.mark.anyio
async def test_top100_facts_ground_reply_without_graphiti_and_are_not_forwarded():
    runtime = Runtime(_settings(), SimpleNamespace())
    runtime.store = FakeStore([])
    runtime.top100_activities = FakeActivities([_activity()])
    runtime._retrieve_facts = AsyncMock(return_value=[])
    runtime.groq = AsyncMock()
    runtime.groq.generate = AsyncMock(return_value="The workshop is in Lagos on 12 November.")

    reply, forwarded = await runtime._generate_known_reply(
        chat_jid="test@g.us",
        sender_jid="2348999999999@s.whatsapp.net",
        message_id="m-4",
        text="When is the AFL leadership workshop?",
        allow_stub=True,
    )

    assert reply == "The workshop is in Lagos on 12 November."
    assert forwarded == []


@pytest.mark.anyio
async def test_today_activity_shortcut_receives_current_top100_records_and_link():
    runtime = Runtime(_settings(), SimpleNamespace())
    runtime.store = FakeStore([])
    runtime.top100_activities = FakeActivities([_activity()])
    runtime._retrieve_facts = AsyncMock(return_value=[])
    runtime.summary_store.get_messages_in_range = lambda *_args, **_kwargs: [
        {
            "sender_jid": "2348999999999@s.whatsapp.net",
            "iso_time": "2026-10-15T10:00:00+01:00",
            "text": "What activities are happening today?",
        }
    ]
    runtime.groq = AsyncMock()
    runtime.groq.generate = AsyncMock(return_value="The AFL leadership workshop is listed for today. https://register.example/afl")

    reply, forwarded = await runtime._generate_known_reply(
        chat_jid="test@g.us",
        sender_jid="2348999999999@s.whatsapp.net",
        message_id="m-today",
        text="What activities are happening today?",
        allow_stub=True,
    )

    assert reply and "https://register.example/afl" in reply
    assert forwarded == []
    assert "[Top100 public events API]" in runtime.groq.generate.await_args.args[0]


def test_top100_system_prompt_requires_public_activity_sources():
    from unipod_bot.groq_client import SYSTEM_PROMPT

    assert "Top100 Africa Future Leaders" in SYSTEM_PROMPT
    assert "public Top100 activity records" in SYSTEM_PROMPT
    assert "source link verbatim" in SYSTEM_PROMPT
