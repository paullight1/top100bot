from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from unipod_bot.config import Settings
from unipod_bot.jids import jid_str, phone_from_jid

_RECENT_CMD = re.compile(r"^(recent|last|log)(?:\s+(\d+))?$", re.IGNORECASE)
_ERRORS_CMD = re.compile(r"^errors?(?:\s+(\d+))?$", re.IGNORECASE)


def _digits(value: str | None) -> str:
    if not value:
        return ""
    return "".join(ch for ch in value if ch.isdigit())


def is_admin_phone(phone: str | None, settings: Settings) -> bool:
    digits = _digits(phone)
    return bool(digits) and digits in settings.admin_phones


def is_admin_jid(jid: Any, settings: Settings) -> bool:
    text = jid_str(jid).lower() if not isinstance(jid, str) else jid.strip().lower()
    if text and text in {j.lower() for j in settings.admin_jids}:
        return True
    return is_admin_phone(phone_from_jid(jid) if not isinstance(jid, str) else jid, settings)


def event_looks_like_admin(event: Any, settings: Settings) -> bool:
    info = getattr(event, "Info", None)
    source = getattr(info, "MessageSource", None) if info is not None else None
    candidates = [
        getattr(source, "Chat", None),
        getattr(source, "Sender", None),
        getattr(source, "SenderAlt", None),
        getattr(source, "RecipientAlt", None),
    ]
    phones: set[str] = set()
    jids: set[str] = set()
    for item in candidates:
        if item is None:
            continue
        text = jid_str(item)
        if text:
            jids.add(text.lower())
        digits = phone_from_jid(item)
        if digits:
            phones.add(digits)
    allow_jids = {j.lower() for j in settings.admin_jids}
    if jids & allow_jids:
        return True
    return bool(phones & set(settings.admin_phones))


@dataclass(frozen=True)
class AdminCommandResult:
    reply: str
    stub_known: bool | None = None
    use_groq: bool = False
    run_health: bool = False
    run_report: bool = False
    run_errors: int = 0  # 0 = don't run; >0 = show N recent errors
    toggle_ingest: bool | None = None
    run_ingest_text: str = ""


