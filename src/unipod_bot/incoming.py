from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from neonize.utils.message import extract_text

from unipod_bot.admin import event_looks_like_admin, is_admin_jid
from unipod_bot.config import Settings, parse_group_jids
from unipod_bot.jids import is_group_jid, jid_str, jid_user


class SkipReason(str, Enum):
    FROM_ME = "from_me"
    EMPTY_DM = "empty_dm"
    GROUP_NOT_ALLOWED = "group_not_allowed"


@dataclass(frozen=True)
class IncomingGroupMessage:
    chat_jid: str
    sender_jid: str
    message_id: str
    text: str
    is_reply_group: bool
    iso_time: str
    mentioned_users: tuple[str, ...] = ()
    chat_obj: Any = None
    urls: tuple[str, ...] = ()
    quoted_text: str = ""
    quoted_sender: str = ""
    has_document: bool = False
    document_filename: str = ""
    document_mimetype: str = ""
    event_obj: Any = None  # raw neonize MessageEv for reply-to quoting
    is_voice_request: bool = False  # True when text starts with /voice
    push_name: str = ""


@dataclass(frozen=True)
class IncomingAdminDm:
    chat_jid: str
    sender_jid: str
    message_id: str
    text: str
    iso_time: str = ""
    chat_obj: Any = None
    event_obj: Any = None
    is_voice_request: bool = False
    push_name: str = ""
    has_document: bool = False
    document_filename: str = ""
    document_mimetype: str = ""


@dataclass(frozen=True)
class IncomingKnowledgeMessage(IncomingGroupMessage):
    """Authorized feed submission; never a member conversation message."""


@dataclass(frozen=True)
class IncomingUserDm:
    chat_jid: str
    sender_jid: str
    message_id: str
    text: str
    iso_time: str
    chat_obj: Any = None
    event_obj: Any = None  # raw neonize MessageEv for reply-to quoting
    is_voice_request: bool = False  # True when text starts with /voice
    push_name: str = ""


def _message_id(event: Any) -> str:
    info = getattr(event, "Info", None)
    if info is None:
        return ""
    return str(getattr(info, "ID", "") or "")


def _is_from_me(event: Any) -> bool:
    info = getattr(event, "Info", None)
    source = getattr(info, "MessageSource", None) if info is not None else None
    return bool(getattr(source, "IsFromMe", False))


def _source(event: Any) -> Any:
    info = getattr(event, "Info", None)
    return getattr(info, "MessageSource", None) if info is not None else None


def _chat_jid(event: Any) -> Any:
    return getattr(_source(event), "Chat", None)


def _sender_jid(event: Any) -> Any:
    source = _source(event)
    return getattr(source, "SenderAlt", None) or getattr(source, "Sender", None)


def _push_name(event: Any) -> str:
    info = getattr(event, "Info", None)
    if info is None:
        return ""
    name = getattr(info, "Pushname", None) or getattr(info, "VerifiedName", None) or ""
    return str(name).strip()


def _find_document_message(payload: Any) -> Any:
    """Extract documentMessage from payload, handling documentWithCaptionMessage wrapping."""
    if payload is None:
        return None
    doc = getattr(payload, "documentMessage", None)
    if doc is not None:
        return doc
    doc_caption = getattr(payload, "documentWithCaptionMessage", None)
    if doc_caption is not None:
        sub_msg = getattr(doc_caption, "message", None)
        if sub_msg is not None:
            return getattr(sub_msg, "documentMessage", None)
    return None


def extract_message_text(payload: Any) -> str:
    if payload is None:
        return ""
    try:
        text = extract_text(payload) or ""
    except Exception:
        text = ""
    if not text:
        conversation = getattr(payload, "conversation", None) or ""
        extended = getattr(payload, "extendedTextMessage", None)
        ext_text = getattr(extended, "text", None) or "" if extended else ""
        doc = _find_document_message(payload)
        doc_caption = getattr(doc, "caption", None) or "" if doc else ""
        text = conversation or ext_text or doc_caption
    return str(text).strip()


def extract_mentioned_users(payload: Any) -> tuple[str, ...]:
    if payload is None:
        return ()
    extended = getattr(payload, "extendedTextMessage", None)
    ctx = getattr(extended, "contextInfo", None) if extended else None
    mentions = getattr(ctx, "mentionedJID", None) if ctx else None
    if not mentions:
        return ()
    found: list[str] = []
    for item in mentions:
        user = jid_user(item)
        if user:
            found.append(user)
    return tuple(found)


_URL_RE = re.compile(
    r"https?://[^\s<>\"']+",
    re.IGNORECASE,
)


def extract_urls(text: str) -> tuple[str, ...]:
    """Extract HTTP(S) URLs from message text."""
    if not text:
        return ()
    return tuple(dict.fromkeys(_URL_RE.findall(text)))


def extract_quoted_context(payload: Any) -> tuple[str, str]:
    """Extract quoted message text and sender from WhatsApp reply-to context.

    Returns (quoted_text, quoted_sender_jid).
    """
    if payload is None:
        return ("", "")
    extended = getattr(payload, "extendedTextMessage", None)
    ctx = getattr(extended, "contextInfo", None) if extended else None
    if ctx is None:
        return ("", "")
    quoted_msg = getattr(ctx, "quotedMessage", None)
    if quoted_msg is None:
        return ("", "")
    quoted_text = (
        getattr(quoted_msg, "conversation", None)
        or getattr(getattr(quoted_msg, "extendedTextMessage", None), "text", None)
        or ""
    )
    participant = str(getattr(ctx, "participant", "") or "")
    return (str(quoted_text).strip(), participant)


