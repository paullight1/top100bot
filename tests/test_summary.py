from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace

from unipod_bot.config import Settings
from unipod_bot.incoming import IncomingGroupMessage, IncomingUserDm
from unipod_bot.runtime import Runtime
from unipod_bot.summary import (
    SummaryStore,
    SummaryWindow,
    format_messages_for_prompt,
    is_summary_request,
    is_today_question,
    parse_summary_time_range,
    SKILL_RECOMMENDATION_TIP,
    SUMMARY_HELP_CARD,
)


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[object, str]] = []

    async def send_message(self, to, text):
        self.sent.append((to, text))


class FakeGroq:
    def __init__(self, text: str = "Detailed executive briefing.") -> None:
        self.text = text
        self.prompts: list[str] = []

    async def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.text


def _settings(**kwargs) -> Settings:
    defaults = dict(
        whatsapp_session_path=Path("data/session.db"),
        test_group_jid="120363test@g.us",
        debug=False,
        log_level=20,
        falkordb_host="localhost",
        falkordb_port=6379,
        falkordb_database="default_db",
        falkordb_username=None,
        falkordb_password=None,
        graphiti_group_id="unipod_innovators",
        admin_phones=("2348011111111",),
        groq_api_keys=("gsk_test",),
    )
    defaults.update(kwargs)
    return Settings(**defaults)


def _group(**kwargs) -> IncomingGroupMessage:
    base = dict(
        chat_jid="120363test@g.us",
        sender_jid="254700000001@s.whatsapp.net",
        message_id="msg1",
        text="Hackathon registration is now open!",
        iso_time="2026-09-24T10:00:00+00:00",
        is_reply_group=True,
        chat_obj=SimpleNamespace(User="120363test", Server="g.us"),
        event_obj=None,
    )
    base.update(kwargs)
    return IncomingGroupMessage(**base)


# ---------------------------------------------------------------------------
# Test parsing
# ---------------------------------------------------------------------------

def test_parse_summary_exact_date():
    base = datetime(2026, 9, 24, 15, 0, 0, tzinfo=timezone.utc)

    # 24 sept
    w1 = parse_summary_time_range("/summary 24 sept", base_time=base)
    assert w1 is not None
    assert w1.start == datetime(2026, 9, 24, 0, 0, 0, tzinfo=timezone.utc)
    assert w1.end.day == 24
    assert "24 Sep 2026" in w1.label
    assert not w1.is_future

    # 24th september
    w2 = parse_summary_time_range("/summary 24th september", base_time=base)
    assert w2 is not None
    assert w2.start.month == 9 and w2.start.day == 24

    # ISO format 2026-09-24
    w3 = parse_summary_time_range("/summary 2026-09-24", base_time=base)
    assert w3 is not None
    assert w3.start.day == 24

    # Slash format 24/09
    w4 = parse_summary_time_range("/summary 24/09", base_time=base)
    assert w4 is not None
    assert w4.start.day == 24


