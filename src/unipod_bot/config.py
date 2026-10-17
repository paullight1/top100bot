from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

_TRUE = {"1", "true", "yes", "on"}


def parse_bool(raw: str | None, default: bool = False) -> bool:
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in _TRUE


def parse_phone_list(*raw_values: str | None) -> tuple[str, ...]:
    """International digits only. Accepts comma/space/semicolon lists and a single ADMIN_PHONE."""
    phones: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        if not raw:
            continue
        for part in raw.replace(";", ",").split(","):
            digits = "".join(ch for ch in part if ch.isdigit())
            if not digits or digits in seen:
                continue
            seen.add(digits)
            phones.append(digits)
    return tuple(phones)


def parse_feedback_contacts(raw: str | None) -> tuple[tuple[str, str], ...]:
    """Parse FEEDBACK_CONTACTS env: 'phone:Name,phone:Name' or just 'phone,phone'."""
    contacts: list[tuple[str, str]] = []
    if not raw:
        return ()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            phone, name = part.split(":", 1)
        else:
            phone, name = part, ""
        digits = "".join(ch for ch in phone if ch.isdigit())
        display = name.strip() or digits
        if digits:
            contacts.append((digits, display))
    return tuple(contacts)


def format_feedback_contacts_str(contacts: tuple[tuple[str, str], ...]) -> str:
    """Format feedback contacts for inclusion in LLM prompts."""
    if not contacts:
        return ""
    parts = []
    for phone, name in contacts:
        if name and name != phone:
            parts.append(f"@{phone} ({name})")
        else:
            parts.append(f"@{phone}")
    return ", ".join(parts)



@dataclass(frozen=True)
class TopicHelper:
    """A domain-specific helper contact."""

    topic: str     # e.g. "bot", "programs", "events"
    phone: str     # digits only
    name: str      # display name


def parse_helpers_config(raw: str | None) -> tuple[TopicHelper, ...]:
    """Parse HELPERS_CONFIG env: 'topic:phone:Name,topic:phone:Name'.

    Each entry must have at least topic and phone. Name is optional.
    Example: 'events:COUNTRYCODEDIGINSONLY:Admin'
    """
    helpers: list[TopicHelper] = []
    if not raw:
        return ()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        segments = part.split(":", 2)
        if len(segments) < 2:
            continue
        topic = segments[0].strip().lower()
        if not topic:
            continue
        phone = "".join(ch for ch in segments[1] if ch.isdigit())
        if not phone:
            continue
        name = segments[2].strip() if len(segments) > 2 else ""
        if not name:
            name = phone
        helpers.append(TopicHelper(topic=topic, phone=phone, name=name))
    return tuple(helpers)


def format_helpers_str(helpers: tuple[TopicHelper, ...]) -> str:
    """Format helpers directory for inclusion in LLM prompts.

    Returns a multi-line string describing who to contact per topic.
    Returns empty string when no helpers are configured.
    """
    if not helpers:
        return ""
    lines: list[str] = []
    for h in helpers:
        contact = f"@{h.phone} ({h.name})" if h.name and h.name != h.phone else f"@{h.phone}"
        lines.append(f"  {h.topic.capitalize()}: {contact}")
    return "\n".join(lines)


def parse_jid_list(raw: str | None) -> tuple[str, ...]:
    jids: list[str] = []
    seen: set[str] = set()
    if not raw:
        return ()
    for part in raw.replace(";", ",").split(","):
        token = part.strip().lower()
        if not token or token in seen:
            continue
        seen.add(token)
        jids.append(token)
    return tuple(jids)


def parse_group_jids(*sources: str | None) -> tuple[str, ...]:
    """Parse comma- or whitespace-separated group JIDs into normalized list."""
    out: list[str] = []
    seen: set[str] = set()
    for source in sources:
        if not source:
            continue
        for token in re.split(r"[\s,;]+", source.strip()):
            t = token.strip()
            if not t:
                continue
            if t in {"*", "all"}:
                return ("*",)
            if "@" not in t and "".join(ch for ch in t if ch.isdigit()) == t:
                t = f"{t}@g.us"
            key = t.lower()
            if key not in seen:
                seen.add(key)
                out.append(t)
    return tuple(out)


