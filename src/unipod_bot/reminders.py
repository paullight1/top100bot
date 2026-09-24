"""Meeting and event reminders module for Unipod WhatsApp Community Bot.

Detects meeting links (Google Meet, Skype, Zoom, Microsoft Teams) shared in groups,
schedules reminders in a persistent SQLite store, and sends:
1. Advance notice 30 minutes before the meeting starts.
2. Start notice with meeting links when the meeting begins.
3. Forwards unverified or conflicting meeting information to admins for confirmation.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("unipod_bot")


class InteractiveReminderTracker:
    """In-memory tracker for users currently in interactive reminder setup."""

    def __init__(self, ttl_seconds: int = 300) -> None:
        self.ttl = ttl_seconds
        self._pending: dict[tuple[str, str], float] = {}

    def set_pending(self, chat_jid: str, sender_jid: str) -> None:
        self._pending[(chat_jid, sender_jid)] = time.time()

    def has_pending(self, chat_jid: str, sender_jid: str) -> bool:
        ts = self._pending.get((chat_jid, sender_jid))
        if ts is None:
            return False
        if time.time() - ts > self.ttl:
            self._pending.pop((chat_jid, sender_jid), None)
            return False
        return True

    def clear_pending(self, chat_jid: str, sender_jid: str) -> None:
        self._pending.pop((chat_jid, sender_jid), None)

# ---------------------------------------------------------------------------
# Regex patterns for meeting links
# ---------------------------------------------------------------------------

_MEET_RE = re.compile(r"https?://meet\.google\.com/[a-zA-Z0-9_-]+", re.IGNORECASE)
_SKYPE_RE = re.compile(r"https?://join\.skype\.com/[a-zA-Z0-9_-]+", re.IGNORECASE)
_ZOOM_RE = re.compile(r"https?://(?:[a-zA-Z0-9_-]+\.)?zoom\.us/(?:j|my)/[a-zA-Z0-9?=_&%-]+", re.IGNORECASE)
_TEAMS_RE = re.compile(r"https?://teams\.microsoft\.com/[a-zA-Z0-9_./?=&#%-]+", re.IGNORECASE)

# Keywords indicating cancellation or opposition
_CANCEL_RE = re.compile(
    r"\b(cancelled|canceled|cancelling|postponed|rescheduled|call off|not happening|no meeting)\b",
    re.IGNORECASE,
)


@dataclass
class ExtractedMeeting:
    meeting_link: str
    meeting_type: str
    topic: str
    scheduled_time: Optional[datetime]
    needs_admin_confirmation: bool
    confirmation_reason: str = ""


@dataclass
class MeetingReminder:
    id: str
    chat_jid: str
    sender_jid: str
    meeting_link: str
    meeting_type: str
    topic: str
    scheduled_time_iso: str
    status: str  # 'scheduled', 'needs_admin_confirmation', 'cancelled', 'completed'
    reminded_30m: bool
    reminded_start: bool
    created_at_iso: str

    @property
    def scheduled_time(self) -> datetime:
        return datetime.fromisoformat(self.scheduled_time_iso)


def detect_meeting_link(text: str) -> tuple[str, str] | None:
    """Return (link, platform_name) if a meeting link is found in text."""
    if not text:
        return None
    m = _MEET_RE.search(text)
    if m:
        return m.group(0), "Google Meet"
    m = _SKYPE_RE.search(text)
    if m:
        return m.group(0), "Skype"
    m = _ZOOM_RE.search(text)
    if m:
        return m.group(0), "Zoom"
    m = _TEAMS_RE.search(text)
    if m:
        return m.group(0), "Microsoft Teams"
    return None


@dataclass
class ManualReminder:
    scheduled_time: datetime
    topic: str
    sender_jid: str
    target_tag: str


_REMINDER_CMD_RE = re.compile(r"(?:^|\s)/(?:reminder|remind)\b", re.IGNORECASE)
_MANUAL_REMINDER_RE = re.compile(
    r"(?:^|\s)/(?:reminder|remind)\b|\b(?:remind\s+(?:me|us|everyone)|set\s+(?:a\s+)?reminder|reminder\b)",
    re.IGNORECASE,
)
_TOPIC_CONNECTORS = re.compile(
    r"\b(?:so\s+that|to|for|about|that)\s+(.+)$|:\s*(.+)$|-\s*(.+)$",
    re.IGNORECASE,
)
_STRIP_PREFIX_RE = re.compile(
    r"^(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?(?:set\s+(?:a\s+)?reminder|remind\s+(?:me|us|everyone)|reminder)\s*",
    re.IGNORECASE,
)
_TIME_PHRASE_RE = re.compile(
    r"\b(?:(?:in|for|after)\s+(?:the\s+next\s+)?\d+\s*(?:mins?|minutes?|hours?|hrs?|seconds?|secs?|m|h)|"
    r"\d+\s*(?:mins?|minutes?|hours?|hrs?|seconds?|secs?|m|h)|"
    r"(?:tomorrow|today)?\s*(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b",
    re.IGNORECASE,
)
_LIST_RE = re.compile(r"\b(?:list|show|all|status|active)\b", re.IGNORECASE)
_CANCEL_CMD_RE = re.compile(
    r"(?:/(?:reminder|remind)\s+cancel\s+(.+)|(?:cancel|delete)\s+reminder\s+(.+))",
    re.IGNORECASE,
)


def extract_reminder_topic(text: str) -> str:
    clean = re.sub(r"@\S+", "", text).strip()
    clean = re.sub(r"(?i)/(?:reminder|remind)\b", "", clean).strip()
    clean = _STRIP_PREFIX_RE.sub("", clean).strip()
    clean_no_time = _TIME_PHRASE_RE.sub("", clean).strip()
    m = _TOPIC_CONNECTORS.search(clean_no_time)
    if m:
        topic = (m.group(1) or m.group(2) or m.group(3) or "").strip()
        topic = re.sub(r"^(?:ai\s+can|i\s+can)\s+", "", topic, flags=re.IGNORECASE).strip()
        if topic:
            return topic[0].upper() + topic[1:]
    fallback = re.sub(r"^[:,\-\s]+|[:,\-\s]+$", "", clean_no_time).strip()
    if fallback:
        return fallback[0].upper() + fallback[1:]
    return "Meeting / Task"


def is_reminder_intent(text: str) -> bool:
    if not text:
        return False
    return bool(_MANUAL_REMINDER_RE.search(text) or _REMINDER_CMD_RE.search(text))


def is_reminder_list_request(text: str) -> bool:
    t = text.strip()
    if _REMINDER_CMD_RE.search(t) and _LIST_RE.search(t):
        return True
    if re.search(r"\b(?:my reminders|list reminders|show reminders|active reminders)\b", t, re.IGNORECASE):
        return True
    return False


def is_reminder_cancel_request(text: str) -> str | None:
    t = text.strip()
    m = _CANCEL_CMD_RE.search(t)
    if m:
        return (m.group(1) or m.group(2) or "").strip()
    if re.search(r"^/(?:reminder|remind)\s+cancel\b", t, re.IGNORECASE):
        parts = t.split(None, 2)
        if len(parts) >= 3:
            return parts[2].strip()
    return None


def is_reminder_help_or_empty(text: str) -> bool:
    t = re.sub(r"@\S+", "", text).strip()
    if re.fullmatch(r"/(?:reminder|remind)(?:\s+help|\s*\?)?", t, re.IGNORECASE):
        return True
    if re.fullmatch(r"(?:please\s+)?(?:set\s+(?:a\s+)?reminder|remind\s+(?:me|us|everyone)|reminder)(?:\s+please)?", t, re.IGNORECASE):
        return True
    return False


def is_reminder_cancel_flow(text: str) -> bool:
    t = text.strip().lower()
    return t in {"cancel", "nevermind", "never mind", "stop", "exit", "quit"}


def format_reminder_interactive_prompt(target_tag: str = "") -> str:
    tag_str = f" for {target_tag}" if target_tag else ""
    return (
        f"⏰ *Interactive Reminder Assistant{tag_str}* 🎯\n\n"
        f"I can remind you about meetings, tasks, hackathons, or deadlines!\n\n"
        f"Please reply with *when* and *what* you would like to be reminded of:\n"
        f"• *When:* e.g. `in 10 mins`, `at 4:30 PM`, `tomorrow at 9 AM`\n"
        f"• *What:* e.g. `start meeting`, `submit code`, `team sync`\n\n"
        f"💡 *One-line Examples:*\n"
        f"• `/reminder 10m start meeting`\n"
        f"• `/reminder tomorrow at 9am project submission`\n"
        f"• `@bot set a reminder for the next 10 mins so that I can start my meeting`\n\n"
        f"_Or send `cancel` to exit._"
    )


def format_active_reminders_list(reminders: list[MeetingReminder], now: datetime | None = None) -> str:
    if not reminders:
        return (
            "📋 *No Active Reminders* ⏰\n\n"
            "You have no pending reminders in this chat.\n"
            "To set one, use `/reminder <time> <topic>` or `@bot remind me <time>`!"
        )
    now_dt = now or datetime.now(timezone.utc)
    lines = ["📋 *Active Reminders in this Chat* ⏰\n"]
    for idx, r in enumerate(reminders, 1):
        diff = r.scheduled_time - now_dt
        secs = max(0, int(diff.total_seconds()))
        if secs < 3600:
            m = max(1, round(secs / 60))
            rel = f"in {m}m"
        else:
            rel = f"in {secs // 3600}h {(secs % 3600) // 60}m"
        time_str = r.scheduled_time.strftime("%H:%M UTC")
        short_id = r.id[:8]
        lines.append(
            f"{idx}. • *Topic:* {r.topic}\n"
            f"   • *When:* {rel} (at {time_str})\n"
            f"   • *ID:* `{short_id}`"
        )
    lines.append("\n_To cancel a reminder, send `/reminder cancel <ID>`_")
    return "\n".join(lines)


def detect_manual_reminder(
    text: str, sender_jid: str, base_time: datetime | None = None
) -> ManualReminder | None:
    if not text or not _MANUAL_REMINDER_RE.search(text):
        return None
    sched_time = parse_meeting_time(text, base_time)
    if sched_time is None:
        return None
    topic = extract_reminder_topic(text)
    user_digits = "".join(ch for ch in sender_jid.split("@")[0] if ch.isdigit())
    target_tag = f"@{user_digits}" if user_digits else "everyone"
    return ManualReminder(
        scheduled_time=sched_time,
        topic=topic,
        sender_jid=sender_jid,
        target_tag=target_tag,
    )


def format_manual_reminder_confirmation(
    reminder: ManualReminder, now: datetime | None = None
) -> str:
    now_dt = now or datetime.now(timezone.utc)
    diff = reminder.scheduled_time - now_dt
    total_seconds = max(0, int(diff.total_seconds()))
    if total_seconds < 3600:
        mins = max(1, round(total_seconds / 60))
        rel_str = f"in {mins} minute{'s' if mins != 1 else ''}"
    else:
        hours = total_seconds // 3600
        mins = (total_seconds % 3600) // 60
        rel_str = f"in {hours}h {mins}m"
    time_str = reminder.scheduled_time.strftime("%H:%M UTC")
    return (
        f"⏰ *Reminder Set!* 🎯\n\n"
        f"• *Topic:* {reminder.topic}\n"
        f"• *When:* {rel_str} (at {time_str})\n"
        f"• *For:* {reminder.target_tag}\n\n"
        f"I will send a reminder here when the time arrives! 🔔"
    )


def parse_meeting_time(text: str, base_time: datetime | None = None) -> datetime | None:
    """Parse scheduled meeting time from message text.

    Supports:
    - Relative shorthand: '10m', '30mins', '1h', '2h', '1h30m', '45 min'
    - Relative phrases: 'in 30 mins', 'for the next 10 mins', 'in 1 hour', 'after 20m'
    - Clock times: 'at 3:00 PM', 'at 15:30', '5pm', '5:30pm', 'today at 4pm', 'tomorrow at 10am'
    """
    if not text:
        return None
    now = base_time or datetime.now(timezone.utc)
    t_lower = text.lower()

    # 1. Compound offset: e.g. "1h 30m", "1 hour 30 mins", "in 2 hrs 15 mins"
    compound_m = re.search(
        r"(?:\b(?:in|for|after)\s+(?:the\s+next\s+)?)?(\d+)\s*(?:h|hrs?|hours?)\s*(?:and\s*)?(\d+)\s*(?:m|mins?|minutes?)\b",
        t_lower,
    )
    if compound_m:
        h = int(compound_m.group(1))
        m = int(compound_m.group(2))
        return now + timedelta(hours=h, minutes=m)

    # 2. Relative offset with unit: "in/for/after X minutes/mins/hours/hrs/secs" or shorthand "10m", "2h", "15min"
    rel_m = re.search(
        r"(?:\b(?:in|for|after)\s+(?:the\s+next\s+)?)?(\d+)\s*(mins?|minutes?|hours?|hrs?|seconds?|secs?|m|h)\b",
        t_lower,
    )
    if rel_m:
        val = int(rel_m.group(1))
        unit = rel_m.group(2)
        if "sec" in unit:
            return now + timedelta(seconds=val)
        if "h" in unit:
            return now + timedelta(hours=val)
        return now + timedelta(minutes=val)

    # 3. Date offset (today vs tomorrow vs weekday)
    day_offset = 0
    if "tomorrow" in t_lower:
        day_offset = 1

    # 4. Explicit time: e.g. "at 3:30 pm", "at 4pm", "at 14:00", "3:00pm", "10am", "16:45"
    time_m = re.search(
        r"(?:at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?|\b(\d{1,2}):(\d{2})\s*(am|pm)?\b|\b(\d{1,2})\s*(am|pm)\b)",
        t_lower,
    )
    if time_m:
        if time_m.group(1) is not None:
            raw_hour = int(time_m.group(1))
            raw_min = int(time_m.group(2)) if time_m.group(2) else 0
            meridiem = time_m.group(3)
        elif time_m.group(4) is not None:
            raw_hour = int(time_m.group(4))
            raw_min = int(time_m.group(5))
            meridiem = time_m.group(6)
        else:
            raw_hour = int(time_m.group(7))
            raw_min = 0
            meridiem = time_m.group(8)

        if meridiem == "pm" and raw_hour < 12:
            raw_hour += 12
        elif meridiem == "am" and raw_hour == 12:
            raw_hour = 0
        elif not meridiem and raw_hour < 7:
            # Assume afternoon if small number without am/pm (e.g. "at 3" -> 15:00)
            raw_hour += 12

        if 0 <= raw_hour <= 23 and 0 <= raw_min <= 59:
            target_date = (now + timedelta(days=day_offset)).date()
            candidate = datetime(
                target_date.year,
                target_date.month,
                target_date.day,
                raw_hour,
                raw_min,
                tzinfo=timezone.utc,
            )
            # If time today has already passed and no day specified, assume tomorrow
            if day_offset == 0 and candidate < now:
                candidate += timedelta(days=1)
            return candidate

    return None


def extract_meeting_details(
    text: str, base_time: datetime | None = None
) -> ExtractedMeeting | None:
    """Extract meeting link, platform, topic, and scheduled time from message."""
    link_info = detect_meeting_link(text)
    if not link_info:
        return None
    link, platform = link_info

    # Clean text to extract topic
    clean = text.replace(link, "").strip()
    topic_lines = [line.strip() for line in clean.splitlines() if line.strip()]
    topic = topic_lines[0] if topic_lines else f"{platform} Discussion"
    # Limit topic length
    if len(topic) > 80:
        topic = topic[:77] + "..."

    scheduled_time = parse_meeting_time(text, base_time)
    if scheduled_time is None:
        return ExtractedMeeting(
            meeting_link=link,
            meeting_type=platform,
            topic=topic,
            scheduled_time=None,
            needs_admin_confirmation=True,
            confirmation_reason="No verifiable date or time found in the message.",
        )

    return ExtractedMeeting(
        meeting_link=link,
        meeting_type=platform,
        topic=topic,
        scheduled_time=scheduled_time,
        needs_admin_confirmation=False,
    )


# ---------------------------------------------------------------------------
# ReminderStore (SQLite-backed persistent store)
# ---------------------------------------------------------------------------

class ReminderStore:
    """Persistent storage for scheduled community meetings and reminders."""

    def __init__(self, db_path: Path | str = "data/reminders.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meetings (
                    id TEXT PRIMARY KEY,
                    chat_jid TEXT NOT NULL,
                    sender_jid TEXT NOT NULL,
                    meeting_link TEXT NOT NULL,
                    meeting_type TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    scheduled_time_iso TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reminded_30m INTEGER NOT NULL DEFAULT 0,
                    reminded_start INTEGER NOT NULL DEFAULT 0,
                    created_at_iso TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def add_meeting(
        self,
        *,
        chat_jid: str,
        sender_jid: str,
        meeting_link: str,
        meeting_type: str,
        topic: str,
        scheduled_time: datetime,
        status: str = "scheduled",
    ) -> MeetingReminder:
        mid = uuid.uuid4().hex[:12]
        now_iso = datetime.now(timezone.utc).isoformat()
        sched_iso = scheduled_time.isoformat()
        reminder = MeetingReminder(
            id=mid,
            chat_jid=chat_jid,
            sender_jid=sender_jid,
            meeting_link=meeting_link,
            meeting_type=meeting_type,
            topic=topic,
            scheduled_time_iso=sched_iso,
            status=status,
            reminded_30m=False,
            reminded_start=False,
            created_at_iso=now_iso,
        )
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO meetings (
                    id, chat_jid, sender_jid, meeting_link, meeting_type,
                    topic, scheduled_time_iso, status, reminded_30m, reminded_start, created_at_iso
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reminder.id,
                    reminder.chat_jid,
                    reminder.sender_jid,
                    reminder.meeting_link,
                    reminder.meeting_type,
                    reminder.topic,
                    reminder.scheduled_time_iso,
                    reminder.status,
                    1 if reminder.reminded_30m else 0,
                    1 if reminder.reminded_start else 0,
                    reminder.created_at_iso,
                ),
            )
            conn.commit()
        logger.info(
            "event=meeting_scheduled id=%s chat=%s time=%s type=%s",
            mid,
            chat_jid,
            sched_iso,
            meeting_type,
        )
        return reminder

    def get_due_30m(self, now: datetime) -> list[MeetingReminder]:
        """Fetch meetings scheduled within 30 minutes that haven't received notice."""
        threshold = now + timedelta(minutes=30)
        now_iso = now.isoformat()
        thresh_iso = threshold.isoformat()
        with self._get_conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM meetings
                WHERE status = 'scheduled'
                  AND meeting_type != 'reminder'
                  AND reminded_30m = 0
                  AND scheduled_time_iso <= ?
                  AND scheduled_time_iso > ?
                """,
                (thresh_iso, now_iso),
            ).fetchall()
            return [self._row_to_model(r) for r in rows]

    def get_due_start(self, now: datetime) -> list[MeetingReminder]:
        """Fetch meetings starting now that haven't received start notice."""
        now_iso = now.isoformat()
        # Keep window to within 1 hour after start to prevent stale alerts on startup
        past_window = (now - timedelta(hours=1)).isoformat()
        with self._get_conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM meetings
                WHERE status = 'scheduled'
                  AND reminded_start = 0
                  AND scheduled_time_iso <= ?
                  AND scheduled_time_iso >= ?
                """,
                (now_iso, past_window),
            ).fetchall()
            return [self._row_to_model(r) for r in rows]

    def mark_30m_sent(self, meeting_id: str) -> None:
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE meetings SET reminded_30m = 1 WHERE id = ?",
                (meeting_id,),
            )
            conn.commit()

    def mark_start_sent(self, meeting_id: str) -> None:
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE meetings SET reminded_start = 1, status = 'completed' WHERE id = ?",
                (meeting_id,),
            )
            conn.commit()

    def cancel_meeting(self, meeting_id: str) -> None:
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE meetings SET status = 'cancelled' WHERE id = ?",
                (meeting_id,),
            )
            conn.commit()

    def cancel_by_link(self, chat_jid: str, link: str) -> int:
        with self._get_conn() as conn:
            cursor = conn.execute(
                "UPDATE meetings SET status = 'cancelled' WHERE chat_jid = ? AND meeting_link = ? AND status = 'scheduled'",
                (chat_jid, link),
            )
            conn.commit()
            return cursor.rowcount

    def cancel_by_query(self, chat_jid: str, query: str) -> MeetingReminder | None:
        """Cancel a scheduled reminder in chat_jid by matching ID prefix or topic keyword."""
        q = query.strip().lower()
        active = self.get_active_meetings(chat_jid)
        for m in active:
            if (
                m.id.lower().startswith(q)
                or q in m.topic.lower()
                or m.id.replace("-", "")[:8].lower() == q
            ):
                self.cancel_meeting(m.id)
                return m
        return None

    def get_active_meetings(self, chat_jid: str | None = None) -> list[MeetingReminder]:
        with self._get_conn() as conn:
            if chat_jid:
                rows = conn.execute(
                    "SELECT * FROM meetings WHERE chat_jid = ? AND status = 'scheduled' ORDER BY scheduled_time_iso ASC",
                    (chat_jid,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM meetings WHERE status = 'scheduled' ORDER BY scheduled_time_iso ASC"
                ).fetchall()
            return [self._row_to_model(r) for r in rows]

    def _row_to_model(self, row: sqlite3.Row) -> MeetingReminder:
        return MeetingReminder(
            id=row["id"],
            chat_jid=row["chat_jid"],
            sender_jid=row["sender_jid"],
            meeting_link=row["meeting_link"],
            meeting_type=row["meeting_type"],
            topic=row["topic"],
            scheduled_time_iso=row["scheduled_time_iso"],
            status=row["status"],
            reminded_30m=bool(row["reminded_30m"]),
            reminded_start=bool(row["reminded_start"]),
            created_at_iso=row["created_at_iso"],
        )


# ---------------------------------------------------------------------------
# Reminder message formatters
# ---------------------------------------------------------------------------

def format_30m_reminder(meeting: MeetingReminder) -> str:
    """Format the 30-minute advance notice message."""
    return (
        f"⏰ *Upcoming Meeting Notice (in 30 mins)*\n\n"
        f"📌 *Topic:* {meeting.topic}\n"
        f"🔗 *Meeting Link:* {meeting.meeting_link}\n\n"
        f"See you there! 👋"
    )


def format_start_reminder(meeting: MeetingReminder) -> str:
    """Format the meeting start notice message."""
    if meeting.meeting_type == "reminder":
        user_phone = "".join(ch for ch in meeting.sender_jid.split("@")[0] if ch.isdigit())
        user_tag = f"@{user_phone}" if user_phone else "everyone"
        return (
            f"⏰ *Reminder for {user_tag}*\n\n"
            f"📌 *Topic:* {meeting.topic}\n\n"
            f"This is your scheduled reminder! 🔔🎯"
        )
    return (
        f"🚀 *Meeting Starting Now*\n\n"
        f"📌 *Topic:* {meeting.topic}\n"
        f"🔗 *Meeting Link:* {meeting.meeting_link}\n\n"
        f"The meeting is starting now — click the link above to join! 🎯"
    )


def format_unverified_meeting_alert(
    *,
    chat_jid: str,
    sender_jid: str,
    link: str,
    text: str,
    reason: str,
) -> str:
    """Format alert for admin when meeting material is ambiguous or opposed."""
    sender_phone = "".join(ch for ch in sender_jid.split("@")[0] if ch.isdigit()) or sender_jid
    return (
        f"⚠️ *Meeting Verification Required*\n\n"
        f"A meeting link was shared but could not be scheduled with certainty:\n"
        f"• *Group:* {chat_jid}\n"
        f"• *Sender:* @{sender_phone}\n"
        f"• *Meeting Link:* {link}\n"
        f"• *Reason:* {reason}\n\n"
        f"📝 *Original Message:*\n\"{text}\""
    )