def test_parse_summary_relative_ranges():
    base = datetime(2026, 9, 24, 15, 0, 0, tzinfo=timezone.utc)

    # today
    w_today = parse_summary_time_range("/summary today", base_time=base)
    assert w_today is not None
    assert w_today.start == datetime(2026, 9, 24, 0, 0, 0, tzinfo=timezone.utc)
    assert w_today.end == base
    assert "today" in w_today.label

    # yesterday
    w_yesterday = parse_summary_time_range("/summary yesterday", base_time=base)
    assert w_yesterday is not None
    assert w_yesterday.start == datetime(2026, 9, 23, 0, 0, 0, tzinfo=timezone.utc)
    assert "yesterday" in w_yesterday.label

    # last 2 days
    w_2d = parse_summary_time_range("/summary last 2 days", base_time=base)
    assert w_2d is not None
    assert w_2d.start == datetime(2026, 9, 22, 0, 0, 0, tzinfo=timezone.utc)
    assert "the last 2 days" in w_2d.label

    # 3 days
    w_3d = parse_summary_time_range("/summary 3 days", base_time=base)
    assert w_3d is not None
    assert w_3d.start == datetime(2026, 9, 21, 0, 0, 0, tzinfo=timezone.utc)

    # week
    w_week = parse_summary_time_range("/summary week", base_time=base)
    assert w_week is not None
    assert w_week.start == datetime(2026, 9, 17, 0, 0, 0, tzinfo=timezone.utc)

    # 2 weeks
    w_2w = parse_summary_time_range("/summary 2 weeks", base_time=base)
    assert w_2w is not None
    assert w_2w.start == datetime(2026, 9, 10, 0, 0, 0, tzinfo=timezone.utc)
    assert "the last 2 weeks" in w_2w.label

    # month
    w_month = parse_summary_time_range("/summary month", base_time=base)
    assert w_month is not None
    assert w_month.start == datetime(2026, 8, 25, 0, 0, 0, tzinfo=timezone.utc)


def test_parse_summary_future_and_empty():
    base = datetime(2026, 9, 24, 15, 0, 0, tzinfo=timezone.utc)

    # Future date
    w_future = parse_summary_time_range("/summary 28 sept", base_time=base)
    assert w_future is not None
    assert w_future.is_future is True

    # Empty command / help
    assert parse_summary_time_range("/summary", base_time=base) is None
    assert parse_summary_time_range("/summary help", base_time=base) is None


# ---------------------------------------------------------------------------
# Test request and question detection
# ---------------------------------------------------------------------------

def test_is_summary_request():
    assert is_summary_request("/summary 24 sept")
    assert is_summary_request("/summarize last 2 days")
    assert is_summary_request("@bot /summary today")
    assert not is_summary_request("can you give me a summary")
    assert not is_summary_request("what happened today?")


def test_is_today_question():
    assert is_today_question("what happened today?")
    assert is_today_question("what has been happening today")
    assert is_today_question("catch me up on today")
    assert is_today_question("what's going on today")
    assert not is_today_question("/summary today")  # Explicit /summary takes precedence


# ---------------------------------------------------------------------------
# Test SummaryStore SQLite persistence
# ---------------------------------------------------------------------------

def test_summary_store(tmp_path: Path):
    db_file = tmp_path / "messages.db"
    store = SummaryStore(db_file)

    store.add_message(
        message_id="m1",
        chat_jid="group1@g.us",
        sender_jid="254711111111@s.whatsapp.net",
        text="Hackathon meetup at 3pm",
        iso_time="2026-09-24T12:00:00+00:00",
        is_group=True,
    )
    store.add_message(
        message_id="m2",
        chat_jid="group1@g.us",
        sender_jid="254722222222@s.whatsapp.net",
        text="Looking for a frontend teammate",
        iso_time="2026-09-24T14:30:00+00:00",
        is_group=True,
    )
    store.add_message(
        message_id="m_old",
        chat_jid="group1@g.us",
        sender_jid="254711111111@s.whatsapp.net",
        text="Old message from last week",
        iso_time="2026-09-17T12:00:00+00:00",
        is_group=True,
    )

    t_start = datetime(2026, 9, 24, 0, 0, 0, tzinfo=timezone.utc)
    t_end = datetime(2026, 9, 24, 23, 59, 59, tzinfo=timezone.utc)

    results = store.get_messages_in_range(t_start, t_end)
    assert len(results) == 2
    assert results[0]["message_id"] == "m1"
    assert results[1]["message_id"] == "m2"

    prompt_lines = format_messages_for_prompt(results)
    assert "@254711111111" in prompt_lines
    assert "Hackathon meetup at 3pm" in prompt_lines
    assert "@254722222222" in prompt_lines


# ---------------------------------------------------------------------------
# Test Runtime integration
# ---------------------------------------------------------------------------

