"""Summary skill module for the Top100 AFL WhatsApp assistant.

Provides the /summary skill to generate detailed community briefings for specific dates
and timeframes (e.g., specific dates like '24 sept', 'last 2 days', 'week', 'month', '2 weeks').
Also supports concise responses for open questions like 'what happened today', with a small
sentence pointing users to the /summary skill.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from unipod_bot.memory.graphiti_store import GraphitiStore

logger = logging.getLogger("unipod_bot")

# ---------------------------------------------------------------------------
# Triggers & regexes
# ---------------------------------------------------------------------------

_SUMMARY_TRIGGER_RE = re.compile(
    r"(?:^|\s)/(?:summary|summarize)\b",
    re.IGNORECASE,
)

_TODAY_QUESTION_RE = re.compile(
    r"\b("
    r"what('s|\s+has|\s+is)?\s+(happened|happening|been happening|going on|occurred)\s+today|"
    r"catch\s+(me\s+)?up\s+on\s+today|"
    r"what\s+did\s+i\s+miss\s+today|"
    r"summary\s+of\s+today|"
    r"today('s)?\s+(updates?|summary|highlights?)|"
    r"what('s|\s+is)\s+new\s+today"
    r")\b",
    re.IGNORECASE,
)

_MONTH_MAP = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

_ORDINAL_SUFFIX_RE = re.compile(r"(\d+)(?:st|nd|rd|th)\b", re.IGNORECASE)


def is_summary_request(text: str) -> bool:
    """Return True if the text invokes the /summary command or skill."""
    if not text:
        return False
    return bool(_SUMMARY_TRIGGER_RE.search(text))


def is_today_question(text: str) -> bool:
    """Return True if the text is an open conversational question about today's activity."""
    if not text:
        return False
    # If explicit /summary is used, that takes precedence as a detailed summary skill call
    if is_summary_request(text):
        return False
    return bool(_TODAY_QUESTION_RE.search(text))


@dataclass(frozen=True)
class SummaryWindow:
    start: datetime
    end: datetime
    label: str
    is_future: bool = False


