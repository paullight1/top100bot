"""Unit tests for meeting and event reminders."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from unipod_bot.config import Settings
from unipod_bot.incoming import IncomingGroupMessage
from unipod_bot.reminders import (
    ReminderStore,
    detect_meeting_link,
    extract_meeting_details,
    format_30m_reminder,
    format_start_reminder,
    format_unverified_meeting_alert,
    parse_meeting_time,
)
from unipod_bot.runtime import Runtime


# ---------------------------------------------------------------------------
# Link Detection & Extraction Tests
# ---------------------------------------------------------------------------

def test_detect_meeting_link_google_meet():
    text = "Join our sync here: https://meet.google.com/abc-defg-hij tomorrow at 10am"
    res = detect_meeting_link(text)
    assert res is not None
    link, platform = res
    assert link == "https://meet.google.com/abc-defg-hij"
    assert platform == "Google Meet"


def test_detect_meeting_link_skype():
    text = "Team call on Skype: https://join.skype.com/AbCdEf1234 at 4pm"
    res = detect_meeting_link(text)
    assert res is not None
    link, platform = res
    assert link == "https://join.skype.com/AbCdEf1234"
    assert platform == "Skype"


def test_detect_meeting_link_zoom():
    text = "Workshop zoom: https://us02web.zoom.us/j/1234567890?pwd=xyz in 45 mins"
    res = detect_meeting_link(text)
    assert res is not None
    link, platform = res
    assert "zoom.us" in link
    assert platform == "Zoom"


def test_detect_meeting_link_teams():
    text = "Office hours: https://teams.microsoft.com/l/meetup-join/19%3ameeting tomorrow at 2pm"
    res = detect_meeting_link(text)
    assert res is not None
    link, platform = res
    assert "teams.microsoft.com" in link
    assert platform == "Microsoft Teams"


def test_detect_meeting_link_none():
    assert detect_meeting_link("Just a normal message without link") is None
    assert detect_meeting_link("Visit https://unipod.org for hackathon info") is None


# ---------------------------------------------------------------------------
# Time Parsing Tests
# ---------------------------------------------------------------------------

def test_parse_meeting_time_relative_mins():
    base = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)
    parsed = parse_meeting_time("Meeting in 45 minutes https://meet.google.com/xyz", base)
    assert parsed is not None
    assert parsed == base + timedelta(minutes=45)


def test_parse_meeting_time_relative_hours():
    base = datetime(2026, 9, 23, 10, 0, 0, tzinfo=timezone.utc)
    parsed = parse_meeting_time("Call in 2 hours https://meet.google.com/xyz", base)
    assert parsed is not None
    assert parsed == base + timedelta(hours=2)


def test_parse_meeting_time_today_pm():
    base = datetime(2026, 9, 23, 10, 0, 0, tzinfo=timezone.utc)
    parsed = parse_meeting_time("Demo at 4:30 pm https://meet.google.com/xyz", base)
    assert parsed is not None
    assert parsed.hour == 16
    assert parsed.minute == 30
    assert parsed.day == 23


def test_parse_meeting_time_tomorrow_am():
    base = datetime(2026, 9, 23, 15, 0, 0, tzinfo=timezone.utc)
    parsed = parse_meeting_time("Standup tomorrow at 9am https://meet.google.com/xyz", base)
    assert parsed is not None
    assert parsed.hour == 9
    assert parsed.minute == 0
    assert parsed.day == 24


def test_extract_meeting_details_with_time():
    text = "Sprint Planning tomorrow at 3pm: https://meet.google.com/abc-defg-hij"
    base = datetime(2026, 9, 23, 10, 0, 0, tzinfo=timezone.utc)
    meeting = extract_meeting_details(text, base)
    assert meeting is not None
    assert not meeting.needs_admin_confirmation
    assert meeting.meeting_type == "Google Meet"
    assert meeting.scheduled_time is not None
    assert meeting.scheduled_time.hour == 15
    assert "Sprint Planning" in meeting.topic


def test_extract_meeting_details_missing_time_needs_confirmation():
    text = "Here is the meeting link for anyone interested: https://meet.google.com/abc-defg-hij"
    meeting = extract_meeting_details(text)
    assert meeting is not None
    assert meeting.needs_admin_confirmation is True
    assert meeting.scheduled_time is None
    assert "No verifiable date or time" in meeting.confirmation_reason


# ---------------------------------------------------------------------------
# ReminderStore Tests
# ---------------------------------------------------------------------------

def test_reminder_store_flow(tmp_path: Path):
    db_file = tmp_path / "test_reminders.db"
    store = ReminderStore(db_path=db_file)

    now = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)
    m_time = now + timedelta(minutes=25)  # due in 25 mins (within 30m window)

    # 1. Add meeting
    reminder = store.add_meeting(
        chat_jid="120363@g.us",
        sender_jid="254142340147@s.whatsapp.net",
        meeting_link="https://meet.google.com/abc-defg-hij",
        meeting_type="Google Meet",
        topic="Project Review",
        scheduled_time=m_time,
    )
    assert reminder.id
    assert reminder.status == "scheduled"

    # 2. Check 30m due
    due_30m = store.get_due_30m(now)
    assert len(due_30m) == 1
    assert due_30m[0].id == reminder.id

    # Mark 30m sent
    store.mark_30m_sent(reminder.id)
    assert len(store.get_due_30m(now)) == 0

    # 3. Fast-forward to start time
    start_now = m_time
    due_start = store.get_due_start(start_now)
    assert len(due_start) == 1
    assert due_start[0].id == reminder.id

    # Mark start sent
    store.mark_start_sent(reminder.id)
    assert len(store.get_due_start(start_now)) == 0

    # Verify completed
    active = store.get_active_meetings("120363@g.us")
    assert len(active) == 0


def test_reminder_store_cancel_by_link(tmp_path: Path):
    db_file = tmp_path / "test_reminders.db"
    store = ReminderStore(db_path=db_file)

    now = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)
    reminder = store.add_meeting(
        chat_jid="120363@g.us",
        sender_jid="254142340147@s.whatsapp.net",
        meeting_link="https://meet.google.com/cancel-me",
        meeting_type="Google Meet",
        topic="Cancelled Meeting",
        scheduled_time=now + timedelta(hours=1),
    )
    assert len(store.get_active_meetings("120363@g.us")) == 1

    cancelled = store.cancel_by_link("120363@g.us", "https://meet.google.com/cancel-me")
    assert cancelled == 1
    assert len(store.get_active_meetings("120363@g.us")) == 0


# ---------------------------------------------------------------------------
# Message Formatters Tests
# ---------------------------------------------------------------------------

def test_format_30m_reminder():
    from unipod_bot.reminders import MeetingReminder
    reminder = MeetingReminder(
        id="123",
        chat_jid="120363@g.us",
        sender_jid="254142340147@s.whatsapp.net",
        meeting_link="https://meet.google.com/test",
        meeting_type="Google Meet",
        topic="Hackathon Ideation",
        scheduled_time_iso=datetime.now(timezone.utc).isoformat(),
        status="scheduled",
        reminded_30m=False,
        reminded_start=False,
        created_at_iso=datetime.now(timezone.utc).isoformat(),
    )
    msg = format_30m_reminder(reminder)
    assert "in 30 mins" in msg
    assert "Hackathon Ideation" in msg
    assert "https://meet.google.com/test" in msg


def test_format_start_reminder():
    from unipod_bot.reminders import MeetingReminder
    reminder = MeetingReminder(
        id="123",
        chat_jid="120363@g.us",
        sender_jid="254142340147@s.whatsapp.net",
        meeting_link="https://meet.google.com/test",
        meeting_type="Google Meet",
        topic="Hackathon Ideation",
        scheduled_time_iso=datetime.now(timezone.utc).isoformat(),
        status="scheduled",
        reminded_30m=False,
        reminded_start=False,
        created_at_iso=datetime.now(timezone.utc).isoformat(),
    )
    msg = format_start_reminder(reminder)
    assert "Starting Now" in msg
    assert "https://meet.google.com/test" in msg


# ---------------------------------------------------------------------------
# Runtime Integration Tests
# ---------------------------------------------------------------------------

class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[Any, str]] = []

    async def send_message(self, target: Any, text: str) -> None:
        self.sent.append((target, text))


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        whatsapp_session_path=tmp_path / "session.db",
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
        admin_phones=("254102838378",),
        admin_jids=("254102838378@s.whatsapp.net",),
    )


@pytest.mark.anyio
async def test_runtime_schedules_meeting_and_alerts_cancellation(tmp_path: Path):
    settings = _settings(tmp_path)
    client = FakeClient()
    runtime = Runtime(settings, client)
    # Use isolated test DB for reminders
    runtime.reminder_store = ReminderStore(db_path=tmp_path / "reminders.db")

    # 1. Message with valid meeting link and time
    msg1 = IncomingGroupMessage(
        chat_jid="120363111@g.us",
        sender_jid="254142340147@s.whatsapp.net",
        message_id="M1",
        text="Sync tomorrow at 4pm: https://meet.google.com/xyz-123-abc",
        is_reply_group=True,
        iso_time="2026-09-23T10:00:00+00:00",
    )
    await runtime._check_meeting_reminders(msg1)
    meetings = runtime.reminder_store.get_active_meetings("120363111@g.us")
    assert len(meetings) == 1
    assert meetings[0].meeting_link == "https://meet.google.com/xyz-123-abc"
    assert meetings[0].scheduled_time.hour == 16

    # 2. Member posts that the meeting is cancelled
    msg2 = IncomingGroupMessage(
        chat_jid="120363111@g.us",
        sender_jid="254142340147@s.whatsapp.net",
        message_id="M2",
        text="The meeting https://meet.google.com/xyz-123-abc is cancelled for tomorrow",
        is_reply_group=True,
        iso_time="2026-09-23T10:05:00+00:00",
    )
    await runtime._check_meeting_reminders(msg2)
    # Meeting should be marked cancelled
    assert len(runtime.reminder_store.get_active_meetings("120363111@g.us")) == 0
    # Admin should receive notification about the cancellation
    assert len(client.sent) >= 1
    assert "Meeting Verification Required" in client.sent[-1][1] or "cancelled" in client.sent[-1][1].lower()


@pytest.mark.anyio
async def test_runtime_unverified_meeting_alerts_admin(tmp_path: Path):
    settings = _settings(tmp_path)
    client = FakeClient()
    runtime = Runtime(settings, client)
    runtime.reminder_store = ReminderStore(db_path=tmp_path / "reminders.db")

    # Message with meeting link but NO time
    msg = IncomingGroupMessage(
        chat_jid="120363222@g.us",
        sender_jid="254142340147@s.whatsapp.net",
        message_id="M3",
        text="Here is our skype link https://join.skype.com/team-call",
        is_reply_group=True,
        iso_time="2026-09-23T10:00:00+00:00",
    )
    await runtime._check_meeting_reminders(msg)

    # Should notify admin that time is missing
    assert len(client.sent) >= 1
    admin_alert = client.sent[-1][1]
    assert "Meeting Verification Required" in admin_alert
    assert "https://join.skype.com/team-call" in admin_alert


def test_detect_manual_reminder_user_prompt():
    from unipod_bot.reminders import detect_manual_reminder
    base = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    text = "@bot can you set a reminder for the next 10 mins so that ai can start my meeting"
    rem = detect_manual_reminder(text, "254712345678@s.whatsapp.net", base_time=base)
    assert rem is not None
    assert rem.topic == "Start my meeting"
    assert rem.scheduled_time == datetime(2026, 9, 24, 12, 10, tzinfo=timezone.utc)
    assert rem.target_tag == "@254712345678"


@pytest.mark.anyio
async def test_manual_reminder_group_flow(tmp_path: Path):
    from unipod_bot.incoming import IncomingGroupMessage
    from unipod_bot.reminders import ReminderStore
    from unipod_bot.runtime import Runtime

    settings = _settings(tmp_path)
    client = FakeClient()
    runtime = Runtime(settings, client)
    runtime.reminder_store = ReminderStore(db_path=tmp_path / "reminders.db")
    base = datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc)

    msg = IncomingGroupMessage(
        chat_jid="120363111@g.us",
        sender_jid="254712345678@s.whatsapp.net",
        message_id="REM1",
        text="@bot can you set a reminder for the next 10 mins so that ai can start my meeting",
        is_reply_group=True,
        iso_time=base.isoformat(),
        chat_obj=SimpleNamespace(User="120363111", Server="g.us"),
    )
    await runtime._handle_group(msg)

    # 1. Bot sends confirmation
    assert len(client.sent) == 1
    assert "Reminder Set!" in client.sent[0][1]
    assert "Start my meeting" in client.sent[0][1]
    assert "@254712345678" in client.sent[0][1]

    # 2. Reminder is saved in store
    active = runtime.reminder_store.get_active_meetings("120363111@g.us")
    assert len(active) == 1
    assert active[0].topic == "Start my meeting"
    assert active[0].meeting_type == "reminder"

    # 3. Fast forward time by 10 minutes and check trigger
    trigger_time = base + timedelta(minutes=10, seconds=1)
    due_start = runtime.reminder_store.get_due_start(trigger_time)
    assert len(due_start) == 1
    from unipod_bot.reminders import format_start_reminder
    alert = format_start_reminder(due_start[0])
    assert "Reminder for @254712345678" in alert
    assert "Start my meeting" in alert


@pytest.mark.anyio
async def test_manual_reminder_dm_flow(tmp_path: Path):
    from unipod_bot.incoming import IncomingUserDm
    from unipod_bot.reminders import ReminderStore
    from unipod_bot.runtime import Runtime

    settings = _settings(tmp_path)
    client = FakeClient()
    runtime = Runtime(settings, client)
    runtime.reminder_store = ReminderStore(db_path=tmp_path / "reminders.db")
    base = datetime(2026, 9, 24, 14, 0, tzinfo=timezone.utc)

    dm = IncomingUserDm(
        chat_jid="254712345678@s.whatsapp.net",
        sender_jid="254712345678@s.whatsapp.net",
        message_id="DM_REM1",
        text="Remind me in 30 mins to join the workshop",
        iso_time=base.isoformat(),
        chat_obj=SimpleNamespace(User="254712345678", Server="s.whatsapp.net"),
    )
    await runtime._handle_user_dm(dm)

    assert len(client.sent) == 1
    assert "Reminder Set!" in client.sent[0][1]
    assert "Join the workshop" in client.sent[0][1]

    active = runtime.reminder_store.get_active_meetings("254712345678@s.whatsapp.net")
    assert len(active) == 1
    assert active[0].topic == "Join the workshop"


@pytest.mark.anyio
async def test_slash_reminder_command_with_shorthand(tmp_path: Path):
    from unipod_bot.incoming import IncomingGroupMessage
    from unipod_bot.reminders import ReminderStore
    from unipod_bot.runtime import Runtime

    settings = _settings(tmp_path)
    client = FakeClient()
    runtime = Runtime(settings, client)
    runtime.reminder_store = ReminderStore(db_path=tmp_path / "reminders.db")
    base = datetime(2026, 9, 24, 11, 0, tzinfo=timezone.utc)

    msg = IncomingGroupMessage(
        chat_jid="120363111@g.us",
        sender_jid="254712345678@s.whatsapp.net",
        message_id="CMD_REM1",
        text="/reminder 10m start meeting",
        is_reply_group=True,
        iso_time=base.isoformat(),
        chat_obj=SimpleNamespace(User="120363111", Server="g.us"),
    )
    await runtime._handle_group(msg)

    assert len(client.sent) == 1
    assert "Reminder Set!" in client.sent[0][1]
    assert "Start meeting" in client.sent[0][1]
    assert "in 10 minutes" in client.sent[0][1]


@pytest.mark.anyio
async def test_interactive_reminder_prompt_and_followup(tmp_path: Path):
    from unipod_bot.incoming import IncomingGroupMessage
    from unipod_bot.reminders import ReminderStore
    from unipod_bot.runtime import Runtime

    settings = _settings(tmp_path)
    client = FakeClient()
    runtime = Runtime(settings, client)
    runtime.reminder_store = ReminderStore(db_path=tmp_path / "reminders.db")
    base = datetime(2026, 9, 24, 15, 0, tzinfo=timezone.utc)

    # Step 1: User says /reminder with no details
    msg1 = IncomingGroupMessage(
        chat_jid="120363111@g.us",
        sender_jid="254712345678@s.whatsapp.net",
        message_id="INTERACT1",
        text="/reminder",
        is_reply_group=True,
        iso_time=base.isoformat(),
        chat_obj=SimpleNamespace(User="120363111", Server="g.us"),
    )
    await runtime._handle_group(msg1)

    # Bot prompts interactively
    assert len(client.sent) == 1
    assert "Interactive Reminder Assistant" in client.sent[0][1]
    assert runtime.interactive_reminders.has_pending("120363111@g.us", "254712345678@s.whatsapp.net")

    # Step 2: User provides time and topic
    msg2 = IncomingGroupMessage(
        chat_jid="120363111@g.us",
        sender_jid="254712345678@s.whatsapp.net",
        message_id="INTERACT2",
        text="in 15 mins to call the hackathon lead",
        is_reply_group=True,
        iso_time=base.isoformat(),
        chat_obj=SimpleNamespace(User="120363111", Server="g.us"),
    )
    await runtime._handle_group(msg2)

    # Bot confirms and pending state is cleared
    assert len(client.sent) == 2
    assert "Reminder Set!" in client.sent[1][1]
    assert "Call the hackathon lead" in client.sent[1][1]
    assert not runtime.interactive_reminders.has_pending("120363111@g.us", "254712345678@s.whatsapp.net")


@pytest.mark.anyio
async def test_interactive_reminder_cancellation(tmp_path: Path):
    from unipod_bot.incoming import IncomingGroupMessage
    from unipod_bot.reminders import ReminderStore
    from unipod_bot.runtime import Runtime

    settings = _settings(tmp_path)
    client = FakeClient()
    runtime = Runtime(settings, client)
    runtime.reminder_store = ReminderStore(db_path=tmp_path / "reminders.db")

    # User initiates reminder
    msg1 = IncomingGroupMessage(
        chat_jid="120363111@g.us",
        sender_jid="254712345678@s.whatsapp.net",
        message_id="CANC1",
        text="@bot set a reminder",
        is_reply_group=True,
        iso_time="2026-09-24T10:00:00+00:00",
        chat_obj=SimpleNamespace(User="120363111", Server="g.us"),
    )
    await runtime._handle_group(msg1)
    assert runtime.interactive_reminders.has_pending("120363111@g.us", "254712345678@s.whatsapp.net")

    # User cancels
    msg2 = IncomingGroupMessage(
        chat_jid="120363111@g.us",
        sender_jid="254712345678@s.whatsapp.net",
        message_id="CANC2",
        text="cancel",
        is_reply_group=True,
        iso_time="2026-09-24T10:01:00+00:00",
        chat_obj=SimpleNamespace(User="120363111", Server="g.us"),
    )
    await runtime._handle_group(msg2)
    assert "Reminder setup cancelled" in client.sent[-1][1]
    assert not runtime.interactive_reminders.has_pending("120363111@g.us", "254712345678@s.whatsapp.net")


@pytest.mark.anyio
async def test_reminder_list_and_cancel_command(tmp_path: Path):
    from unipod_bot.incoming import IncomingGroupMessage
    from unipod_bot.reminders import ReminderStore
    from unipod_bot.runtime import Runtime

    settings = _settings(tmp_path)
    client = FakeClient()
    runtime = Runtime(settings, client)
    runtime.reminder_store = ReminderStore(db_path=tmp_path / "reminders.db")
    base = datetime(2026, 9, 24, 16, 0, tzinfo=timezone.utc)

    # Add a reminder via /reminder
    msg_add = IncomingGroupMessage(
        chat_jid="120363111@g.us",
        sender_jid="254712345678@s.whatsapp.net",
        message_id="M_ADD",
        text="/reminder 30m team sync",
        is_reply_group=True,
        iso_time=base.isoformat(),
        chat_obj=SimpleNamespace(User="120363111", Server="g.us"),
    )
    await runtime._handle_group(msg_add)

    # List active reminders
    msg_list = IncomingGroupMessage(
        chat_jid="120363111@g.us",
        sender_jid="254712345678@s.whatsapp.net",
        message_id="M_LIST",
        text="/reminder list",
        is_reply_group=True,
        iso_time=base.isoformat(),
        chat_obj=SimpleNamespace(User="120363111", Server="g.us"),
    )
    await runtime._handle_group(msg_list)
    assert "Active Reminders in this Chat" in client.sent[-1][1]
    assert "Team sync" in client.sent[-1][1]

    # Cancel by topic keyword
    msg_cancel = IncomingGroupMessage(
        chat_jid="120363111@g.us",
        sender_jid="254712345678@s.whatsapp.net",
        message_id="M_CANCEL",
        text="/reminder cancel team sync",
        is_reply_group=True,
        iso_time=base.isoformat(),
        chat_obj=SimpleNamespace(User="120363111", Server="g.us"),
    )
    await runtime._handle_group(msg_cancel)
    assert "Reminder Cancelled!" in client.sent[-1][1]
    assert "Team sync" in client.sent[-1][1]
    assert len(runtime.reminder_store.get_active_meetings("120363111@g.us")) == 0


