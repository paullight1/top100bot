from __future__ import annotations

import logging
import re
from typing import Any

from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from unipod_bot.config import Settings
from unipod_bot.groq_gate import groq_reply
from unipod_bot.whatsapp import looks_like_question

logger = logging.getLogger("unipod_bot")

try:
    from groq import APIError, APITimeoutError, AsyncGroq, RateLimitError
except ImportError:  # pragma: no cover
    APIError = APITimeoutError = RateLimitError = Exception  # type: ignore[misc, assignment]
    AsyncGroq = None  # type: ignore[assignment]


def _retry_groq(exc: BaseException) -> bool:
    """Retry predicate for transient errors — NOT rate limits (handled by key rotation)."""
    if isinstance(exc, (APITimeoutError, TimeoutError)):
        return True
    if isinstance(exc, RateLimitError):
        return False  # handled by key-rotation loop, not tenacity
    if isinstance(exc, APIError):
        status = getattr(exc, "status_code", None)
        return status in {408, 409, 500, 502, 503, 504}
    return False


SYSTEM_PROMPT = """You are an ambient member of the Unipod innovators WhatsApp community.
Answer only from the provided facts and recent chat context. Do not invent information.
If the facts and recent chat do not contain the answer, reply with exactly: UNKNOWN

Tone: Be warm, accommodating, and eloquent — not robotic or blunt.
Use 1–2 relevant emojis per reply for warmth (e.g. 📅, ✅, 👋, 🎯).
Do NOT overuse emojis or add hollow filler like "Great question!" or "Sure thing!".

Formatting & Output Rules:
- Prohibited formatting: Do NOT use markdown headers (# Heading), code blocks with backticks, or horizontal rules (---).
- Numbered lists (1., 2., 3.): Use ONLY for sequential steps, instructions, chronological agendas, or ranked processes where order matters (e.g. how to register, submission steps). Keep them formatted as 1. Step one, 2. Step two.
- Bullet points (• ): Use ONLY for parallel, non-sequential collections (e.g. multiple events, criteria, eligibility rules, resources, links, or contact options). Place each item on its own bullet point with clean spacing between items for readability.
- Hyphens (-): Use within an item as an inline attribute separator (e.g. • Hackathon - Oct 15 - Main Lab) or for date and time spans (e.g. 9:00 AM - 5:00 PM, Oct 12 - Oct 14). Do NOT use hyphens as top-level list markers (always use • for bullets).
- Standard single-fact answers: When answering straightforward questions or providing a single piece of information, write in natural, conversational paragraphs (default to 1–3 sentences). Never wrap a single fact or single-sentence answer in a bullet point or numbered item.
- Multi-part answers: If a correct answer requires multiple details (multi-step how-to, rules, several facts), structure it cleanly with paragraphs, numbered steps, or bullet points depending on whether order matters.
- Voice note requests (/voice): When requested to reply in voice mode, deliver a smooth, natural spoken answer intended for text-to-speech. Do NOT use bullet points, hyphens, numbered list notations, or raw URLs. Use natural spoken transitions ("First...", "Also...", "Finally...") and spell out time ranges with "to".

Tagging and mentioning users — CRITICAL:
- Whenever you tag or refer to any user (the question asker, someone sharing an event, a team-seeker, or a contact), ALWAYS use the clean phone tag format: @<digits> (e.g. @254712345678).
- NEVER append "@s.whatsapp.net" or write raw JID domains in your reply.
- Pure @<digits> tags automatically render as WhatsApp native green mention bubbles for the user.

Source attribution & verification:
- State facts clearly, concisely, and accurately based on the provided context.
- Mention who provided the information (e.g. "according to @phone") when helpful for context.
- Facts include a source tag like [from @phone] or [from @phone in group …]. Admin phone numbers are trusted sources.
- ONLY include the unverified disclaimer and refer to feedback contacts if:
  1. The information is an informal observation, rumor, or unconfirmed personal claim, OR
  2. The user specifically asks whether the information is official, verified, or confirmed, OR
  3. There is an explicit contradiction between sources.
- Do NOT add the unverified disclaimer or admin contact info to routine, general, or established community facts.

Team-finding and collaboration matching:
- Pay close attention to facts tagged with [team-seeking] or [looking-for-collaborator].
  These indicate community members who expressed interest in joining or forming a team.
- When someone asks about finding a team, teammates, or collaborators for an event
  (hackathon, project, competition, etc.), check the facts for any members who posted
  similar team-seeking intent.
- If you find matching members, tag them directly using @phone in your reply so
  WhatsApp notifies them. For example: "You might want to connect with @254XXXXXXXXX
  who was also looking for teammates for this hackathon 🎯"
- When someone announces they are looking for a team (not a question), note the intent
  and if there are already matching seekers in the facts, proactively introduce them:
  "@254XXX was also looking for a hackathon team — you two should connect! 🤝"
- Only tag users when there is a factual basis from the knowledge graph; never invent matches.

Cross-group context:
- The Unipod community spans multiple WhatsApp groups serving the same purpose.
- Facts may include [in group …] indicating which group the info came from.
- When information was derived from another group, acknowledge this naturally, e.g.
  "Based on what was shared in another community group, …"

Links and references:
- When your answer references a link, document, or file that was previously shared
  in the community, include the URL or filename in your reply so the user can access it.

Resolve follow-ups such as "this", "that", "who said this", or "is this verified"
from the recent messages in this chat.
When community facts disagree, prefer the latest statement in recent messages.

Domain helpers — referral directory:
- A list of designated helpers per topic/domain will be provided in the prompt when configured.
- Only refer the user to a helper if the question cannot be answered from the facts or if the user asks for personal assistance in that domain.
- Do NOT tag helpers on routine answers where the facts already provide the answer.
- When no helpers are configured or none match the topic, skip this step entirely.
"""

