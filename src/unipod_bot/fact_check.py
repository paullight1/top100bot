"""Fact-checking pipeline for non-question statements in group chat.

When a group message is a statement (not a question, not an @-mention), this
module checks whether the claims contradict verified facts in Graphiti. If a
contradiction is found, it generates a polite correction reply.

The pipeline:
1. `looks_like_factual_claim` — lightweight heuristic to decide whether a
   statement is worth checking (contains dates, numbers, names, assertions).
2. The caller retrieves Graphiti facts for the statement text.
3. The correction prompt is assembled and sent to Groq with CORRECTION_PROMPT.
4. If Groq returns "NO_CORRECTION", the bot stays silent. Otherwise it replies.
"""

from __future__ import annotations

import re

# Patterns that suggest a message contains a factual claim worth checking.
# We intentionally keep this conservative to avoid over-correcting casual chat.
_DATE_PATTERN = re.compile(
    r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{1,2}\s+"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*"
    r"(?:\s+\d{2,4})?)\b",
    re.IGNORECASE,
)
_TIME_PATTERN = re.compile(r"\b\d{1,2}:\d{2}\s*(?:am|pm)?\b", re.IGNORECASE)
_NUMBER_CLAIM = re.compile(r"\b(?:costs?|price|fee|amount|total)\s+\w*\s*\d", re.IGNORECASE)
_ASSERTION_KEYWORDS = re.compile(
    r"\b(the\s+(?:event|hackathon|meeting|deadline|registration|venue|date|time|location|price|fee)"
    r"|(?:starts?|ends?|begins?|closes?|opens?)\s+(?:at|on|in|by)"
    r"|(?:is\s+(?:at|on|in|scheduled|moved|changed|cancelled|postponed))"
    r"|(?:was\s+(?:moved|changed|cancelled|postponed|rescheduled)))\b",
    re.IGNORECASE,
)

_NO_CORRECTION = re.compile(r"^NO_CORRECTION\b", re.IGNORECASE)


def looks_like_factual_claim(text: str) -> bool:
    """Return True if the text looks like it contains a checkable factual claim.

    This is deliberately conservative — we only flag messages that contain
    dates, times, numeric claims, or assertion keywords about events/schedules.
    Casual chat and opinions are NOT flagged.
    """
    stripped = (text or "").strip()
    if not stripped or len(stripped) < 10:
        return False
    if _DATE_PATTERN.search(stripped):
        return True
    if _TIME_PATTERN.search(stripped):
        return True
    if _NUMBER_CLAIM.search(stripped):
        return True
    if _ASSERTION_KEYWORDS.search(stripped):
        return True
    return False


def is_no_correction(text: str | None) -> bool:
    """Return True if the LLM response indicates no correction is needed."""
    if not text or not text.strip():
        return True
    return bool(_NO_CORRECTION.match(text.strip()))