def handle_admin_dm(
    text: str,
    settings: Settings,
    *,
    stub_known: bool,
    recent_formatter: Callable[[int], str],
    ingest_count: int,
    notify_count: int,
    graphiti_ready: bool = False,
    graphiti_ingest: int = 0,
    admin_ingest_enabled: bool = True,
    knowledge_mode: bool = False,
) -> AdminCommandResult:
    lowered = text.strip().lower()

    # --- ping ---
    if lowered in {"ping", "pong", "hi", "hello", "hey"}:
        return AdminCommandResult("🏓 *Pong* — Bot is online and listening.")

    # --- help ---
    if lowered in {"help", "?"}:
        return AdminCommandResult(
            "📖 *Admin Commands*\n"
            "\n"
            "• *ping* — Check bot is alive\n"
            "• *status* — Bot config & counters\n"
            "• *health* — Run a live health check\n"
            "• *report* — View daily activity report\n"
            "• *errors [n]* — Recent errors (default 10)\n"
            "• *recent [n]* — Last N group messages\n"
            "• *known* — Switch to grounded-reply mode\n"
            "• *unknown* — Switch to silent + admin-notify mode\n"
            "• *ingest [on|off|<text>]* — Toggle ingest mode or ingest facts\n"
            "• *help* — This message\n"
            + ("• *knowledge help* — Draft, review, publish and retire approved knowledge\n" if knowledge_mode else "") +
            "\n"
            "_Any other text is answered via Groq or ingested into the knowledge graph._"
        )

    # --- status ---
    if lowered == "status":
        test_group = settings.test_group_jid or "(group processing disabled)"
        mode = ("✅ Known (approved library + public activities)" if knowledge_mode
                else "✅ Known (Groq from Graphiti + chat)") if stub_known else "🔇 Unknown (silent + admin DM)"
        groq = f"✅ On — {settings.groq_model}" if settings.groq_enabled else "❌ Off"
        graphiti = (
            f"✅ On — group={settings.graphiti_group_id}, episodes={graphiti_ingest}"
            if graphiti_ready
            else "❌ Off"
        )
        ingest_mode_str = "✅ On" if admin_ingest_enabled else "❌ Off"
        return AdminCommandResult(
            "🤖 *Bot Status*\n"
            "\n"
            "⚙️ *Configuration*\n"
            f"  Debug: {'on' if settings.debug else 'off'}\n"
            f"  Reply group: {test_group}\n"
            f"  Admins: {len(settings.admin_phones)}\n"
            f"  Admin Ingest Mode: {ingest_mode_str}\n"
            + (f"  Knowledge: enabled; feed={'configured' if settings.knowledge_group_jid else 'disabled'}\n" if knowledge_mode else "") +
            "\n"
            "🔁 *Reply Mode*\n"
            f"  {mode}\n"
            "\n"
            "🧠 *Backends*\n"
            f"  Groq: {groq}\n"
            f"  Graphiti: {graphiti}\n"
            "\n"
            "📊 *Counters*\n"
            f"  Messages logged: {ingest_count}\n"
            f"  Admin notifies: {notify_count}"
        )

    # --- ingest mode toggle & explicit ingest command ---
    if lowered == "ingest":
        mode_str = "✅ On" if admin_ingest_enabled else "❌ Off"
        return AdminCommandResult(
            f"📥 *Admin Ingest Mode: {mode_str}*\n\n"
            "• *ingest on* — Enable automatic ingestion of admin messages\n"
            "• *ingest off* — Disable automatic ingestion\n"
            "• *ingest <text>* — Ingest specific information into knowledge graph immediately"
        )
    if lowered in {"ingest on", "ingest mode on", "mode ingest on"}:
        return AdminCommandResult(
            "📥 *Admin Ingest Mode → On*\n\n"
            "Statements you send in this admin chat that are not bot commands will be "
            "ingested into the community knowledge graph as verified admin facts. 🧠",
            toggle_ingest=True,
        )
    if lowered in {"ingest off", "ingest mode off", "mode ingest off"}:
        return AdminCommandResult(
            "📥 *Admin Ingest Mode → Off*\n\n"
            "Automatic ingestion in this admin chat is now disabled. "
            "Use `ingest <text>` to add specific facts manually. ⏸️",
            toggle_ingest=False,
        )
    if (
        lowered.startswith("ingest ")
        or lowered.startswith("/ingest ")
        or lowered.startswith("ingest:")
        or lowered.startswith("learn ")
    ):
        fact_text = re.sub(
            r"^(?:/?ingest:?|learn)\s*", "", text.strip(), flags=re.IGNORECASE
        ).strip()
        if fact_text:
            return AdminCommandResult("", run_ingest_text=fact_text)

    # --- health ---
    if lowered == "health":
        return AdminCommandResult("", run_health=True)

    # --- report ---
    if lowered in {"report", "daily report"}:
        return AdminCommandResult("", run_report=True)

    # --- errors ---
    match_errors = _ERRORS_CMD.match(text.strip())
    if match_errors:
        n = int(match_errors.group(1) or "10")
        return AdminCommandResult("", run_errors=max(1, min(n, 50)))

    # --- stub known ---
    if lowered in {"stub known", "known"}:
        groq_hint = (
            "Replies use only published current knowledge and public Top100 activities."
            if knowledge_mode else
            "Groq will answer in-group from Graphiti facts and recent chat."
            if settings.groq_enabled
            else "⚠️ GROQ_API_KEY unset — placeholder string will be sent."
        )
        return AdminCommandResult(
            "🔁 *Mode Changed → Known*\n"
            "\n"
            f"  {groq_hint}\n"
            "\n"
            "Mention or question in the reply group now triggers a grounded reply.",
            stub_known=True,
        )

    # --- stub unknown ---
    if lowered in {"stub unknown", "unknown"}:
        return AdminCommandResult(
            "🔁 *Mode Changed → Unknown*\n"
            "\n"
            "  Mention or question in the reply group stays silent.\n"
            "  You'll receive a DM notification instead.",
            stub_known=False,
        )

    # --- recent ---
    match = _RECENT_CMD.match(text.strip())
    if match:
        n = int(match.group(2) or "15")
        return AdminCommandResult(
            f"📝 *Recent Group Messages* (last {n})\n"
            f"\n"
            f"{recent_formatter(n)}"
        )

    # --- fallback: Groq or help ---
    return AdminCommandResult("", use_groq=True) if settings.groq_enabled or knowledge_mode else AdminCommandResult(
        "📖 *Available Commands*\n"
        "\n"
        "ping · status · health · errors · recent · help\n"
        "\n"
        "_Send any of these, or set GROQ_API_KEY for free-form questions._"
    )


def admin_dm_reply(text: str, settings: Settings) -> str:
    return handle_admin_dm(
        text,
        settings,
        stub_known=False,
        recent_formatter=lambda _n: "(no group text logged yet this process)",
        ingest_count=0,
        notify_count=0,
    ).reply
