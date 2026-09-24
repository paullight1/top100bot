from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Protocol


# ---------------------------------------------------------------------------
# Team-seeking intent detection
# ---------------------------------------------------------------------------

_TEAM_SEEKING_RE = re.compile(
    r"\b("
    r"looking for (?:a )?team(?:mates?|s)?|"
    r"need (?:a )?team(?:mates?|s)?|"
    r"want (?:to join|a team|teammates?)|want teammates?|"
    r"anyone (?:want|need|looking) (?:to|for) (?:join|team|teammates?)|"
    r"looking for (?:co-?founder|collaborator|partner)s?|"
    r"seeking (?:team|teammates?|partner|collaborator)s?|"
    r"join (?:my|a|our) team|"
    r"form(?:ing)? (?:a )?team|"
    r"open (?:to|for) (?:joining|collaboration)|"
    r"join (?:the )?hackathon (?:team|group)"
    r")\b",
    re.IGNORECASE,
)

_EVENT_RE = re.compile(
    r"\b("
    r"hackathons?|workshops?|bootcamps?|meetups?|webinars?|"
    r"demo day|pitch (?:competition|session|event)?|challenges?|"
    r"orientations?|conferences?|summits?|info session|"
    r"ideation session"
    r")\b",
    re.IGNORECASE,
)

_DEADLINE_RE = re.compile(
    r"\b("
    r"deadlines?|due (?:date|on|by)|submit (?:before|by|until)|"
    r"submissions? (?:closes?|deadline|due|ends?)|"
    r"registration (?:closes?|ends?|deadline)|"
    r"apply (?:before|by|until)|last day (?:to|for)|"
    r"closes? on|by (?:11:59|midnight|\d{1,2}(?::\d{2})?\s*(?:am|pm))"
    r")\b",
    re.IGNORECASE,
)

_RESOURCE_RE = re.compile(
    r"\b("
    r"github\.com|drive\.google\.com|docs\.google\.com|figma\.com|"
    r"notion\.site|dropbox\.com|guidelines?|rubrics?|templates?|"
    r"slides?|repo|portal|form|pdf"
    r")\b",
    re.IGNORECASE,
)

_ANNOUNCEMENT_RE = re.compile(
    r"\b("
    r"announcements?|official notice|psa|important notice|"
    r"attention all|urgent notice|official update|rules? update"
    r")\b",
    re.IGNORECASE,
)


def classify_message_content(
    text: str,
    urls: tuple[str, ...] | list[str] = (),
    has_document: bool = False,
) -> list[str]:
    """Classify message text into semantic categories for indexing and retrieval."""
    if not text and not urls and not has_document:
        return []
    cats: list[str] = []
    if _EVENT_RE.search(text):
        cats.append("event")
    if _DEADLINE_RE.search(text):
        cats.append("deadline")
    if _TEAM_SEEKING_RE.search(text):
        cats.append("team-seeking")
    if urls or has_document or _RESOURCE_RE.search(text):
        cats.append("resource")
    if _ANNOUNCEMENT_RE.search(text):
        cats.append("announcement")
    return cats


def detect_categories_tag(
    text: str,
    urls: tuple[str, ...] | list[str] = (),
    has_document: bool = False,
) -> str:
    """Format category annotations for Graphiti episodes."""
    cats = [c for c in classify_message_content(text, urls, has_document) if c != "team-seeking"]
    if cats:
        return f" [categories: {', '.join(cats)}]"
    return ""


def _detect_team_intent(text: str) -> str:
    """Return a [team-seeking] annotation if the message expresses team-seeking intent.

    The tag is appended to the episode body so Graphiti indexes the intent
    as a searchable fact linked to the sender.
    """
    if not text:
        return ""
    if _TEAM_SEEKING_RE.search(text):
        return " [team-seeking]"
    return ""


class EpisodeSource(Protocol):
    chat_jid: str
    sender_jid: str
    message_id: str
    text: str
    iso_time: str


def parse_iso_time(iso_time: str) -> datetime:
    raw = (iso_time or "").strip()
    if not raw:
        return datetime.now(timezone.utc)
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def episode_body(msg: EpisodeSource) -> str:
    """Graphiti EpisodeType.message format: 'actor: content'.

    Appends extracted URLs and team-seeking intent tags when present,
    so Graphiti can index and retrieve them for collaboration matching.
    """
    body = f"{msg.sender_jid}: {msg.text}"
    urls = getattr(msg, "urls", None)
    if urls:
        body += f" [links: {', '.join(urls)}]"
    body += _detect_team_intent(msg.text)
    return body


