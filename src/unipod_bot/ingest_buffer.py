"""Batched ingestion buffer — accumulate messages per-group, flush as one episode.

Each Graphiti ``add_episode()`` triggers an internal LLM extraction pipeline
(entity extraction, relationship extraction, dedup, summary updates) —
roughly 4+ Groq calls.  With individual ingestion, 100 messages/hour costs
~400 Groq calls.  By batching N messages into a single conversation-chunk
episode, we pay ~4 calls per batch instead.

Design:
- Messages are buffered per group JID (conversations stay coherent).
- A batch is flushed when either the **time window** expires or the
  **max batch size** is reached.
- ``flush_all()`` is called periodically by a background timer task and
  on graceful shutdown.
- Individual message IDs are still tracked for duplicate prevention on
  reconnect.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from unipod_bot.incoming import IncomingGroupMessage

logger = logging.getLogger("unipod_bot")


@dataclass
class BufferedMessage:
    """Lightweight snapshot of a message waiting for batch ingestion."""

    message_id: str
    sender_jid: str
    chat_jid: str
    text: str
    iso_time: str
    urls: list[str] = field(default_factory=list)


@dataclass
class _GroupBuffer:
    """Per-group accumulator with a creation timestamp for time-based flush."""

    messages: list[BufferedMessage] = field(default_factory=list)
    created_at: float = field(default_factory=time.monotonic)

    @property
    def size(self) -> int:
        return len(self.messages)


FlushCallback = "asyncio.coroutines.coroutine"  # type alias for docs


class IngestBuffer:
    """Accumulates group messages and flushes them as batched episodes.

    Parameters
    ----------
    max_messages:
        Flush a group's buffer once it reaches this many messages.
    interval_seconds:
        Flush all buffers that have been open longer than this (seconds).
    on_flush:
        Async callback invoked with a list of ``BufferedMessage`` for each
        flushed group.  The runtime wires this to ``store.ingest_batch()``.
    """

    def __init__(
        self,
        *,
        max_messages: int = 50,
        interval_seconds: float = 1800.0,
        on_flush: "asyncio.coroutines.coroutine | None" = None,
    ) -> None:
        self._max_messages = max(max_messages, 1)
        self._interval = max(interval_seconds, 10.0)
        self._on_flush = on_flush
        self._buffers: dict[str, _GroupBuffer] = defaultdict(_GroupBuffer)
        self._lock = asyncio.Lock()
        # Stats
        self.total_buffered: int = 0
        self.total_flushed_batches: int = 0
        self.total_flushed_messages: int = 0

    @property
    def pending_count(self) -> int:
        """Total messages waiting across all group buffers."""
        return sum(buf.size for buf in self._buffers.values())

    @property
    def pending_groups(self) -> int:
        """Number of groups with pending messages."""
        return sum(1 for buf in self._buffers.values() if buf.size > 0)

    def add(self, msg: "IncomingGroupMessage") -> bool:
        """Buffer a message for batched ingestion.

        Returns True if the group buffer is now full and should be flushed.
        The caller should await ``flush_group(chat_jid)`` if True.
        """
        if not (msg.text or "").strip() or not msg.message_id:
            return False

        entry = BufferedMessage(
            message_id=msg.message_id,
            sender_jid=msg.sender_jid,
            chat_jid=msg.chat_jid,
            text=msg.text,
            iso_time=msg.iso_time,
            urls=list(getattr(msg, "urls", None) or []),
        )
        buf = self._buffers[msg.chat_jid]
        buf.messages.append(entry)
        self.total_buffered += 1

        if buf.size >= self._max_messages:
            return True  # signal caller to flush
        return False

    async def flush_group(self, chat_jid: str) -> int:
        """Flush a single group's buffer.  Returns count of messages flushed."""
        async with self._lock:
            buf = self._buffers.get(chat_jid)
            if not buf or buf.size == 0:
                return 0
            messages = list(buf.messages)
            buf.messages.clear()
            buf.created_at = time.monotonic()

        count = len(messages)
        if self._on_flush is not None:
            try:
                await self._on_flush(messages)
                self.total_flushed_batches += 1
                self.total_flushed_messages += count
                logger.info(
                    "event=ingest_batch_flushed group=%s messages=%s",
                    chat_jid,
                    count,
                )
            except Exception:
                logger.exception(
                    "event=ingest_batch_flush_failed group=%s messages=%s",
                    chat_jid,
                    count,
                )
                # Re-buffer the messages so they aren't lost, capped to avoid runaway growth
                async with self._lock:
                    rebuf = self._buffers[chat_jid]
                    combined = messages + rebuf.messages
                    rebuf.messages = combined[-self._max_messages * 2 :]
                return 0
        return count

    async def flush_all(self) -> int:
        """Flush all group buffers that have exceeded the time window.

        Returns total messages flushed across all groups.
        """
        now = time.monotonic()
        groups_to_flush: list[str] = []

        for jid, buf in list(self._buffers.items()):
            if buf.size == 0:
                continue
            age = now - buf.created_at
            if age >= self._interval:
                groups_to_flush.append(jid)

        total = 0
        for jid in groups_to_flush:
            total += await self.flush_group(jid)
        return total

    async def flush_all_now(self) -> int:
        """Flush ALL group buffers regardless of age (for shutdown)."""
        groups_to_flush = [
            jid for jid, buf in list(self._buffers.items()) if buf.size > 0
        ]
        total = 0
        for jid in groups_to_flush:
            total += await self.flush_group(jid)
        return total

    def status_line(self) -> str:
        """One-line status for heartbeat / admin commands."""
        return (
            f"pending={self.pending_count} groups={self.pending_groups} "
            f"flushed_batches={self.total_flushed_batches} "
            f"flushed_msgs={self.total_flushed_messages}"
        )