CORRECTION_PROMPT = """You are an ambient member of the Unipod innovators WhatsApp community.
You noticed a statement in the group that may contradict verified facts you have.

Rules:
1. ONLY correct if you are ABSOLUTELY CERTAIN the facts contradict the statement.
   If there is ANY ambiguity or the contradiction is minor/debatable, reply: NO_CORRECTION
2. Be polite and gentle. Do NOT be confrontational or condescending.
3. Use a tone like: "Just a small note — [correct fact]. You might want to check with
   [feedback contacts] for the latest details 😊"
4. Keep it brief (1–3 sentences). Use 1–2 emojis for warmth.
5. If the user has already pushed back or insisted on their version in recent messages,
   reply: NO_CORRECTION — do NOT argue. The bot backs off to avoid conflict.
6. If no contradiction is found, reply with exactly: NO_CORRECTION
"""

_UNKNOWN = re.compile(r"^UNKNOWN\b", re.IGNORECASE)


def is_unknown_reply(text: str | None) -> bool:
    if not text or not text.strip():
        return True
    return bool(_UNKNOWN.match(text.strip()))


def assemble_reply_prompt(
    *,
    facts: list[str],
    history: str,
    sender_jid: str,
    question: str,
    feedback_contacts: str = "",
    admin_phones: tuple[str, ...] = (),
    cross_group_context: str = "",
    helpers_text: str = "",
    is_voice: bool = False,
    quoted_text: str = "",
) -> str:
    fact_block = "\n".join(f"- {item}" for item in facts) or "(none)"
    hist_block = history.strip() or "(none)"
    fc_block = (
        f"\nFeedback / verification contacts: {feedback_contacts}\n"
        if feedback_contacts
        else ""
    )
    admin_block = (
        f"\nAdmin phone numbers (trusted sources): {', '.join('@' + p for p in admin_phones)}\n"
        if admin_phones
        else ""
    )
    cross_block = (
        f"\nRecent messages from other community groups (cross-group context):\n{cross_group_context}\n"
        if cross_group_context.strip()
        else ""
    )
    helpers_block = (
        f"\nDomain helpers (refer users to these contacts for the listed topics):\n{helpers_text}\n"
        if helpers_text.strip()
        else ""
    )
    voice_block = (
        "\n[VOICE NOTE MODE (/voice)]:\n"
        "The user requested an audio voice note. This response will be synthesized into spoken speech.\n"
        "- Deliver a warm, fluid spoken response that sounds natural when read aloud.\n"
        "- Do NOT use bullet points (•), hyphens as dashes/bullets, numbered list notations (e.g. '1.', '2.'), or markdown symbols.\n"
        "- Use spoken transition words (e.g. 'First...', 'Next...', 'Also...', 'Finally...') to structure multi-part answers.\n"
        "- Express time and date ranges using words like 'to' or 'until' (e.g. 'from 9 to 11 AM', not '9 - 11 AM').\n"
        "- Do NOT include raw web links or email addresses; refer to them colloquially (e.g. 'the shared link').\n"
        if is_voice
        else ""
    )
    quoted_block = (
        f"\nMessage being replied to (quoted context):\n\"{quoted_text.strip()}\"\n"
        if quoted_text.strip()
        else ""
    )
    sender_digits = "".join(ch for ch in sender_jid.split("@")[0] if ch.isdigit())
    sender_tag = f"@{sender_digits}" if sender_digits else sender_jid
    return (
        f"Community facts (older graph memory; may be stale):\n{fact_block}\n\n"
        f"Recent messages in this chat (current thread; prefer these on conflict):\n{hist_block}\n\n"
        f"{cross_block}"
        f"{admin_block}"
        f"{fc_block}"
        f"{helpers_block}"
        f"{voice_block}"
        f"{quoted_block}"
        f"Question from {sender_tag}:\n{question}\n"
    )