def parse_summary_time_range(
    raw_query: str,
    base_time: datetime | None = None,
) -> SummaryWindow | None:
    """Parse natural language date/time specifications into a UTC SummaryWindow.

    Supported queries:
    - 'today'
    - 'yesterday'
    - 'last 2 days', '2 days', '3 days', 'last 3 days', 'N days'
    - 'week', 'this week', 'last week', 'past week'
    - '2 weeks', 'last 2 weeks', 'past 2 weeks'
    - 'month', 'this month', 'last month', 'past month'
    - '24 sept', '24 september', '24th sept 2026', 'sept 24'
    - '2026-09-24', '24/09', '24/09/2026', '24-09'
    """
    if base_time is None:
        base_time = datetime.now(timezone.utc)
    elif base_time.tzinfo is None:
        base_time = base_time.replace(tzinfo=timezone.utc)

    # Strip command prefix and mentions
    cleaned = _SUMMARY_TRIGGER_RE.sub("", raw_query).strip()
    cleaned = re.sub(r"(?:/voice|/?voicemode|/?voice_mode)\b", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"@\S+", "", cleaned).strip()
    # Normalize spaces
    cleaned = " ".join(cleaned.split()).lower()

    if not cleaned or cleaned in ("help", "?", "skills", "skill"):
        return None

    # 1. 'today'
    if cleaned in ("today", "for today", "today's"):
        start = base_time.replace(hour=0, minute=0, second=0, microsecond=0)
        end = base_time
        return SummaryWindow(start=start, end=end, label=f"today ({base_time.strftime('%d %b %Y')})")

    # 2. 'yesterday'
    if cleaned in ("yesterday", "for yesterday", "yesterday's"):
        y = base_time - timedelta(days=1)
        start = y.replace(hour=0, minute=0, second=0, microsecond=0)
        end = y.replace(hour=23, minute=59, second=59, microsecond=999999)
        return SummaryWindow(start=start, end=end, label=f"yesterday ({y.strftime('%d %b %Y')})")

    # 3. Relative weeks
    m_weeks = re.search(r"\b(?:last|past)?\s*(\d+)\s*weeks?\b", cleaned)
    if m_weeks:
        num_weeks = int(m_weeks.group(1))
        start = (base_time - timedelta(days=7 * num_weeks)).replace(hour=0, minute=0, second=0, microsecond=0)
        label = f"the last {num_weeks} weeks" if num_weeks > 1 else "the past week"
        return SummaryWindow(start=start, end=base_time, label=label)

    if cleaned in ("this week", "week", "past week", "last week"):
        if cleaned == "last week":
            # Monday of previous week to Sunday of previous week
            days_since_monday = base_time.weekday()
            last_sunday = base_time - timedelta(days=days_since_monday + 1)
            last_monday = last_sunday - timedelta(days=6)
            start = last_monday.replace(hour=0, minute=0, second=0, microsecond=0)
            end = last_sunday.replace(hour=23, minute=59, second=59, microsecond=999999)
            return SummaryWindow(start=start, end=end, label="last week")
        # Default 'week' or 'this week' -> last 7 days up to now
        start = (base_time - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
        return SummaryWindow(start=start, end=base_time, label="this week")

    # 4. Relative months
    m_months = re.search(r"\b(?:last|past)?\s*(\d+)\s*months?\b", cleaned)
    if m_months:
        num_months = int(m_months.group(1))
        start = (base_time - timedelta(days=30 * num_months)).replace(hour=0, minute=0, second=0, microsecond=0)
        label = f"the last {num_months} months" if num_months > 1 else "the past month"
        return SummaryWindow(start=start, end=base_time, label=label)

    if cleaned in ("this month", "month", "past month", "last month"):
        if cleaned == "this month":
            start = base_time.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            return SummaryWindow(start=start, end=base_time, label=f"this month ({base_time.strftime('%B %Y')})")
        # 'month' / 'last month' -> last 30 days
        start = (base_time - timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0)
        return SummaryWindow(start=start, end=base_time, label="the past month")

    # 5. Relative days (e.g. 'last 2 days', '3 days', 'last 3 days')
    m_days = re.search(r"\b(?:last|past)?\s*(\d+)\s*days?\b", cleaned)
    if m_days:
        num_days = int(m_days.group(1))
        start = (base_time - timedelta(days=num_days)).replace(hour=0, minute=0, second=0, microsecond=0)
        label = f"the last {num_days} days"
        return SummaryWindow(start=start, end=base_time, label=label)

    # 6. ISO format YYYY-MM-DD
    m_iso = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", cleaned)
    if m_iso:
        year, month, day = int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))
        try:
            target = datetime(year, month, day, tzinfo=timezone.utc)
            start = target.replace(hour=0, minute=0, second=0, microsecond=0)
            end = target.replace(hour=23, minute=59, second=59, microsecond=999999)
            is_future = start > base_time
            return SummaryWindow(start=start, end=end, label=target.strftime("%d %b %Y"), is_future=is_future)
        except ValueError:
            return None

    # 7. Numeric DD/MM/YYYY or DD/MM or DD-MM
    m_num = re.search(r"\b(\d{1,2})[/\.-](\d{1,2})(?:[/\.-](\d{2,4}))?\b", cleaned)
    if m_num:
        day, month = int(m_num.group(1)), int(m_num.group(2))
        raw_year = m_num.group(3)
        if raw_year:
            year = int(raw_year)
            if year < 100:
                year += 2000
        else:
            year = base_time.year
        try:
            target = datetime(year, month, day, tzinfo=timezone.utc)
            start = target.replace(hour=0, minute=0, second=0, microsecond=0)
            end = target.replace(hour=23, minute=59, second=59, microsecond=999999)
            is_future = start > base_time
            return SummaryWindow(start=start, end=end, label=target.strftime("%d %b %Y"), is_future=is_future)
        except ValueError:
            return None

    # 8. Text date: '24 sept', '24th september', 'sept 24', 'september 24 2026'
    cleaned_ord = _ORDINAL_SUFFIX_RE.sub(r"\1", cleaned)
    month_names_pattern = "|".join(_MONTH_MAP.keys())

    # Pattern A: '24 september [2026]'
    m_text_a = re.search(
        rf"\b(\d{{1,2}})\s+({month_names_pattern})(?:\s+(\d{{4}}))?\b",
        cleaned_ord,
    )
    if m_text_a:
        day = int(m_text_a.group(1))
        month = _MONTH_MAP[m_text_a.group(2)]
        year = int(m_text_a.group(3)) if m_text_a.group(3) else base_time.year
        try:
            target = datetime(year, month, day, tzinfo=timezone.utc)
            start = target.replace(hour=0, minute=0, second=0, microsecond=0)
            end = target.replace(hour=23, minute=59, second=59, microsecond=999999)
            is_future = start > base_time
            return SummaryWindow(start=start, end=end, label=target.strftime("%d %b %Y"), is_future=is_future)
        except ValueError:
            return None

    # Pattern B: 'september 24 [2026]'
    m_text_b = re.search(
        rf"\b({month_names_pattern})\s+(\d{{1,2}})(?:\s+(\d{{4}}))?\b",
        cleaned_ord,
    )
    if m_text_b:
        month = _MONTH_MAP[m_text_b.group(1)]
        day = int(m_text_b.group(2))
        year = int(m_text_b.group(3)) if m_text_b.group(3) else base_time.year
        try:
            target = datetime(year, month, day, tzinfo=timezone.utc)
            start = target.replace(hour=0, minute=0, second=0, microsecond=0)
            end = target.replace(hour=23, minute=59, second=59, microsecond=999999)
            is_future = start > base_time
            return SummaryWindow(start=start, end=end, label=target.strftime("%d %b %Y"), is_future=is_future)
        except ValueError:
            return None

    return None


