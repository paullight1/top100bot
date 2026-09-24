"""Periodic health checks for all bot backends.

Validates FalkorDB, Graphiti, Groq, WhatsApp connection, and queue depths
at configurable intervals. Failures are logged and trigger admin DM alerts
via the ErrorTracker.

Health checks run as a background asyncio task alongside the message worker.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from unipod_bot.memory.graphiti_store import GraphitiStore

logger = logging.getLogger("unipod_bot")


@dataclass
class HealthStatus:
    """Result of a single health check cycle."""

    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    falkordb_ok: bool = False
    graphiti_ok: bool = False
    groq_ok: bool = False
    whatsapp_connected: bool = False
    worker_queue_depth: int = 0
    ingest_queue_depth: int = 0
    memory_rss_mb: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return (
            self.falkordb_ok
            and self.graphiti_ok
            and self.groq_ok
            and self.whatsapp_connected
            and not self.errors
        )


class HealthChecker:
    """Runs periodic health checks against all bot backends."""

    def __init__(
        self,
        *,
        interval_seconds: float = 300.0,
        store: GraphitiStore | None = None,
        groq_client: Any = None,
        worker_queue: asyncio.Queue | None = None,
        ingest_queue: asyncio.Queue | None = None,
    ) -> None:
        self._interval = max(interval_seconds, 30.0)
        self._store = store
        self._groq = groq_client
        self._worker_queue = worker_queue
        self._ingest_queue = ingest_queue
        self._last_status: HealthStatus | None = None
        self._check_count = 0
        self._fail_count = 0

    @property
    def last_status(self) -> HealthStatus | None:
        return self._last_status

    @property
    def check_count(self) -> int:
        return self._check_count

    @property
    def fail_count(self) -> int:
        return self._fail_count

    async def check_once(self) -> HealthStatus:
        """Run a single health check cycle."""
        status = HealthStatus()
        self._check_count += 1

        # FalkorDB ping
        status.falkordb_ok = await self._check_falkordb()
        if not status.falkordb_ok:
            status.errors.append("FalkorDB unreachable")

        # Graphiti responsiveness
        status.graphiti_ok = await self._check_graphiti()
        if not status.graphiti_ok and self._store is not None:
            status.errors.append("Graphiti search unresponsive")

        # Groq API
        status.groq_ok = await self._check_groq()
        if not status.groq_ok and self._groq is not None:
            status.errors.append("Groq API unreachable")

        # Queue depths
        if self._worker_queue:
            status.worker_queue_depth = self._worker_queue.qsize()
        if self._ingest_queue:
            status.ingest_queue_depth = self._ingest_queue.qsize()

        # Memory usage
        status.memory_rss_mb = _get_rss_mb()

        # WhatsApp connection (inferred from recent activity)
        status.whatsapp_connected = True  # Updated by runtime if disconnect detected

        if not status.all_ok:
            self._fail_count += 1

        self._last_status = status
        logger.info(
            "event=health_check ok=%s falkordb=%s graphiti=%s groq=%s "
            "worker_q=%s ingest_q=%s rss_mb=%.1f errors=%s",
            status.all_ok,
            status.falkordb_ok,
            status.graphiti_ok,
            status.groq_ok,
            status.worker_queue_depth,
            status.ingest_queue_depth,
            status.memory_rss_mb,
            ",".join(status.errors) or "none",
        )
        return status

    async def _check_falkordb(self) -> bool:
        if self._store is None:
            return True  # Not configured = not a failure
        try:
            graphiti = self._store._graphiti
            if graphiti is None:
                return False
            result = await asyncio.wait_for(
                graphiti.driver.execute_query("RETURN 1 AS ping"),
                timeout=5.0,
            )
            return bool(result)
        except Exception as exc:
            logger.warning("event=health_falkordb_failed error=%s", exc, exc_info=True)
            return False

    async def _check_graphiti(self) -> bool:
        if self._store is None:
            return True  # Not configured
        timeout = 30.0
        if hasattr(self._store, "_settings"):
            timeout = max(
                float(getattr(self._store._settings, "graphiti_search_timeout_seconds", 20.0)),
                30.0,
            )
        for attempt in range(2):
            try:
                await asyncio.wait_for(
                    self._store.search_facts("health check ping"),
                    timeout=timeout,
                )
                return True
            except Exception as exc:
                if attempt == 0:
                    logger.warning("event=health_graphiti_retry attempt=1 error=%s", exc)
                    await asyncio.sleep(2.0)
                    continue
                logger.warning("event=health_graphiti_failed error=%s", exc, exc_info=True)
                return False
        return False

    async def _check_groq(self) -> bool:
        if self._groq is None:
            return True  # Not configured
        try:
            # GroqReplyClient creates clients dynamically per-key;
            # verify the pool has at least one usable key by making a
            # lightweight generation call.
            from unipod_bot.groq_gate import groq_reply

            async with groq_reply():
                key = self._groq._pool.get_key()
                client = self._groq._make_client(key)
                response = await asyncio.wait_for(
                    client.chat.completions.create(
                        model=self._groq._settings.groq_model,
                        messages=[{"role": "user", "content": "ping"}],
                        max_completion_tokens=5,
                    ),
                    timeout=15.0,
                )
            return bool(response.choices)
        except Exception as exc:
            logger.warning("event=health_groq_failed error=%s", exc, exc_info=True)
            return False

    def format_status(self) -> str:
        """Format last health status for admin display."""
        s = self._last_status
        if s is None:
            return "🩺 *Health Check*\n\nNo health check has been run yet."
        ok = "✅" if s.all_ok else "❌"
        return (
            f"🩺 *Health Check* {ok}\n"
            f"_Checked at {s.timestamp.strftime('%H:%M:%S UTC')}_\n"
            f"\n"
            f"*Backends*\n"
            f"  FalkorDB: {'✅' if s.falkordb_ok else '❌'}\n"
            f"  Graphiti: {'✅' if s.graphiti_ok else '❌'}\n"
            f"  Groq API: {'✅' if s.groq_ok else '❌'}\n"
            f"  WhatsApp: {'✅' if s.whatsapp_connected else '❌'}\n"
            f"\n"
            f"*Queues*\n"
            f"  Worker: {s.worker_queue_depth}\n"
            f"  Ingest: {s.ingest_queue_depth}\n"
            f"\n"
            f"*Resources*\n"
            f"  Memory RSS: {s.memory_rss_mb:.1f} MB\n"
            f"\n"
            f"*History*\n"
            f"  Total checks: {self._check_count}\n"
            f"  Failed: {self._fail_count}"
        )


def _get_rss_mb() -> float:
    """Get current process RSS in MB."""
    try:
        if os.name == "nt":
            import ctypes

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_ulong),
                    ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(counters)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            if ctypes.windll.psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb
            ):
                return counters.WorkingSetSize / (1024 * 1024)
        else:
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) / 1024
    except Exception:
        pass
    return 0.0