def test_runtime_summary_exact_date_flow(tmp_path: Path):
    client = FakeClient()
    groq = FakeGroq("### Executive Overview\nHackathon registration opened.")
    runtime = Runtime(_settings(), client, groq=groq)
    runtime.summary_store = SummaryStore(tmp_path / "messages.db")

    chat = SimpleNamespace(User="120363test", Server="g.us")

    async def run() -> None:
        # Ingest a message
        await runtime._handle_group(
            _group(
                chat_obj=chat,
                text="Hackathon registration is live!",
                message_id="m1",
                iso_time="2026-09-24T10:00:00+00:00",
            )
        )
        # Request summary for 24 sept
        await runtime._handle_group(
            _group(
                chat_obj=chat,
                text="/summary 24 sept",
                message_id="m2",
                iso_time="2026-09-24T16:00:00+00:00",
            )
        )

    asyncio.run(run())

    # Check groq was called with the detailed summary prompt
    assert len(groq.prompts) == 1
    assert "Hackathon registration is live!" in groq.prompts[0]
    assert "Executive Overview" in groq.prompts[0]

    # Bot replied to group with the summary
    assert len(client.sent) == 1
    assert "Executive Overview" in client.sent[0][1]


def test_runtime_summary_empty_date(tmp_path: Path):
    client = FakeClient()
    groq = FakeGroq("Should not be called")
    runtime = Runtime(_settings(), client, groq=groq)
    runtime.summary_store = SummaryStore(tmp_path / "messages.db")

    chat = SimpleNamespace(User="120363test", Server="g.us")

    async def run() -> None:
        # Request summary for a date with no recorded messages
        await runtime._handle_group(
            _group(
                chat_obj=chat,
                text="/summary 15 aug",
                message_id="m1",
                iso_time="2026-09-24T16:00:00+00:00",
            )
        )

    asyncio.run(run())

    assert len(groq.prompts) == 0
    assert len(client.sent) == 1
    assert "No community messages or activity were recorded for 15 Aug 2026" in client.sent[0][1]


def test_runtime_summary_help_when_no_arguments():
    client = FakeClient()
    groq = FakeGroq("Should not be called")
    runtime = Runtime(_settings(), client, groq=groq)

    chat = SimpleNamespace(User="120363test", Server="g.us")

    async def run() -> None:
        await runtime._handle_group(
            _group(
                chat_obj=chat,
                text="/summary",
                message_id="m1",
            )
        )

    asyncio.run(run())

    assert len(groq.prompts) == 0
    assert len(client.sent) == 1
    assert "Unipod Summary Skill" in client.sent[0][1]


def test_runtime_conversational_today_question(tmp_path: Path):
    client = FakeClient()
    groq = FakeGroq("Today members discussed the upcoming AI hackathon and formed 2 teams.")
    runtime = Runtime(_settings(), client, groq=groq)
    runtime.summary_store = SummaryStore(tmp_path / "messages.db")

    chat = SimpleNamespace(User="120363test", Server="g.us")
    now_iso = datetime.now(timezone.utc).isoformat()

    async def run() -> None:
        # Ingest message today
        await runtime._handle_group(
            _group(
                chat_obj=chat,
                text="We are forming teams for the AI hackathon today!",
                message_id="m1",
                iso_time=now_iso,
            )
        )
        # Ask open question
        await runtime._handle_group(
            _group(
                chat_obj=chat,
                text="What happened today in the group?",
                message_id="m2",
                iso_time=now_iso,
            )
        )

    asyncio.run(run())

    assert len(groq.prompts) == 1
    assert "concise, clear summary of what happened today" in groq.prompts[0]
    assert len(client.sent) == 1
    reply = client.sent[0][1]
    # Check that concise summary is returned AND the skill recommendation tip is appended
    assert "AI hackathon" in reply
    assert "💡 Tip: For an in-depth, structured report, you can use the /summary skill" in reply
    assert "skill" in reply