# ---------------------------------------------------------------------------
# Persistent message store
# ---------------------------------------------------------------------------

class SummaryStore:
    """Persistent SQLite store for community messages to power accurate date-range summaries."""

    def __init__(self, db_path: Path | str = "data/messages.db") -> None:
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
                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    chat_jid TEXT NOT NULL,
                    sender_jid TEXT NOT NULL,
                    text TEXT NOT NULL,
                    iso_time TEXT NOT NULL,
                    is_group INTEGER NOT NULL DEFAULT 1,
                    created_at_epoch REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_iso ON messages(iso_time)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_jid)"
            )
            conn.commit()

    def add_message(
        self,
        *,
        message_id: str,
        chat_jid: str,
        sender_jid: str,
        text: str,
        iso_time: str,
        is_group: bool = True,
    ) -> None:
        body = (text or "").strip()
        mid = (message_id or "").strip()
        if not body or not mid:
            return
        now_epoch = time.time()
        try:
            with self._get_conn() as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO messages
                    (message_id, chat_jid, sender_jid, text, iso_time, is_group, created_at_epoch)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (mid, chat_jid, sender_jid, body, iso_time, 1 if is_group else 0, now_epoch),
                )
                conn.commit()
        except Exception:
            logger.exception("event=summary_store_add_failed id=%s", mid)

    def get_messages_in_range(
        self,
        start_time: datetime,
        end_time: datetime,
        chat_jid: str | None = None,
        only_groups: bool = True,
    ) -> list[dict[str, Any]]:
        """Retrieve stored messages whose iso_time falls between start_time and end_time."""
        start_iso = start_time.isoformat()
        end_iso = end_time.isoformat()
        query = (
            "SELECT message_id, chat_jid, sender_jid, text, iso_time, is_group "
            "FROM messages "
            "WHERE iso_time >= ? AND iso_time <= ? "
        )
        params: list[Any] = [start_iso, end_iso]
        if chat_jid:
            query += "AND chat_jid = ? "
            params.append(chat_jid)
        if only_groups:
            query += "AND is_group = 1 "
        query += "ORDER BY iso_time ASC"

        try:
            with self._get_conn() as conn:
                rows = conn.execute(query, params).fetchall()
                return [dict(r) for r in rows]
        except Exception:
            logger.exception("event=summary_store_query_failed")
            return []


