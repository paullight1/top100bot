"""Text sanitization utilities for WhatsApp bot output.

sanitize_for_chat()  -- strip markdown artifacts; applied to ALL Groq replies.
sanitize_for_tts()   -- further normalize for natural speech synthesis; applied
                        when generating voice notes via Piper TTS.
"""

from __future__ import annotations

import re


# ---------------------------------------------------------------------------
# Chat sanitization  (remove markdown; keep emojis + natural punctuation)
# ---------------------------------------------------------------------------

# Bold / italic markers
_BOLD_ITALIC = re.compile(r"\*{1,3}(.+?)\*{1,3}", re.DOTALL)
_UNDER_ITALIC = re.compile(r"_{1,2}(.+?)_{1,2}", re.DOTALL)
_STRIKE = re.compile(r"~~(.+?)~~", re.DOTALL)

# Inline code and code blocks
_CODE_BLOCK = re.compile(r"```[\s\S]*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]+)`")

# Markdown headers (## Heading)
_HEADER = re.compile(r"^#{1,6}\s+", re.MULTILINE)

# Markdown blockquote lines
_BLOCKQUOTE = re.compile(r"^>\s*", re.MULTILINE)

# Bullet list markers at line start (normalized to clean unicode bullet •)
_BULLET_NORM = re.compile(r"^[\s]*[-*+]\s+", re.MULTILINE)

# Numbered list markers at line start (normalized to clean '1. ', '2. ', keeping numbers)
_NUMBERED_NORM = re.compile(r"^[\s]*(\d+)[.)]\s+", re.MULTILINE)

# Horizontal rules
_HRULE = re.compile(r"^[-_*]{3,}\s*$", re.MULTILINE)

# Markdown table rows (| col | col |)
_TABLE_ROW = re.compile(r"^\|.+\|$", re.MULTILINE)
_TABLE_SEP = re.compile(r"^\|[-| :]+\|$", re.MULTILINE)

# Collapse 3+ newlines to 2
_MULTI_NL = re.compile(r"\n{3,}")


