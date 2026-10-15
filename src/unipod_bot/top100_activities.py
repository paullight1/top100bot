"""Read and rank activities exposed by Top100's public events API."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

logger = logging.getLogger("unipod_bot")

DEFAULT_BASE_URL = "https://www.top100afl.com"
EVENTS_URL = f"{DEFAULT_BASE_URL}/events"
_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_STOP_WORDS = {
    "a", "about", "an", "and", "are", "at", "be", "by", "can", "date",
    "does", "for", "from", "how", "i", "in", "is", "it", "me", "of", "on",
    "or", "our", "the", "this", "to", "we", "what", "when", "where", "which",
    "who", "will", "with", "you",
}
_ACTIVITY_WORDS = {
    "activity", "activities", "afl", "bootcamp", "event", "events", "forum",
    "hackathon", "masterclass", "programme", "program", "registration", "register",
    "session", "sessions", "summit", "training", "workshop",
}
_FOLLOW_UP_RE = re.compile(r"\b(it|that|this|they|them|there|those|when does it|where is it)\b", re.I)
_URL_SCHEMES = {"http", "https"}


@dataclass(frozen=True)
class Top100Activity:
    id: str
    slug: str
    title: str
    summary: str
    description: str
    start_at: datetime | None
    end_at: datetime | None
    timezone: str
    location: str
    registration_url: str | None
    activity_url: str
    tags: tuple[str, ...]
    programme_label: str | None
    session_number: int | None


def is_activity_question(question: str, history: str = "") -> bool:
    """Identify activity questions, including short follow-ups anchored by history."""
    question_tokens = set(_tokens(question))
    if question_tokens & _ACTIVITY_WORDS:
        return True
    if _FOLLOW_UP_RE.search(question):
        return bool(set(_tokens(history)) & _ACTIVITY_WORDS)
    return False


def _tokens(value: str) -> list[str]:
    return [token.lower() for token in _TOKEN_RE.findall(value or "") if token.lower() not in _STOP_WORDS]


def _field(row: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in row:
            value = row[name]
            if value is not None:
                return value
    return default


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _valid_url(value: Any) -> str | None:
    url = _text(value)
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in _URL_SCHEMES or not parsed.netloc:
        return None
    return url


def _parse_date(value: Any, timezone_name: str) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed
    try:
        zone = ZoneInfo(timezone_name or "Africa/Lagos")
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("Africa/Lagos")
    return parsed.replace(tzinfo=zone)


def _activity(row: Any) -> Top100Activity | None:
    if not isinstance(row, dict):
        return None
    if _text(_field(row, "status")).lower() != "published":
        return None
    if _text(_field(row, "visibility")).lower() != "public":
        return None

    title = _text(_field(row, "title"))
    slug = _text(_field(row, "slug"))
    activity_id = str(_field(row, "id", default=slug or title))
    if not title or not activity_id:
        return None

    timezone_name = _text(_field(row, "timezone")) or "Africa/Lagos"
    registration_url = _valid_url(_field(row, "registration_url", "registrationUrl"))
    raw_tags = _field(row, "tags", default=[])
    tags = tuple(tag.strip() for tag in raw_tags if isinstance(tag, str) and tag.strip()) if isinstance(raw_tags, list) else ()
    session = _field(row, "session_number", "sessionNumber")
    if not isinstance(session, int) or isinstance(session, bool):
        session = None

    return Top100Activity(
        id=activity_id,
        slug=slug,
        title=title,
        summary=_text(_field(row, "summary", "subtitle")),
        description=_text(_field(row, "description")),
        start_at=_parse_date(_field(row, "start_at", "startAt", "starts_at"), timezone_name),
        end_at=_parse_date(_field(row, "end_at", "endAt", "ends_at"), timezone_name),
        timezone=timezone_name,
        location=_text(_field(row, "location")) or ", ".join(
            part for part in (_text(_field(row, "city")), _text(_field(row, "country"))) if part
        ),
        registration_url=registration_url,
        activity_url=registration_url or EVENTS_URL,
        tags=tags,
        programme_label=_text(_field(row, "programme_label", "programmeLabel")) or None,
        session_number=session,
    )


class Top100ActivitiesClient:
    """Small fail-soft client for Top100's anonymous, public events endpoint."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout_seconds: float = 4.0,
        cache_seconds: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.strip().rstrip("/") or DEFAULT_BASE_URL
        self.timeout_seconds = max(0.1, min(float(timeout_seconds), 15.0))
        self.cache_seconds = max(0.0, float(cache_seconds))
        self._transport = transport
        self._cached_at = 0.0
        self._cached_rows: list[Any] | None = None

    async def _fetch_rows(self) -> list[Any]:
        if self._cached_rows is not None and time.monotonic() - self._cached_at < self.cache_seconds:
            return self._cached_rows
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_seconds),
                transport=self._transport,
            ) as client:
                response = await client.get(
                    f"{self.base_url}/api/events",
                    params={"audience": "awardees", "limit": "100"},
                )
                response.raise_for_status()
                payload = response.json()
            if not isinstance(payload, list):
                logger.warning("event=top100_api_invalid_payload")
                return []
            self._cached_rows = payload
            self._cached_at = time.monotonic()
            return payload
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            logger.warning("event=top100_api_unavailable error=%s", type(exc).__name__)
            return []

    async def fetch_relevant(self, question: str, history: str = "") -> list[Top100Activity]:
        if not is_activity_question(question, history):
            return []
        rows = await self._fetch_rows()
        query_tokens = set(_tokens(question))
        history_tokens = set(_tokens(history))
        ranked: list[tuple[int, int, Top100Activity]] = []
        for index, row in enumerate(rows):
            activity = _activity(row)
            if activity is None:
                continue
            searchable = " ".join((
                activity.title, activity.summary, activity.description,
                activity.location, " ".join(activity.tags), activity.programme_label or "",
            ))
            event_tokens = set(_tokens(searchable))
            direct_score = len(query_tokens & event_tokens)
            context_score = len((history_tokens - query_tokens) & event_tokens)
            score = direct_score * 3 + context_score
            if score:
                ranked.append((score, -index, activity))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [activity for _, _, activity in ranked[:5]]
