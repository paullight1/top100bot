from __future__ import annotations

import re

_GREETINGS = re.compile(
    r"^(?:(?:hi|hello|hey|dear|habari|jambo|bonjour|salut|please|tafadhali|s'il vous plaît|svp)\b[\s,:-]*)+",
    re.IGNORECASE,
)

# English question starters and auxiliary verbs
_EN_WH_START = re.compile(
    r"^\s*(who|what|when|where|why|how|is|are|am|was|were|can|could|should|"
    r"do|does|did|will|would|which|whose|whom|may|might|shall|must)\b",
    re.IGNORECASE,
)

# English inquiry phrases (even without question mark)
_EN_INQUIRY = re.compile(
    r"\b(tell me|show me|explain|clarify|describe|help me with|give me details|"
    r"anyone know|anybody know|does anyone know|can someone|could someone|"
    r"any idea|wondering if|want to know|would like to know|curious about|"
    r"how to|how do|how can|what about|how about|where to|is there|are there)\b",
    re.IGNORECASE,
)

# Swahili interrogatives and question phrases
_SW_QUESTION = re.compile(
    r"\b(nani|nini|lini|wapi|kwa nini|kwanini|mbona|vipi|aje|gani|ngapi|je|"
    r"unaweza|tunaweza|naweza|niambie|nieleze|tueleze|eleza|"
    r"nataka kujua|ningependa kujua|naomba kujua|mtu yeyote anajua|yeyote anajua|"
    r"kuna mtu|habari gani)\b",
    re.IGNORECASE,
)

# French interrogatives and question phrases
_FR_QUESTION = re.compile(
    r"\b(qui|que|quoi|quand|où|ou|pourquoi|comment|combien|quel|quelle|quels|quelles|"
    r"est-ce que|est-ce|qu'est-ce que|qu'est-ce|c'est quoi|y a-t-il|y'a-t-il|"
    r"peut-on|pouvons-nous|puis-je|pouvez-vous|peux-tu|"
    r"dis-moi|dites-moi|expliquez|explique|montrez|"
    r"quelqu'un sait|je voudrais savoir|je veux savoir)\b",
    re.IGNORECASE,
)

_QUESTION_MARKS = ("?", "؟", "¿")
_BOT_TAG_RE = re.compile(r"(?:^|\s)@bot\b", re.IGNORECASE)


def looks_like_question(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if any(q in stripped for q in _QUESTION_MARKS):
        return True
    body = _GREETINGS.sub("", stripped).strip()
    if not body:
        return False
    if _EN_WH_START.search(body) or _EN_INQUIRY.search(body):
        return True
    if _SW_QUESTION.search(body):
        return True
    if _FR_QUESTION.search(body):
        return True
    return False


def is_bot_mentioned(
    text: str,
    mentioned: tuple[str, ...],
    own_users: set[str],
) -> bool:
    lowered = text.lower()
    if _BOT_TAG_RE.search(lowered):
        return True
    if not own_users:
        return False
    mentioned_norm = {m.lower() for m in mentioned}
    own_norm = {u.lower() for u in own_users if u}
    if mentioned_norm & own_norm:
        return True
    # Digits-only comparison for phone number mentions
    own_digits = {"".join(ch for ch in u if ch.isdigit()) for u in own_norm if u}
    mentioned_digits = {"".join(ch for ch in m if ch.isdigit()) for m in mentioned_norm if m}
    if own_digits & mentioned_digits:
        return True
    for user in own_norm:
        if user and f"@{user}" in lowered:
            return True
    for d in own_digits:
        if d and f"@{d}" in lowered:
            return True
    return False


def is_reply_to_bot(quoted_sender: str, own_users: set[str]) -> bool:
    """Return True if the quoted message was sent by the bot."""
    if not quoted_sender or not own_users:
        return False
    norm = quoted_sender.lower().strip()
    own_norm = {u.lower() for u in own_users if u}
    if norm in own_norm:
        return True
    q_digits = "".join(ch for ch in norm.split("@")[0] if ch.isdigit())
    if not q_digits:
        return False
    own_digits = {"".join(ch for ch in u if ch.isdigit()) for u in own_norm if u}
    return q_digits in own_digits