def resolve_log_level(*, debug: bool, log_level_name: str) -> int:
    if debug:
        return logging.DEBUG
    return int(getattr(logging, log_level_name.upper(), logging.INFO))


def parse_daily_report_time(
    val: str | None, default_hour: int = 0, default_minute: int = 0
) -> tuple[int, int]:
    """Parse HH:MM or HH string into (hour, minute) UTC."""
    if not val:
        return default_hour, default_minute
    cleaned = val.strip()
    if ":" in cleaned:
        parts = cleaned.split(":", 1)
        try:
            h = int(parts[0].strip())
            m = int(parts[1].strip())
            return max(0, min(23, h)), max(0, min(59, m))
        except (ValueError, TypeError):
            return default_hour, default_minute
    try:
        h = int(cleaned)
        return max(0, min(23, h)), default_minute
    except (ValueError, TypeError):
        return default_hour, default_minute



@dataclass(frozen=True)
class Settings:
    whatsapp_session_path: Path
    test_group_jid: str | None
    debug: bool
    log_level: int
    falkordb_host: str
    falkordb_port: int
    falkordb_database: str
    falkordb_username: str | None
    falkordb_password: str | None
    graphiti_group_id: str
    admin_phones: tuple[str, ...]
    top100_api_base_url: str = "https://www.top100afl.com"
    admin_jids: tuple[str, ...] = ()
    test_group_jids: tuple[str, ...] = ()
    bot_phone: str | None = None
    groq_api_keys: tuple[str, ...] = ()
    groq_model: str = "openai/gpt-oss-120b"
    groq_timeout_seconds: float = 45.0
    openai_api_key: str | None = None
    openai_embedding_model: str = "text-embedding-3-small"
    semaphore_limit: int = 2
    retrieval_min_facts: int = 1
    retrieval_min_score: float = 0.0
    graphiti_search_timeout_seconds: float = 20.0
    graphiti_wanted: bool = True
    heartbeat_seconds: float = 60.0
    short_term_max_messages: int = 50
    queue_maxsize: int = 1000
    feedback_contacts: tuple[tuple[str, str], ...] = ()
    retrieval_min_correction_facts: int = 2
    health_check_interval_seconds: float = 300.0
    health_check_initial_delay_seconds: float = 120.0
    daily_report_hour_utc: int = 0
    daily_report_minute_utc: int = 0
    critical_alert_cooldown_seconds: float = 600.0
    pdf_ingestion_enabled: bool = True
    pdf_translation_enabled: bool = False
    pdf_translation_lang: str | None = None
    ingest_batch_interval_seconds: float = 1800.0
    ingest_batch_max_messages: int = 15
    retrieval_num_results: int = 15
    tts_enabled: bool = True
    tts_cache_enabled: bool = True
    tts_cache_dir: Path = Path("data/tts_cache")
    tts_cache_max_files: int = 1000
    piper_tts_url: str | None = None
    piper_tts_api_key: str | None = None
    piper_tts_voice: str = "en_US-lessac-medium"
    tts_timeout_seconds: float = 30.0
    helpers: tuple[TopicHelper, ...] = ()
    knowledge_library_enabled: bool = True
    knowledge_group_jid: str | None = None
    knowledge_db_path: Path | None = None

    def __post_init__(self) -> None:
        if self.knowledge_group_jid:
            group = self.knowledge_group_jid.strip().lower()
            if not re.fullmatch(r"[\w-]+@g\.us", group):
                raise ValueError("KNOWLEDGE_GROUP_JID must be one exact WhatsApp group JID.")
            if not self.knowledge_library_enabled:
                raise ValueError("The knowledge group requires KNOWLEDGE_LIBRARY_ENABLED=true.")
            object.__setattr__(self, "knowledge_group_jid", group)
        if not self.test_group_jids and self.test_group_jid:
            parsed = parse_group_jids(self.test_group_jid)
            object.__setattr__(self, "test_group_jids", parsed)

    @property
    def admin_phone(self) -> str | None:
        return self.admin_phones[0] if self.admin_phones else None

    @property
    def groq_api_key(self) -> str | None:
        """First key — for backward compat with code that needs a single key."""
        return self.groq_api_keys[0] if self.groq_api_keys else None

    @property
    def groq_enabled(self) -> bool:
        return len(self.groq_api_keys) > 0

    @property
    def graphiti_enabled(self) -> bool:
        """Write-only Graphiti ingest needs Groq (extraction) and OpenAI (embeddings)."""
        return self.graphiti_wanted and bool(self.groq_api_keys and self.openai_api_key)

    @property
    def tts_configured(self) -> bool:
        """True if TTS is enabled and a Piper TTS URL is provided."""
        return self.tts_enabled and bool(self.piper_tts_url)