def assemble_correction_prompt(
    *,
    statement: str,
    sender_jid: str,
    facts: list[str],
    feedback_contacts: str = "",
) -> str:
    fact_block = "\n".join(f"- {item}" for item in facts) or "(none)"
    fc_block = (
        f"\nFeedback / verification contacts: {feedback_contacts}\n"
        if feedback_contacts
        else ""
    )
    sender_digits = "".join(ch for ch in sender_jid.split("@")[0] if ch.isdigit())
    sender_tag = f"@{sender_digits}" if sender_digits else sender_jid
    return (
        f"Verified community facts:\n{fact_block}\n\n"
        f"{fc_block}"
        f"Statement from {sender_tag}:\n{statement}\n\n"
        f"If any part of the statement contradicts the verified facts, "
        f"write a polite, brief correction. Otherwise reply: NO_CORRECTION\n"
    )


_QUERY_INTENT_BOOSTS = [
    (
        re.compile(r"\b(deadlines?|due (?:date|on|by)|when (?:is|are|does)|closes?|submissions?|apply by)\b", re.IGNORECASE),
        "deadline schedule date",
    ),
    (
        re.compile(r"\b(teams?|teammates?|collaborat(?:or|ion)|partner|co-?founder)\b", re.IGNORECASE),
        "team collaboration teammates",
    ),
    (
        re.compile(r"\b(documents?|pdfs?|guidelines?|rubrics?|templates?|links?|repos?|slides?)\b", re.IGNORECASE),
        "document resource link pdf",
    ),
    (
        re.compile(r"\b(hackathons?|workshops?|bootcamps?|meetups?|events?|webinars?)\b", re.IGNORECASE),
        "event hackathon workshop",
    ),
]


def detect_query_boost(query: str) -> str:
    """Return keyword boosts matching user query intent."""
    boosts: list[str] = []
    for pattern, boost in _QUERY_INTENT_BOOSTS:
        if pattern.search(query):
            boosts.append(boost)
    return " ".join(boosts)


def retrieval_query(question: str, history: str, quoted_text: str = "") -> str:
    """Blend recent chat, quoted context, and intent boosting into the Graphiti query so follow-ups resolve."""
    parts: list[str] = []
    seen: set[str] = set()
    for line in (history or "").splitlines():
        raw = line.strip()
        if not raw or raw == "(none)":
            continue
        body = raw.split(": ", 1)[-1].strip()
        key = body.lower()
        if not body or key in seen:
            continue
        seen.add(key)
        parts.append(body)
    q = (question or "").strip()
    quoted = (quoted_text or "").strip()
    if quoted and quoted.lower() not in seen:
        parts.append(quoted)
    if q and q.lower() not in seen:
        parts.append(q)
    boost = detect_query_boost(f"{q} {quoted}".strip())
    base = " ".join(parts[-6:]) or q
    return f"{base} {boost}".strip() if boost else base