def extract_document_info(payload: Any) -> tuple[bool, str, str]:
    """Detect if message contains a document attachment.

    Returns (has_document, filename, mimetype).
    """
    doc = _find_document_message(payload)
    if doc is None:
        return (False, "", "")
    filename = str(getattr(doc, "fileName", "") or getattr(doc, "title", "") or "")
    mimetype = str(getattr(doc, "mimetype", "") or "")
    return (True, filename.strip(), mimetype.strip())


def datetime_from_unix(value: float) -> datetime:
    """WhatsApp timestamps may be seconds, milliseconds, or nanoseconds."""
    if value >= 1e17:
        value = value / 1e9
    elif value >= 1e12:
        value = value / 1e3
    return datetime.fromtimestamp(value, tz=timezone.utc)


def event_timestamp_iso(event: Any) -> str:
    info = getattr(event, "Info", None)
    ts = getattr(info, "Timestamp", None) if info is not None else None
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.isoformat()
    if isinstance(ts, (int, float)):
        return datetime_from_unix(float(ts)).isoformat()
    return datetime.now(timezone.utc).isoformat()


def is_reply_group(chat_jid: str, settings: Settings) -> bool:
    targets = settings.test_group_jids or (
        parse_group_jids(settings.test_group_jid) if settings.test_group_jid else ()
    )
    if not targets:
        return False
    if "*" in targets or "all" in targets:
        return True
    chat_norm = chat_jid.strip().lower()
    chat_num = chat_norm.split("@", 1)[0]
    for target in targets:
        target_norm = target.strip().lower()
        target_num = target_norm.split("@", 1)[0]
        if chat_norm == target_norm or chat_num == target_num:
            return True
    return False



_VOICE_RE = re.compile(
    r"(?:^|[\s@\(\[,;:\-/])(?:/voice|/?voicemode|/?voice_mode)(?:\b|[\s!?\.\)\]]|$)",
    re.IGNORECASE,
)


def _strip_voice_prefix(text: str) -> tuple[str, bool]:
    """Strip /voice or /voicemode from any position in the message. Returns (cleaned_text, is_voice_request)."""
    if not text:
        return "", False
    if _VOICE_RE.search(text):
        cleaned = _VOICE_RE.sub(" ", text)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned, True
    return text, False


def classify_message(
    event: Any, settings: Settings
) -> IncomingGroupMessage | IncomingAdminDm | IncomingUserDm | SkipReason:
    if _is_from_me(event):
        return SkipReason.FROM_ME
    chat = _chat_jid(event)
    payload = getattr(event, "Message", None)
    text = extract_message_text(payload)
    iso_time = event_timestamp_iso(event)
    push_name = _push_name(event)
    if is_group_jid(chat):
        chat_str = jid_str(chat)
        if settings.knowledge_group_jid and chat_str.lower() == settings.knowledge_group_jid:
            # Check the sender, never the group's identity or an LLM claim.
            sender = jid_str(_sender_jid(event))
            if not is_admin_jid(sender, settings):
                return SkipReason.GROUP_NOT_ALLOWED
            has_doc, filename, mimetype = extract_document_info(payload)
            return IncomingKnowledgeMessage(
                chat_jid=chat_str, sender_jid=sender, message_id=_message_id(event),
                text=text, is_reply_group=False, iso_time=iso_time, chat_obj=chat,
                event_obj=event, has_document=has_doc, document_filename=filename,
                document_mimetype=mimetype, push_name=push_name)
        if not is_reply_group(chat_str, settings):
            return SkipReason.GROUP_NOT_ALLOWED
        # Detect and strip /voice prefix before any further processing
        text, is_voice = _strip_voice_prefix(text)
        urls = extract_urls(text)
        quoted_text, quoted_sender = extract_quoted_context(payload)
        has_doc, doc_filename, doc_mimetype = extract_document_info(payload)
        return IncomingGroupMessage(
            chat_jid=chat_str,
            sender_jid=jid_str(_sender_jid(event)),
            message_id=_message_id(event),
            text=text,
            is_reply_group=is_reply_group(chat_str, settings),
            iso_time=iso_time,
            mentioned_users=extract_mentioned_users(payload),
            chat_obj=chat,
            urls=urls,
            quoted_text=quoted_text,
            quoted_sender=quoted_sender,
            has_document=has_doc,
            document_filename=doc_filename,
            document_mimetype=doc_mimetype,
            event_obj=event,
            is_voice_request=is_voice,
            push_name=push_name,
        )
    if event_looks_like_admin(event, settings):
        has_doc, filename, mimetype = extract_document_info(payload)
        if not text and not (has_doc and mimetype == "application/pdf"):
            return SkipReason.EMPTY_DM
        text, is_voice = _strip_voice_prefix(text)
        return IncomingAdminDm(
            chat_jid=jid_str(chat),
            sender_jid=jid_str(_sender_jid(event)),
            message_id=_message_id(event),
            text=text,
            iso_time=iso_time,
            chat_obj=chat,
            event_obj=event,
            is_voice_request=is_voice,
            push_name=push_name,
            has_document=has_doc, document_filename=filename, document_mimetype=mimetype,
        )
    if not text:
        return SkipReason.EMPTY_DM
    text, is_voice = _strip_voice_prefix(text)
    return IncomingUserDm(
        chat_jid=jid_str(chat),
        sender_jid=jid_str(_sender_jid(event)),
        message_id=_message_id(event),
        text=text,
        iso_time=iso_time,
        chat_obj=chat,
        event_obj=event,
        is_voice_request=is_voice,
        push_name=push_name,
    )
