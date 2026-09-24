"""Groq API key pool with round-robin rotation and per-key cooldown.

Usage:
    pool = GroqKeyPool(["gsk_key1", "gsk_key2", "gsk_key3"])
    key = pool.get_key()          # next available key
    pool.mark_rate_limited(key, retry_after=600)  # park exhausted key

When a key returns 429, call ``mark_rate_limited`` with the ``retry-after``
value from Groq's response headers.  The key will be skipped until the
cooldown expires, and the pool will rotate to the next available one.

When all keys are exhausted, call ``pause_until_reset()`` to park the entire
pool until midnight UTC (when Groq's daily token limits reset).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("unipod_bot")


class GroqKeyPoolExhausted(RuntimeError):
    """All API keys are currently rate-limited."""


def _seconds_until_midnight_utc() -> float:
    """Seconds from now until the next midnight UTC (Groq daily limit reset)."""
    now = datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (tomorrow - now).total_seconds()


class GroqKeyPool:
    """Thread-safe round-robin key pool with per-key cooldown."""

    def __init__(self, keys: list[str] | tuple[str, ...]) -> None:
        if not keys:
            raise ValueError("GroqKeyPool requires at least one API key")
        self._keys: tuple[str, ...] = tuple(keys)
        # Cooldown expiry timestamps (monotonic seconds); 0 = available.
        self._cooldowns: dict[str, float] = {k: 0.0 for k in self._keys}
        self._index: int = 0
        self._lock = threading.Lock()
        # Pool-wide pause (monotonic timestamp). 0 = not paused.
        self._paused_until: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def total(self) -> int:
        return len(self._keys)

    @property
    def is_paused(self) -> bool:
        """True if the pool is paused (all keys exhausted, waiting for daily reset)."""
        with self._lock:
            if self._paused_until <= 0:
                return False
            if time.monotonic() >= self._paused_until:
                # Auto-resume: pause has expired
                self._paused_until = 0.0
                # Clear per-key cooldowns too
                for k in self._keys:
                    self._cooldowns[k] = 0.0
                logger.info("event=groq_pool_auto_resumed keys=%d", len(self._keys))
                return False
            return True

    @property
    def available_count(self) -> int:
        if self.is_paused:
            return 0
        now = time.monotonic()
        with self._lock:
            return sum(1 for k in self._keys if self._cooldowns[k] <= now)

    def get_key(self) -> str:
        """Return the next available key (round-robin, skip cooled-down).

        Raises ``GroqKeyPoolExhausted`` if every key is currently rate-limited
        or the pool is paused until daily reset.
        """
        if self.is_paused:
            raise GroqKeyPoolExhausted(
                f"All {len(self._keys)} Groq API keys exhausted. "
                f"Pool paused until daily limit reset."
            )
        now = time.monotonic()
        with self._lock:
            n = len(self._keys)
            for _ in range(n):
                key = self._keys[self._index % n]
                self._index = (self._index + 1) % n
                if self._cooldowns[key] <= now:
                    return key
        # All keys exhausted — find the soonest recovery.
        soonest = min(self._cooldowns.values())
        wait = max(soonest - now, 0)
        raise GroqKeyPoolExhausted(
            f"All {len(self._keys)} Groq API keys are rate-limited. "
            f"Next key available in ~{wait:.0f}s."
        )

    def mark_rate_limited(
        self, key: str, retry_after: float = 60.0
    ) -> None:
        """Park *key* for *retry_after* seconds."""
        with self._lock:
            self._cooldowns[key] = time.monotonic() + max(retry_after, 1.0)
        # Mask the key in the log (show first 8 chars).
        masked = key[:8] + "…" if len(key) > 8 else key
        logger.warning(
            "event=groq_key_rate_limited key=%s cooldown_s=%.0f available=%d/%d",
            masked,
            retry_after,
            self.available_count,
            self.total,
        )

    def pause_until_reset(self) -> float:
        """Pause the entire pool until midnight UTC (Groq daily limit reset).

        Returns the number of seconds until the pool resumes.
        """
        wait = _seconds_until_midnight_utc()
        with self._lock:
            self._paused_until = time.monotonic() + wait
        logger.critical(
            "event=groq_pool_paused all_keys_exhausted keys=%d resume_in_s=%.0f",
            len(self._keys),
            wait,
        )
        return wait

    def resume(self) -> None:
        """Manually resume the pool (e.g. admin command)."""
        with self._lock:
            self._paused_until = 0.0
            for k in self._keys:
                self._cooldowns[k] = 0.0
        logger.info("event=groq_pool_manual_resume keys=%d", len(self._keys))

    def peek_key(self) -> str:
        """Return the current key without advancing the index (for diagnostics)."""
        with self._lock:
            return self._keys[self._index % len(self._keys)]

    def status_line(self) -> str:
        """Human-readable status for heartbeat / daily report."""
        if self.is_paused:
            return f"PAUSED (0/{self.total} keys) — waiting for daily reset"
        return f"{self.available_count}/{self.total} keys available"
