"""Error tracking with severity classification and rate-limited admin alerts.

Severity levels:
- CRITICAL: Immediate admin DM — bot crash, WhatsApp disconnect, FalkorDB down,
  persistent Groq failures. Rate-limited to max 1 DM per error type per cooldown.
- ERROR: Batched into daily report — individual Groq timeouts, Graphiti ingest
  failures, PDF parse failures.
- WARNING: Logged only — queue backpressure, slow queries.

Integrates with Python's logging module via a custom handler.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger("unipod_bot")


class Severity(str, Enum):
    CRITICAL = "critical"
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class TrackedError:
    """A recorded error event."""

    severity: Severity
    error_type: str
    message: str
    timestamp: float  # time.monotonic()
    details: str = ""


class ErrorTracker:
    """Classifies errors by severity and rate-limits critical admin alerts.

    SAFETY: Admin alerts are sent exclusively via the admin_notify callback
    which is bound to AdminInbox.targets() — never to groups.
    """

    def __init__(
        self,
        cooldown_seconds: float = 600.0,
        admin_notify: Callable[[str], Any] | None = None,
    ) -> None:
        self._cooldown = max(cooldown_seconds, 60.0)
        self._admin_notify = admin_notify
        self._last_alert_time: dict[str, float] = defaultdict(float)
        self._errors: list[TrackedError] = []
        self._counts: dict[Severity, int] = defaultdict(int)

    @property
    def total_critical(self) -> int:
        return self._counts[Severity.CRITICAL]

    @property
    def total_errors(self) -> int:
        return self._counts[Severity.ERROR]

    @property
    def total_warnings(self) -> int:
        return self._counts[Severity.WARNING]

    def track(
        self,
        severity: Severity,
        error_type: str,
        message: str,
        details: str = "",
    ) -> TrackedError:
        """Record an error and optionally trigger an admin alert."""
        now = time.monotonic()
        error = TrackedError(
            severity=severity,
            error_type=error_type,
            message=message,
            timestamp=now,
            details=details,
        )
        self._errors.append(error)
        self._counts[severity] += 1

        # Keep only last 500 errors in memory
        if len(self._errors) > 500:
            self._errors = self._errors[-250:]

        if severity == Severity.CRITICAL:
            self._maybe_alert(error, now)

        logger.log(
            logging.CRITICAL if severity == Severity.CRITICAL else (
                logging.ERROR if severity == Severity.ERROR else logging.WARNING
            ),
            "event=tracked_error severity=%s type=%s msg=%s",
            severity.value,
            error_type,
            message,
        )
        return error

    def _maybe_alert(self, error: TrackedError, now: float) -> None:
        """Send admin alert if cooldown has passed for this error type."""
        last = self._last_alert_time[error.error_type]
        if now - last < self._cooldown:
            logger.debug(
                "event=alert_suppressed type=%s cooldown_remaining=%.0f",
                error.error_type,
                self._cooldown - (now - last),
            )
            return

        self._last_alert_time[error.error_type] = now
        body = (
            f"🚨 *CRITICAL ALERT*\n"
            f"Type: {error.error_type}\n"
            f"Message: {error.message}\n"
        )
        if error.details:
            body += f"Details: {error.details[:500]}\n"

        if self._admin_notify is not None:
            try:
                self._admin_notify(body)
            except Exception:
                logger.exception("event=alert_send_failed type=%s", error.error_type)

    def get_recent(self, severity: Severity | None = None, limit: int = 20) -> list[TrackedError]:
        """Get recent errors, optionally filtered by severity."""
        filtered = self._errors if severity is None else [
            e for e in self._errors if e.severity == severity
        ]
        return filtered[-limit:]

    def format_summary(self) -> str:
        """Format error summary for admin display."""
        total = sum(self._counts.values())
        if total == 0:
            return "🛡️ *Error Summary*\n\nNo errors recorded this session. ✅"
        return (
            "🛡️ *Error Summary*\n"
            "\n"
            f"  🚨 Critical: {self._counts[Severity.CRITICAL]}\n"
            f"  ❗ Errors: {self._counts[Severity.ERROR]}\n"
            f"  ⚠️ Warnings: {self._counts[Severity.WARNING]}\n"
            f"  Total: {total}"
        )

    def format_recent(self, limit: int = 10) -> str:
        """Format recent errors for admin display."""
        recent = self.get_recent(limit=limit)
        if not recent:
            return "🛡️ *Recent Errors*\n\nNo errors recorded."
        lines = [f"🛡️ *Recent Errors* (last {len(recent)})\n"]
        for e in reversed(recent):
            icon = {"critical": "🚨", "error": "❗", "warning": "⚠️"}.get(
                e.severity.value, "•"
            )
            lines.append(f"\n{icon} *{e.error_type}*")
            lines.append(f"  {e.message[:200]}")
            if e.details:
                lines.append(f"  _Details:_ {e.details[:150]}")
        return "\n".join(lines)


class ErrorTrackingHandler(logging.Handler):
    """Logging handler that intercepts ERROR+ logs and tracks them.

    Attach to the unipod_bot logger to automatically capture errors
    without modifying every try/except block.
    """

    # Map specific log messages to error types for classification
    _CRITICAL_PATTERNS = (
        "graphiti_start_failed",
        "whatsapp_disconnect",
        "falkordb_unreachable",
        "runtime_close_failed",
    )

    def __init__(self, tracker: ErrorTracker) -> None:
        super().__init__(level=logging.ERROR)
        self._tracker = tracker
        self._inside_emit = False  # re-entrancy guard

    def emit(self, record: logging.LogRecord) -> None:
        # Guard against recursive log loop:
        # emit() → tracker.track() → logger.error("tracked_error") → emit() …
        if self._inside_emit:
            return
        self._inside_emit = True
        try:
            message = self.format(record) if self.formatter else record.getMessage()
            if "event=tracked_error" in message or record.name == "unipod_bot.error_tracker":
                return
            error_type = self._classify_type(message)
            severity = self._classify_severity(record, message)
            self._tracker.track(
                severity=severity,
                error_type=error_type,
                message=message[:200],
                details=str(record.exc_info[1]) if record.exc_info else "",
            )
        except Exception:
            pass  # Never let tracking break the application
        finally:
            self._inside_emit = False

    def _classify_severity(self, record: logging.LogRecord, message: str) -> Severity:
        if record.levelno >= logging.CRITICAL:
            return Severity.CRITICAL
        msg_lower = message.lower()
        for pattern in self._CRITICAL_PATTERNS:
            if pattern in msg_lower:
                return Severity.CRITICAL
        return Severity.ERROR

    def _classify_type(self, message: str) -> str:
        """Extract error type from structured log message."""
        msg = message.lower()
        if "groq" in msg:
            return "groq_error"
        if "graphiti" in msg:
            return "graphiti_error"
        if "falkordb" in msg:
            return "falkordb_error"
        if "whatsapp" in msg or "neonize" in msg:
            return "whatsapp_error"
        if "pdf" in msg:
            return "pdf_error"
        if "queue" in msg:
            return "queue_error"
        return "unknown_error"