def format_messages_for_prompt(messages: list[dict[str, Any]]) -> str:
    """Format a list of message dicts into chronological lines for LLM summarization."""
    lines: list[str] = []
    for m in messages:
        sender = m.get("sender_jid", "unknown")
        phone = "".join(ch for ch in sender.split("@")[0] if ch.isdigit())
        handle = f"@{phone}" if phone else sender
        iso = m.get("iso_time", "")
        # Short time display
        time_part = iso[11:16] if len(iso) >= 16 else ""
        text = m.get("text", "").strip()
        if time_part:
            lines.append(f"[{time_part}] {handle}: {text}")
        else:
            lines.append(f"{handle}: {text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

SKILL_RECOMMENDATION_TIP = (
    "💡 *Tip:* For an in-depth, structured report, you can use the */summary* skill "
    "(e.g., */summary today*, */summary 24 sept*, or */summary last 2 days*)."
)

DETAILED_SUMMARY_PROMPT = """You are the Top100 AFL WhatsApp assistant. Generate a highly detailed, professional, and well-structured briefing of community activity for {label}.

Community Messages & Discussions:
{messages_text}

Instructions:
1. Provide a comprehensive, detailed breakdown of the discussions and happenings.
2. Group the briefing into clear, well-formatted sections with relevant emojis:
   - 📋 *Executive Overview*: High-level synthesis of primary topics, discussions, and group pulse.
   - 🚀 *Events, Hackathons & Deadlines*: Specific hackathons, bootcamps, workshops, dates, locations, and deadlines mentioned.
   - 🤝 *Collaboration & Roles*: Team formations, skill requests, co-founder searches, and project collaborations.
   - 📚 *Shared Resources & Links*: Documentation, repositories, guides, or files shared.
   - 📢 *Official Notices & Updates*: Announcements from organizers, admins, or lab leads.
3. Omit any section that has no relevant activity.
4. Formatting rules:
   - Use numbered lists strictly for chronological or sequential steps.
   - Use bullet points ('• ') for lists and collections.
   - Use hyphens ('-') only for inline separators (e.g. '• Hackathon - Oct 15 - Lab 3') or time ranges.
   - Attribute key updates or resources to the member handles (@254...) who posted them.
5. Tone: Warm, accommodating, and eloquent. Factual and grounded strictly in the messages provided without hallucinations.

Provide the detailed summary:"""

CONCISE_SUMMARY_PROMPT = """You are the Top100 AFL WhatsApp assistant. Provide a concise, clear summary of what happened today in the Top100 AFL community.

Community Messages Today:
{messages_text}

Instructions:
1. Keep the summary concise: 1–2 short paragraphs or 3–5 bullet points highlighting the main discussions, events, or announcements.
2. Do not use generic filler like "Here is what happened today".
3. Formatting rules:
   - Use bullet points ('• ') for collections.
   - Use hyphens ('-') only for inline separators or time ranges.
4. Tone: Warm, factual, and direct to the point.

Provide the short summary:"""

SUMMARY_HELP_CARD = (
    "📋 *Top100 AFL Summary Skill*\n\n"
    "Get a detailed briefing of community discussions, hackathons, team formations, "
    "and resources for any date or timeframe.\n\n"
    "*How to use:*\n"
    "• `/summary today` - Full detailed report of today's discussions\n"
    "• `/summary yesterday` - Detailed briefing for yesterday\n"
    "• `/summary 24 sept` - Activity for a specific date (e.g. `24 sept`, `15 oct`)\n"
    "• `/summary last 2 days` (or `3 days`) - Multi-day community report\n"
    "• `/summary week` (or `last 2 weeks`) - Weekly overview\n"
    "• `/summary month` - Monthly compilation\n\n"
    "💡 _Tip: You can add `/voice` (e.g. `/summary today /voice`) to receive the report as a spoken voice note skill._"
)
