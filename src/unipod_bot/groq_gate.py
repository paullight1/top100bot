from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

_lock = asyncio.Lock()
_reply_waiters = 0


@asynccontextmanager
async def groq_reply() -> AsyncIterator[None]:
    """Exclusive Groq slot for group/DM answers.

    Ingest waiters skip acquiring the lock while a reply is queued. An ingest
    call that already holds the lock is not cancelled.
    """
    global _reply_waiters
    _reply_waiters += 1
    try:
        async with _lock:
            yield
    finally:
        _reply_waiters -= 1


@asynccontextmanager
async def groq_ingest() -> AsyncIterator[None]:
    """Exclusive Groq slot for Graphiti LLM extraction (not embeddings)."""
    while True:
        async with _lock:
            if _reply_waiters == 0:
                yield
                return
        await asyncio.sleep(0.05)


def groq_lock() -> asyncio.Lock:
    """Legacy exclusive lock (ingest-safe only if no reply is waiting)."""
    return _lock