def episode_source_description(msg: EpisodeSource) -> str:
    kind = "whatsapp_dm" if getattr(msg, "is_group", True) is False else "whatsapp_group"
    urls = getattr(msg, "urls", None)
    url_count = len(urls) if urls else 0
    cats = classify_message_content(getattr(msg, "text", "") or "", urls or ())
    cat_str = f" categories={','.join(cats)}" if cats else ""
    return (
        f"{kind} source_jid={msg.chat_jid} sender={msg.sender_jid} "
        f"message_id={msg.message_id} timestamp={msg.iso_time} urls={url_count}{cat_str}"
    )


def reply_episode_body(msg: EpisodeSource, reply: str) -> str:
    return f"bot: {reply}"


def reply_source_description(msg: EpisodeSource) -> str:
    return (
        f"whatsapp_bot_reply source_jid={msg.chat_jid} in_reply_to={msg.message_id} "
        f"timestamp={msg.iso_time} question={msg.text}"
    )


def episode_name(message_id: str) -> str:
    return (message_id or "").strip()


def reply_episode_name(message_id: str) -> str:
    mid = episode_name(message_id)
    return f"bot-{mid}" if mid else "bot-unknown"


def pdf_episode_body(sender_jid: str, filename: str, text_chunk: str, chunk_index: int = 0) -> str:
    """Format PDF text as a Graphiti episode body."""
    prefix = f"[PDF: {filename}] [category: resource]" if filename else "[PDF] [category: resource]"
    chunk_label = f" (part {chunk_index + 1})" if chunk_index > 0 else ""
    return f"{sender_jid}: {prefix}{chunk_label} {text_chunk}"


def pdf_episode_name(message_id: str, chunk_index: int = 0) -> str:
    mid = episode_name(message_id)
    if chunk_index == 0:
        return f"pdf-{mid}" if mid else "pdf-unknown"
    return f"pdf-{mid}-{chunk_index}" if mid else f"pdf-unknown-{chunk_index}"


def pdf_source_description(
    chat_jid: str, sender_jid: str, message_id: str, filename: str
) -> str:
    return (
        f"whatsapp_pdf source_jid={chat_jid} sender={sender_jid} "
        f"message_id={message_id} filename={filename} categories=resource"
    )


# --- Batched ingestion helpers ---

def batched_episode_body(messages: "list") -> str:
    """Combine multiple buffered messages into a single episode body.

    Each message becomes a line in Graphiti's ``EpisodeType.message`` format:
    ``actor: content``.  URLs, team-seeking, and category tags are appended
    per-message when present.
    """
    lines: list[str] = []
    for msg in messages:
        line = f"{msg.sender_jid}: {msg.text}"
        urls = getattr(msg, "urls", None)
        if urls:
            line += f" [links: {', '.join(urls)}]"
        line += _detect_team_intent(msg.text)
        cat_tag = detect_categories_tag(msg.text, urls or ())
        if cat_tag:
            line += cat_tag
        lines.append(line)
    return "\n".join(lines)


def batched_episode_name(first_message_id: str, count: int) -> str:
    """Episode name for a batch: ``batch-<first_id>-x<count>``."""
    mid = (first_message_id or "").strip()
    if not mid:
        return f"batch-unknown-x{count}"
    return f"batch-{mid}-x{count}"


def batched_source_description(messages: "list") -> str:
    """Source description that preserves provenance for all messages in a batch.

    Includes all message IDs and the set of source group JIDs.
    """
    msg_ids = [m.message_id for m in messages]
    group_jids = sorted(set(m.chat_jid for m in messages))
    senders = sorted(set(m.sender_jid for m in messages))
    all_cats: set[str] = set()
    for m in messages:
        all_cats.update(classify_message_content(getattr(m, "text", "") or "", getattr(m, "urls", ()) or ()))
    cats_str = f" categories={','.join(sorted(all_cats))}" if all_cats else ""
    first = messages[0] if messages else None
    ts_start = first.iso_time if first else ""
    ts_end = messages[-1].iso_time if messages else ""
    return (
        f"whatsapp_group_batch source_jids={','.join(group_jids)} "
        f"senders={','.join(senders)} "
        f"message_ids={','.join(msg_ids)} "
        f"count={len(messages)} "
        f"time_range={ts_start}..{ts_end}{cats_str}"
    )