def _optional_str(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def parse_groq_keys(raw: str | None) -> tuple[str, ...]:
    """Parse comma-separated Groq API keys. Filters blanks, deduplicates."""
    if not raw:
        return ()
    keys: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        key = part.strip()
        if key and key not in seen:
            seen.add(key)
            keys.append(key)
    return tuple(keys)


_GROQ_MODEL_ALIASES = {
    "gpt-oss-120b": "openai/gpt-oss-120b",
    "gpt-oss-20b": "openai/gpt-oss-20b",
}


def resolve_groq_model(raw: str | None) -> str:
    model = (raw or "").strip() or "openai/gpt-oss-120b"
    return _GROQ_MODEL_ALIASES.get(model, model)


def normalize_graphiti_group_id(raw: str | None) -> str:
    """Graphiti allows only ASCII letters, digits, underscore, and hyphen (no colon)."""
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "_", (raw or "").strip())
    return cleaned or "top100_afl"


def load_settings(*, dotenv: bool = True) -> Settings:
    if dotenv:
        load_dotenv()
    debug = parse_bool(os.getenv("DEBUG"), default=False)
    level_name = os.getenv("LOG_LEVEL", "INFO")
    session = Path(os.getenv("WHATSAPP_SESSION_PATH", "data/session.db"))
    admin_jids = parse_jid_list(os.getenv("ADMIN_JIDS"))
    graphiti_group_id = normalize_graphiti_group_id(
        os.getenv("GRAPHITI_GROUP_ID", "top100_afl")
    )
    falkor_db = os.getenv("FALKORDB_DATABASE", "").strip()
    test_group_raw = _optional_str("TEST_GROUP_JID")
    test_groups_raw = _optional_str("TEST_GROUP_JIDS")
    group_jids = parse_group_jids(test_groups_raw, test_group_raw)
    first_group = group_jids[0] if (group_jids and group_jids[0] != "*") else test_group_raw
    report_time_raw = os.getenv("DAILY_REPORT_TIME") or os.getenv("DAILY_REPORT_TIME_UTC")
    if report_time_raw:
        report_hour, report_minute = parse_daily_report_time(report_time_raw)
    else:
        report_hour = int(os.getenv("DAILY_REPORT_HOUR_UTC", "0") or "0")
        report_minute = int(os.getenv("DAILY_REPORT_MINUTE_UTC", "0") or "0")
    return Settings(
        whatsapp_session_path=session,
        test_group_jid=first_group,
        debug=debug,
        log_level=resolve_log_level(debug=debug, log_level_name=level_name),
        falkordb_host=os.getenv("FALKORDB_HOST", "localhost").strip() or "localhost",
        falkordb_port=int(os.getenv("FALKORDB_PORT", "6379")),
        falkordb_database=falkor_db or graphiti_group_id,
        falkordb_username=_optional_str("FALKORDB_USERNAME"),
        falkordb_password=_optional_str("FALKORDB_PASSWORD"),
        graphiti_group_id=graphiti_group_id,
        admin_phones=parse_phone_list(
            os.getenv("ADMIN_PHONES"),
            os.getenv("ADMIN_PHONE"),
        ),
        top100_api_base_url=(
            os.getenv("TOP100_API_BASE_URL", "https://www.top100afl.com").strip().rstrip("/")
            or "https://www.top100afl.com"
        ),
        admin_jids=admin_jids,
        knowledge_library_enabled=parse_bool(os.getenv("KNOWLEDGE_LIBRARY_ENABLED"), default=True),
        knowledge_group_jid=_optional_str("KNOWLEDGE_GROUP_JID"),
        knowledge_db_path=Path(os.environ["KNOWLEDGE_DB_PATH"]) if _optional_str("KNOWLEDGE_DB_PATH") else None,
        test_group_jids=group_jids,
        bot_phone=_optional_str("BOT_PHONE"),
        groq_api_keys=parse_groq_keys(os.getenv("GROQ_API_KEY")),
        groq_model=resolve_groq_model(os.getenv("GROQ_MODEL")),
        groq_timeout_seconds=float(os.getenv("GROQ_TIMEOUT_SECONDS", "45") or "45"),
        openai_api_key=_optional_str("OPENAI_API_KEY"),
        openai_embedding_model=os.getenv(
            "OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"
        ).strip()
        or "text-embedding-3-small",
        semaphore_limit=int(os.getenv("SEMAPHORE_LIMIT", "2") or "2"),
        retrieval_min_facts=int(os.getenv("RETRIEVAL_MIN_FACTS", "1") or "1"),
        retrieval_min_score=float(os.getenv("RETRIEVAL_MIN_SCORE", "0") or "0"),
        graphiti_search_timeout_seconds=float(
            os.getenv("GRAPHITI_SEARCH_TIMEOUT_SECONDS", "20") or "20"
        ),
        graphiti_wanted=parse_bool(os.getenv("GRAPHITI_ENABLED"), default=True),
        heartbeat_seconds=float(os.getenv("HEARTBEAT_SECONDS", "60") or "60"),
        short_term_max_messages=int(os.getenv("SHORT_TERM_MAX_MESSAGES", "50") or "50"),
        queue_maxsize=int(os.getenv("QUEUE_MAXSIZE", "1000") or "1000"),
        feedback_contacts=parse_feedback_contacts(os.getenv("FEEDBACK_CONTACTS")),
        retrieval_min_correction_facts=int(
            os.getenv("RETRIEVAL_MIN_CORRECTION_FACTS", "2") or "2"
        ),
        health_check_interval_seconds=float(
            os.getenv("HEALTH_CHECK_INTERVAL_SECONDS", "300") or "300"
        ),
        health_check_initial_delay_seconds=float(
            os.getenv("HEALTH_CHECK_INITIAL_DELAY_SECONDS", "120") or "120"
        ),
        daily_report_hour_utc=report_hour,
        daily_report_minute_utc=report_minute,
        critical_alert_cooldown_seconds=float(
            os.getenv("CRITICAL_ALERT_COOLDOWN_SECONDS", "600") or "600"
        ),
        pdf_ingestion_enabled=parse_bool(
            os.getenv("PDF_INGESTION_ENABLED"), default=True
        ),
        pdf_translation_enabled=parse_bool(
            os.getenv("PDF_TRANSLATION_ENABLED"), default=False
        ),
        pdf_translation_lang=_optional_str("PDF_TRANSLATION_LANG"),
        ingest_batch_interval_seconds=float(
            os.getenv("INGEST_BATCH_INTERVAL_SECONDS", "1800") or "1800"
        ),
        ingest_batch_max_messages=int(
            os.getenv("INGEST_BATCH_MAX_MESSAGES", "50") or "50"
        ),
        retrieval_num_results=int(
            os.getenv("RETRIEVAL_NUM_RESULTS", "15") or "15"
        ),
        tts_enabled=parse_bool(os.getenv("TTS_ENABLED"), default=True),
        tts_cache_enabled=parse_bool(os.getenv("TTS_CACHE_ENABLED"), default=True),
        tts_cache_dir=Path(os.getenv("TTS_CACHE_DIR", "data/tts_cache")),
        tts_cache_max_files=int(os.getenv("TTS_CACHE_MAX_FILES", "1000") or "1000"),
        piper_tts_url=_optional_str("PIPER_TTS_URL"),
        piper_tts_api_key=_optional_str("PIPER_TTS_API_KEY")
        or _optional_str("PIPER_API_KEY"),
        piper_tts_voice=os.getenv("PIPER_TTS_VOICE", "en_US-lessac-medium").strip()
        or "en_US-lessac-medium",
        tts_timeout_seconds=float(
            os.getenv("TTS_TIMEOUT_SECONDS", "30") or "30"
        ),
        helpers=parse_helpers_config(os.getenv("HELPERS_CONFIG")),
    )