def is_question_fact(text: str) -> bool:
    body = (text or "").split(": ", 1)[-1].strip()
    return looks_like_question(body)


class GroqReplyClient:
    def __init__(self, settings: Settings, pool: "GroqKeyPool | None" = None) -> None:
        if AsyncGroq is None:
            raise RuntimeError("The groq package is not installed. Run: uv add groq && uv sync")

        from unipod_bot.groq_pool import GroqKeyPool

        if pool is not None:
            self._pool = pool
        elif settings.groq_api_keys:
            self._pool = GroqKeyPool(list(settings.groq_api_keys))
        else:
            raise ValueError("GROQ_API_KEY is required for replies")
        self._settings = settings

    def _make_client(self, api_key: str) -> "AsyncGroq":
        return AsyncGroq(
            api_key=api_key,
            timeout=self._settings.groq_timeout_seconds,
            max_retries=0,
        )

    @staticmethod
    def _extract_retry_after(exc: BaseException) -> float:
        """Pull retry-after seconds from a Groq 429 response."""
        resp = getattr(exc, "response", None)
        if resp is not None:
            headers = getattr(resp, "headers", {})
            raw = headers.get("retry-after")
            if raw:
                try:
                    return float(raw)
                except (ValueError, TypeError):
                    pass
        return 60.0

    @retry(
        retry=retry_if_exception(_retry_groq),
        wait=wait_exponential(multiplier=1, min=1, max=20),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def chat_completion(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_completion_tokens: int = 1024,
        reasoning_effort: str | None = "low",
    ) -> str:
        """Call Groq chat completion with automatic key rotation and retry."""
        from unipod_bot.groq_pool import GroqKeyPoolExhausted

        for _attempt in range(self._pool.total):
            try:
                key = self._pool.get_key()
            except GroqKeyPoolExhausted:
                raise

            logger.debug("event=groq_call model=%s key=%s…", self._settings.groq_model, key[:8])
            try:
                async with groq_reply():
                    client = self._make_client(key)
                    kwargs: dict[str, Any] = {
                        "model": self._settings.groq_model,
                        "messages": messages,
                        "temperature": temperature,
                        "max_completion_tokens": max_completion_tokens,
                    }
                    if reasoning_effort is not None:
                        kwargs["reasoning_effort"] = reasoning_effort
                    response = await client.chat.completions.create(**kwargs)
                choice = response.choices[0].message.content or ""
                return choice.strip()
            except RateLimitError as exc:
                retry_after = self._extract_retry_after(exc)
                self._pool.mark_rate_limited(key, retry_after)
                logger.warning(
                    "event=groq_key_429 model=%s key=%s… retry_after=%.0f rotating_immediately",
                    self._settings.groq_model,
                    key[:8],
                    retry_after,
                )
                continue  # immediately try next key
            except APIError as exc:
                logger.error(
                    "event=groq_http status=%s model=%s body=%s",
                    getattr(exc, "status_code", None),
                    self._settings.groq_model,
                    getattr(exc, "body", None) or str(exc),
                )
                raise  # let tenacity handle transient errors

        # All keys exhausted in this rotation pass — park pool until daily reset
        wait = self._pool.pause_until_reset()
        raise GroqKeyPoolExhausted(
            f"All {self._pool.total} Groq API keys hit rate limits. "
            f"Pool paused for {wait:.0f}s until midnight UTC."
        )

    async def generate(self, user_prompt: str) -> str:
        """Generate a reply, rotating keys immediately on 429 (no backoff)."""
        return await self.chat_completion(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            max_completion_tokens=1024,
            reasoning_effort="low",
        )

