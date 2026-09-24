"""Daily activity report for admin DMs.

Accumulates counters throughout the day and sends a formatted summary
to all admin DMs at a configurable UTC time (hour and minute). The report is NEVER sent
to groups — hardcoded safety check ensures admin-only delivery.

Counters reset after each report is sent and state is persisted to disk.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("unipod_bot")


@dataclass
class DailyCounters:
    """Activity counters accumulated over a 24h period."""

    # Message processing
    messages_ingested: int = 0
    messages_per_group: dict[str, int] = field(default_factory=dict)

    # Reply activity
    questions_answered: int = 0
    questions_forwarded_to_admin: int = 0
    fact_corrections_issued: int = 0
    fact_corrections_suppressed: int = 0  # backed off due to user insistence

    # Graphiti
    graphiti_episodes_created: int = 0
    graphiti_search_count: int = 0
    graphiti_search_failures: int = 0

    # Groq API
    groq_calls: int = 0
    groq_errors: int = 0
    groq_total_latency_ms: float = 0.0

    # PDF
    pdfs_ingested: int = 0
    pdf_pages_processed: int = 0
    pdf_translations: int = 0

    # Health
    health_checks_passed: int = 0
    health_checks_failed: int = 0

    # Errors
    critical_errors: int = 0
    errors: int = 0
    warnings: int = 0

    @property
    def groq_avg_latency_ms(self) -> float:
        return self.groq_total_latency_ms / self.groq_calls if self.groq_calls else 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DailyCounters:
        valid_fields = cls.__dataclass_fields__.keys()
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**filtered)


class DailyReporter:
    """Accumulates daily counters and generates admin reports.

    SAFETY: The report is only sent to admin DMs via AdminInbox.targets().
    The send method explicitly rejects group JIDs.
    """

    def __init__(
        self,
        report_hour_utc: int = 0,
        report_minute_utc: int = 0,
        state_file: Path | str | None = None,
    ) -> None:
        self._report_hour = max(0, min(23, report_hour_utc))
        self._report_minute = max(0, min(59, report_minute_utc))
        self._state_file = Path(state_file) if state_file else None
        self._counters = DailyCounters()
        self._last_report_date: str = ""
        self._report_count = 0

        self._load_state()

        # Startup check: if the scheduled time for today has already passed,
        # mark today as reported so we do NOT send an empty/spurious report on startup.
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        if self._last_report_date != today:
            now_time = (now.hour, now.minute)
            scheduled_time = (self._report_hour, self._report_minute)
            if now_time >= scheduled_time:
                logger.info(
                    "event=daily_report_startup_skip scheduled=%02d:%02d now=%02d:%02d date=%s",
                    self._report_hour,
                    self._report_minute,
                    now.hour,
                    now.minute,
                    today,
                )
                self._last_report_date = today
                self._save_state()

    @property
    def counters(self) -> DailyCounters:
        return self._counters

    @property
    def report_count(self) -> int:
        return self._report_count

    @property
    def report_hour_utc(self) -> int:
        return self._report_hour

    @property
    def report_minute_utc(self) -> int:
        return self._report_minute

    @property
    def last_report_date(self) -> str:
        return self._last_report_date

    def should_report_now(self) -> bool:
        """Check if it's time to send the daily report."""
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        if today == self._last_report_date:
            return False
        return (now.hour, now.minute) >= (self._report_hour, self._report_minute)

    def increment(self, counter_name: str, amount: int = 1) -> None:
        """Increment a counter by name."""
        current = getattr(self._counters, counter_name, None)
        if current is not None and isinstance(current, (int, float)):
            setattr(self._counters, counter_name, current + amount)
            self._save_state()

    def increment_group(self, group_jid: str) -> None:
        """Increment per-group message count."""
        self._counters.messages_per_group[group_jid] = (
            self._counters.messages_per_group.get(group_jid, 0) + 1
        )
        self._save_state()

    def format_report(self, pool_status: str = "") -> str:
        """Generate the daily activity report text."""
        c = self._counters
        now = datetime.now(timezone.utc)

        # Per-group breakdown
        group_lines = []
        for gid, count in sorted(
            c.messages_per_group.items(), key=lambda x: -x[1]
        ):
            short = gid[:20] + "…" if len(gid) > 20 else gid
            group_lines.append(f"  • {short}: {count}")
        group_block = "\n".join(group_lines) if group_lines else "  (none)"

        pool_line = f"  Key pool: {pool_status}\n" if pool_status else ""

        return (
            f"📊 *Daily Bot Report* — {now.strftime('%Y-%m-%d %H:%M UTC')}\n"
            f"\n"
            f"*Messages*\n"
            f"  Ingested: {c.messages_ingested}\n"
            f"  Per group:\n{group_block}\n"
            f"\n"
            f"*Replies*\n"
            f"  Questions answered: {c.questions_answered}\n"
            f"  Forwarded to admin: {c.questions_forwarded_to_admin}\n"
            f"  Fact corrections: {c.fact_corrections_issued}\n"
            f"  Corrections backed off: {c.fact_corrections_suppressed}\n"
            f"\n"
            f"*Graphiti*\n"
            f"  Episodes created: {c.graphiti_episodes_created}\n"
            f"  Searches: {c.graphiti_search_count} ({c.graphiti_search_failures} failed)\n"
            f"\n"
            f"*Groq API*\n"
            f"  Calls: {c.groq_calls} ({c.groq_errors} errors)\n"
            f"  Avg latency: {c.groq_avg_latency_ms:.0f}ms\n"
            f"{pool_line}"
            f"\n"
            f"*Documents*\n"
            f"  PDFs ingested: {c.pdfs_ingested} ({c.pdf_pages_processed} pages)\n"
            f"  Translations: {c.pdf_translations}\n"
            f"\n"
            f"*Health*\n"
            f"  Checks passed: {c.health_checks_passed}\n"
            f"  Checks failed: {c.health_checks_failed}\n"
            f"\n"
            f"*Errors*\n"
            f"  Critical: {c.critical_errors}\n"
            f"  Errors: {c.errors}\n"
            f"  Warnings: {c.warnings}\n"
        )

    def mark_reported(self) -> None:
        """Mark today's report as sent and reset counters."""
        self._last_report_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._counters = DailyCounters()
        self._report_count += 1
        self._save_state()
        logger.info("event=daily_report_sent report_number=%s", self._report_count)

    def _save_state(self) -> None:
        if not self._state_file:
            return
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            state = {
                "last_report_date": self._last_report_date,
                "report_count": self._report_count,
                "counters": self._counters.to_dict(),
            }
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
            tmp.replace(self._state_file)
        except Exception:
            logger.debug("event=daily_report_save_state_failed", exc_info=True)

    def _load_state(self) -> None:
        if not self._state_file or not self._state_file.exists():
            return
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            self._last_report_date = data.get("last_report_date", "")
            self._report_count = data.get("report_count", 0)
            if "counters" in data and isinstance(data["counters"], dict):
                self._counters = DailyCounters.from_dict(data["counters"])
        except Exception:
            logger.debug("event=daily_report_load_state_failed", exc_info=True)

