from __future__ import annotations

import re

from unipod_bot.conversation import addressed_to_bot, is_short_followup
from unipod_bot.fact_check import looks_like_factual_claim
from unipod_bot.incoming import IncomingGroupMessage
from unipod_bot.models import IncomingMessage
from unipod_bot.whatsapp import is_bot_mentioned, is_reply_to_bot, looks_like_question

_LEADING_MENTIONS = re.compile(r"^(?:@\S+\s+)+")
_AT_DIGITS = re.compile(r"@(\d+)")
_REMINDER_TRIGGER_RE = re.compile(r"(?:^|\s)/(?:reminder|remind)\b", re.IGNORECASE)
_SUMMARY_TRIGGER_RE = re.compile(r"(?:^|\s)/(?:summary|summarize)\b", re.IGNORECASE)


def mentions_in_text(text: str) -> tuple[str, ...]:
    return tuple(_AT_DIGITS.findall(text or ""))


def question_body(text: str) -> str:
    return _LEADING_MENTIONS.sub("", (text or "").strip())


def group_reply_trigger(
    text: str,
    mentioned_users: tuple[str, ...],
    own_users: set[str],
    quoted_sender: str = "",
    is_voice: bool = False,
) -> str | None:
    """Return 'mention', 'question', 'voice', 'reminder', 'summary', or 'statement_check' for group reply routing."""
    stripped = (text or "").strip()
    if not stripped and not is_voice:
        return None
    combined = tuple(dict.fromkeys((*mentioned_users, *mentions_in_text(stripped))))
    if is_bot_mentioned(stripped, combined, own_users) or is_reply_to_bot(quoted_sender, own_users):
        return "mention"
    # If the message @-mentions specific users (not the bot), the question
    # is directed at them.  Don't answer on their behalf.
    if combined:
        return None
    if addressed_to_bot(stripped):
        return "mention"
    if is_short_followup(stripped):
        return "question"
    if _SUMMARY_TRIGGER_RE.search(stripped):
        return "summary"
    if _REMINDER_TRIGGER_RE.search(stripped):
        return "reminder"
    if is_voice:
        return "voice"
    if looks_like_question(question_body(stripped)):
        return "question"
    # Not a question — check if it contains a factual claim worth verifying.
    if looks_like_factual_claim(stripped):
        return "statement_check"
    return None


def group_message_trigger(msg: IncomingGroupMessage, own_users: set[str]) -> str | None:
    return group_reply_trigger(
        msg.text,
        msg.mentioned_users,
        own_users,
        quoted_sender=getattr(msg, "quoted_sender", ""),
        is_voice=getattr(msg, "is_voice_request", False),
    )


def should_attempt_reply(
    msg: IncomingMessage,
    own_users: set[str],
) -> bool:
    """Return True if this message is an @-mention or a question (groups) or any DM text."""
    if not msg.text or msg.is_from_me:
        return False
    if not msg.is_group:
        return True
    return group_reply_trigger(msg.text, msg.mentioned_users, own_users) is not None