def _strip_lone_bullet(text: str) -> str:
    """If text consists of a single bullet point without a list, convert to a clean sentence."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    bullet_lines = [line for line in lines if line.startswith("• ")]
    if len(bullet_lines) == 1 and len(lines) == 1:
        return text.replace("• ", "", 1).strip()
    return text


def _format_list_line_skips(text: str) -> str:
    """Ensure clean line skips (blank lines) before and after list blocks."""
    lines = text.split("\n")
    out: list[str] = []
    is_list = lambda l: bool(re.match(r"^[\s]*(?:•|\d+\.)\s+", l))

    for i, line in enumerate(lines):
        curr_is_list = is_list(line)
        if i > 0 and curr_is_list and not is_list(lines[i - 1]) and lines[i - 1].strip() and out and out[-1] != "":
            out.append("")
        elif i > 0 and not curr_is_list and line.strip() and is_list(lines[i - 1]) and out and out[-1] != "":
            out.append("")
        out.append(line)
    return "\n".join(out)


def ensure_emojis(text: str) -> str:
    """Ensure the text contains at least one warm, relevant emoji for chat presentability."""
    if not text or not text.strip():
        return text
    if _EMOJI.search(text):
        return text
    lower = text.lower()
    if re.search(r"\b(hackathon|meet(?:ing)?|call|event|workshop|schedule|session|date|time|tomorrow|friday|monday|today)\b", lower):
        emoji = "📅"
    elif re.search(r"\b(team|member|teammate|collaborat(?:or|ion)|partner|connect|join)\b", lower):
        emoji = "🤝"
    elif re.search(r"\b(link|portal|website|document|pdf|room|lab|location|venue|where)\b", lower):
        emoji = "📍"
    elif re.search(r"\b(register|step|submit|guideline|rule|done|success|confirm)\b", lower):
        emoji = "✅"
    elif re.search(r"\b(tip|idea|note|innovat(?:or|ion)|project|build)\b", lower):
        emoji = "💡"
    else:
        emoji = "🎯"
    return f"{text.rstrip()} {emoji}"


def sanitize_for_chat(text: str, add_emojis: bool = False) -> str:
    """Remove markdown formatting from LLM output for plain WhatsApp chat.

    Preserves clean bullet points (•), numbered lists (1., 2.), emojis,
    @-mentions, URLs, and natural punctuation. Ensures line skips around
    lists and optional emoji enrichment.
    """
    if not text:
        return text

    t = text

    # Remove fenced code blocks entirely (replace with stripped content)
    t = _CODE_BLOCK.sub(lambda m: m.group(0).strip("`").strip(), t)

    # Strip markdown table separators (dashes row)
    t = _TABLE_SEP.sub("", t)

    # Bold/italic/strikethrough — keep inner content
    t = _BOLD_ITALIC.sub(r"\1", t)
    t = _UNDER_ITALIC.sub(r"\1", t)
    t = _STRIKE.sub(r"\1", t)

    # Inline code — keep content
    t = _INLINE_CODE.sub(r"\1", t)

    # Headers — remove # markers, keep text
    t = _HEADER.sub("", t)

    # Blockquotes — remove > markers
    t = _BLOCKQUOTE.sub("", t)

    # Unordered bullets — normalize to clean unicode bullet points
    t = _BULLET_NORM.sub("• ", t)

    # Numbered lists — normalize to clean '1. ', '2. ', preserving list numbers
    t = _NUMBERED_NORM.sub(r"\1. ", t)

    # Strip lone bullet on single-line / single-sentence responses
    t = _strip_lone_bullet(t)

    # Horizontal rules
    t = _HRULE.sub("", t)

    # Ensure clean line skips before and after list blocks
    t = _format_list_line_skips(t)

    # Collapse extra blank lines
    t = _MULTI_NL.sub("\n\n", t)

    t = t.strip()

    if add_emojis:
        t = ensure_emojis(t)

    return t


# ---------------------------------------------------------------------------
# TTS sanitization  (further normalize for natural spoken output)
# ---------------------------------------------------------------------------

# Emoji unicode ranges (broad set)
_EMOJI = re.compile(
    "["
    "\U0001F600-\U0001F64F"   # emoticons
    "\U0001F300-\U0001F5FF"   # symbols & pictographs
    "\U0001F680-\U0001F6FF"   # transport & map
    "\U0001F1E0-\U0001F1FF"   # flags
    "\U00002600-\U000027BF"   # misc symbols
    "\U0001F900-\U0001F9FF"   # supplemental symbols
    "\U00002702-\U000027B0"
    "]+",
    flags=re.UNICODE,
)

# URLs — replace with spoken phrase
_URL = re.compile(r"https?://\S+")

# WhatsApp @mentions — keep phone number, drop the @
_MENTION = re.compile(r"@(\d+)")

# Parenthetical asides common in LLM output
_PAREN_NOTE = re.compile(r"\s*\(.*?\)", re.DOTALL)

# Multiple spaces / tabs
_MULTI_SPACE = re.compile(r"[ \t]+")

# Markdown table: convert to simple sentence-per-row
_TABLE_CELL = re.compile(r"\|")

# Spoken ordinals for numbered list items in voice synthesis
_ORDINALS: dict[int, str] = {
    1: "First,",
    2: "Second,",
    3: "Third,",
    4: "Fourth,",
    5: "Fifth,",
    6: "Sixth,",
    7: "Seventh,",
    8: "Eighth,",
    9: "Ninth,",
    10: "Tenth,",
}

# Regex to detect numbered lines for voice conversion (e.g. "1. Register")
_NUMBERED_LINE = re.compile(r"^(\d+)\.\s*(.*)$")

# Hyphen between time/date/day ranges (e.g. 9:00 AM - 5:00 PM, Monday - Friday, 2 - 4 PM, Oct 10 - Oct 12)
_TIME_DATE_RANGE = re.compile(
    r"(\b(?:\d{1,2}(?::\d{2})?\s*(?:am|pm|AM|PM)?|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*|Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*(?:\s+\d{1,2})?)"
    r"\s*[-–—]\s*"
    r"(\b(?:\d{1,2}(?::\d{2})?\s*(?:am|pm|AM|PM)?|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*|Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*(?:\s+\d{1,2})?)",
    re.IGNORECASE,
)

# Separator hyphens (e.g. "Event Name - Details") -> convert to comma for natural speech pause
_SEPARATOR_HYPHEN = re.compile(r"\s+[-–—]\s+")


def _table_to_sentences(text: str) -> str:
    """Convert markdown tables to spoken sentences."""
    lines = text.splitlines()
    out: list[str] = []
    for line in lines:
        if _TABLE_ROW.match(line) and not _TABLE_SEP.match(line):
            cells = [c.strip() for c in line.strip("|").split("|") if c.strip()]
            if cells:
                out.append(", ".join(cells) + ".")
        else:
            out.append(line)
    return "\n".join(out)


def _convert_numbered_to_spoken(text: str) -> str:
    """Convert numbered lists (1. Item) into natural spoken transitions ('First, item')."""
    lines = text.splitlines()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        m = _NUMBERED_LINE.match(stripped)
        if m:
            num = int(m.group(1))
            body = m.group(2).strip()
            transition = _ORDINALS.get(num, f"Number {num},")
            out.append(f"{transition} {body}")
        else:
            out.append(line)
    return "\n".join(out)


def sanitize_for_tts(text: str) -> str:
    """Normalize text for natural-sounding speech synthesis.

    Steps:
    1. Apply chat sanitization first (remove markdown).
    2. Convert tables to spoken sentences.
    3. Convert numbered lists into natural spoken transition words ('First, ...', 'Second, ...').
    4. Strip all bullet markers (•, -, *, +) cleanly from line starts.
    5. Convert time/date/day hyphen ranges ('9 AM - 5 PM') to 'to' ('9 AM to 5 PM').
    6. Convert separator hyphens (' - ') to natural commas (', ') for speech pausing.
    7. Remove emojis (they don't translate to speech).
    8. Replace URLs with a brief spoken reference.
    9. Convert @phone mentions to "user <phone>".
    10. Remove parenthetical asides that sound awkward aloud.
    11. Clean up stray leading/trailing dashes and normalize whitespace.
    12. Ensure sentences end with clean punctuation for natural TTS cadence.
    """
    if not text:
        return text

    # Step 1 — base markdown cleanup
    t = sanitize_for_chat(text)

    # Step 2 — flatten tables
    t = _table_to_sentences(t)

    # Step 3 — convert numbered lists to spoken transitions
    t = _convert_numbered_to_spoken(t)

    # Step 4 — strip all bullet markers for spoken speech
    t = re.sub(r"^[ \t]*[•\-*+]\s*", "", t, flags=re.MULTILINE)

    # Step 5 — time/date/day ranges with hyphens -> "to"
    t = _TIME_DATE_RANGE.sub(r"\1 to \2", t)

    # Step 6 — separator hyphens -> natural speech pause (comma)
    t = _SEPARATOR_HYPHEN.sub(", ", t)

    # Step 7 — remove emojis
    t = _EMOJI.sub("", t)

    # Step 8 — replace URLs
    t = _URL.sub("a shared link", t)

    # Step 9 — @mentions -> "user <phone>"
    t = _MENTION.sub(r"user \1", t)

    # Step 10 — remove parenthetical asides
    t = _PAREN_NOTE.sub("", t)

    # Step 11 — strip stray leading/trailing dashes on lines
    t = re.sub(r"^[-–—]\s*", "", t, flags=re.MULTILINE)
    t = re.sub(r"\s*[-–—]$", "", t, flags=re.MULTILINE)

    # Step 12 — normalize whitespace
    t = _MULTI_SPACE.sub(" ", t)
    t = _MULTI_NL.sub("\n\n", t)

    # Ensure sentences end with punctuation for natural TTS pausing
    lines = []
    for line in t.splitlines():
        line = line.strip()
        if line and line[-1] not in ".!?:":
            line += "."
        lines.append(line)
    t = "\n".join(lines)

    return t.strip()
