"""Tests for batched ingestion buffer and related integration."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

from unipod_bot.config import Settings
from unipod_bot.incoming import IncomingGroupMessage
from unipod_bot.ingest import (
    batched_episode_body,
    batched_episode_name,
    batched_source_description,
)
from unipod_bot.ingest_buffer import BufferedMessage, IngestBuffer
from unipod_bot.runtime import Runtime


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[object, str]] = []

    async def send_message(self, to, text):
        self.sent.append((to, text))


def _settings(**kwargs) -> Settings:
    if "groq_api_key" in kwargs:
        key = kwargs.pop("groq_api_key")
        kwargs.setdefault("groq_api_keys", (key,) if key else ())
    defaults = dict(
        whatsapp_session_path=Path("data/session.db"),
        test_group_jid="120363test@g.us",
        debug=False,
        log_level=20,
        falkordb_host="localhost",
        falkordb_port=6379,
        falkordb_database="default_db",
        falkordb_username=None,
        falkordb_password=None,
        graphiti_group_id="top100_afl",
        admin_phones=("2348011111111",),
    )
    defaults.update(kwargs)
    return Settings(**defaults)


def _group(**kwargs) -> IncomingGroupMessage:
    base = dict(
        chat_jid="120363test@g.us",
        sender_jid="2348099999999@s.whatsapp.net",
        message_id="MID1",
        text="Hackathon is monday",
        is_reply_group=True,
        iso_time="2026-09-20T05:42:39+00:00",
        mentioned_users=(),
        chat_obj=SimpleNamespace(User="120363test", Server="g.us"),
    )
    base.update(kwargs)
    return IncomingGroupMessage(**base)


def _buffered(message_id: str = "MID1", text: str = "hello", **kwargs) -> BufferedMessage:
    defaults = dict(
        message_id=message_id,
        sender_jid="2348099999999@s.whatsapp.net",
        chat_jid="120363test@g.us",
        text=text,
        iso_time="2026-09-20T05:42:39+00:00",
    )
    defaults.update(kwargs)
    return BufferedMessage(**defaults)


# --- Unit tests for ingest helpers ---

def test_batched_episode_body_joins_messages():
    msgs = [
        _buffered("M1", "Hello everyone"),
        _buffered("M2", "When is hackathon?", sender_jid="234800@s.whatsapp.net"),
        _buffered("M3", "It's on Monday"),
    ]
    body = batched_episode_body(msgs)
    lines = body.split("\n")
    assert len(lines) == 3
    assert "2348099999999@s.whatsapp.net: Hello everyone" in lines[0]
    assert "234800@s.whatsapp.net: When is hackathon?" in lines[1]
    assert "It's on Monday" in lines[2]


def test_batched_episode_body_includes_urls():
    msg = _buffered("M1", "Check this", urls=["https://example.com"])
    body = batched_episode_body([msg])
    assert "[links: https://example.com]" in body


def test_batched_episode_name_format():
    assert batched_episode_name("MID1", 5) == "batch-MID1-x5"
    assert batched_episode_name("", 3) == "batch-unknown-x3"


def test_batched_source_description_includes_all_ids():
    msgs = [
        _buffered("M1", "hello"),
        _buffered("M2", "world", chat_jid="other@g.us"),
    ]
    desc = batched_source_description(msgs)
    assert "M1" in desc
    assert "M2" in desc
    assert "120363test@g.us" in desc
    assert "other@g.us" in desc
    assert "count=2" in desc


# --- Unit tests for IngestBuffer ---

def test_buffer_add_returns_false_below_max():
    buf = IngestBuffer(max_messages=5, interval_seconds=1800)
    msg = _group(message_id="M1")
    assert buf.add(msg) is False
    assert buf.pending_count == 1
    assert buf.pending_groups == 1


def test_buffer_add_returns_true_at_max():
    buf = IngestBuffer(max_messages=3, interval_seconds=1800)
    buf.add(_group(message_id="M1"))
    buf.add(_group(message_id="M2"))
    result = buf.add(_group(message_id="M3"))
    assert result is True
    assert buf.pending_count == 3


def test_buffer_skips_empty_text():
    buf = IngestBuffer(max_messages=10, interval_seconds=1800)
    assert buf.add(_group(message_id="M1", text="")) is False
    assert buf.add(_group(message_id="M2", text="   ")) is False
    assert buf.pending_count == 0


def test_buffer_skips_empty_message_id():
    buf = IngestBuffer(max_messages=10, interval_seconds=1800)
    assert buf.add(_group(message_id="", text="hello")) is False
    assert buf.pending_count == 0


def test_buffer_groups_by_chat_jid():
    buf = IngestBuffer(max_messages=10, interval_seconds=1800)
    buf.add(_group(message_id="M1", chat_jid="group1@g.us"))
    buf.add(_group(message_id="M2", chat_jid="group2@g.us"))
    buf.add(_group(message_id="M3", chat_jid="group1@g.us"))
    assert buf.pending_count == 3
    assert buf.pending_groups == 2


def test_flush_group_calls_callback():
    flushed_batches: list[list] = []

    async def on_flush(messages):
        flushed_batches.append(messages)

    buf = IngestBuffer(max_messages=10, interval_seconds=1800, on_flush=on_flush)
    buf.add(_group(message_id="M1", text="hello"))
    buf.add(_group(message_id="M2", text="world"))

    async def run():
        count = await buf.flush_group("120363test@g.us")
        assert count == 2

    asyncio.run(run())
    assert len(flushed_batches) == 1
    assert len(flushed_batches[0]) == 2
    assert flushed_batches[0][0].message_id == "M1"
    assert flushed_batches[0][1].message_id == "M2"
    assert buf.pending_count == 0
    assert buf.total_flushed_batches == 1
    assert buf.total_flushed_messages == 2


def test_flush_group_empty_is_noop():
    async def on_flush(messages):
        raise AssertionError("should not be called")

    buf = IngestBuffer(max_messages=10, interval_seconds=1800, on_flush=on_flush)

    async def run():
        count = await buf.flush_group("nonexistent@g.us")
        assert count == 0

    asyncio.run(run())


def test_flush_all_respects_interval():
    flushed: list[list] = []

    async def on_flush(messages):
        flushed.append(messages)

    buf = IngestBuffer(max_messages=100, interval_seconds=10, on_flush=on_flush)
    buf.add(_group(message_id="M1"))
    # Backdate the buffer so it looks old enough to flush
    buf._buffers["120363test@g.us"].created_at -= 20.0

    async def run():
        count = await buf.flush_all()
        assert count == 1

    asyncio.run(run())
    assert len(flushed) == 1


def test_flush_all_skips_young_buffers():
    flushed: list[list] = []

    async def on_flush(messages):
        flushed.append(messages)

    # Very long interval — buffer should NOT flush
    buf = IngestBuffer(max_messages=100, interval_seconds=99999, on_flush=on_flush)
    buf.add(_group(message_id="M1"))

    async def run():
        count = await buf.flush_all()
        assert count == 0

    asyncio.run(run())
    assert len(flushed) == 0
    assert buf.pending_count == 1  # still buffered


def test_flush_all_now_ignores_interval():
    flushed: list[list] = []

    async def on_flush(messages):
        flushed.append(messages)

    buf = IngestBuffer(max_messages=100, interval_seconds=99999, on_flush=on_flush)
    buf.add(_group(message_id="M1"))
    buf.add(_group(message_id="M2", chat_jid="other@g.us"))

    async def run():
        count = await buf.flush_all_now()
        assert count == 2

    asyncio.run(run())
    assert len(flushed) == 2  # one per group
    assert buf.pending_count == 0


def test_flush_rebuffers_on_callback_error():
    """If the flush callback fails, messages should be re-buffered."""
    call_count = 0

    async def failing_flush(messages):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("simulated failure")

    buf = IngestBuffer(max_messages=10, interval_seconds=1800, on_flush=failing_flush)
    buf.add(_group(message_id="M1"))
    buf.add(_group(message_id="M2"))

    async def run():
        count = await buf.flush_group("120363test@g.us")
        assert count == 0  # flush failed

    asyncio.run(run())
    assert call_count == 1
    assert buf.pending_count == 2  # messages re-buffered
    assert buf.total_flushed_batches == 0


def test_status_line():
    buf = IngestBuffer(max_messages=10, interval_seconds=1800)
    buf.add(_group(message_id="M1"))
    status = buf.status_line()
    assert "pending=1" in status
    assert "groups=1" in status


# --- Integration: Runtime uses buffer ---

class FakeGraphitiStore:
    def __init__(self) -> None:
        self.batches: list[list] = []
        self.individual: list[str] = []
        self.replies: list[str] = []

    async def ingest_message(self, msg) -> bool:
        self.individual.append(msg.message_id)
        return True

    async def ingest_reply(self, msg, reply: str) -> bool:
        self.replies.append(f"bot-{msg.message_id}")
        return True

    async def ingest_batch(self, messages) -> bool:
        self.batches.append(messages)
        return True

    async def search_facts(self, query: str) -> list[str]:
        return []

    def is_grounded(self, facts: list[str]) -> bool:
        return False


def test_group_messages_go_to_buffer_not_direct_ingest():
    """Messages should be buffered, not immediately ingested via ingest_queue."""
    runtime = Runtime(_settings(), FakeClient())
    store = FakeGraphitiStore()
    runtime.store = store  # type: ignore[assignment]

    async def run():
        # Non-reply group message should be buffered
        await runtime._handle_group(
            _group(text="Some chat", message_id="M1", is_reply_group=False)
        )
        # Ingest-only message (no trigger) should also be buffered
        await runtime._handle_group(
            _group(text="Another chat", message_id="M2", is_reply_group=True)
        )
        # At this point, ingest_queue should be empty (messages in buffer)
        assert runtime.ingest_queue.empty()
        assert runtime.ingest_buffer.pending_count == 2
        # Flush should be empty — messages are in buffer, not queue
        await runtime.flush_ingest()
        assert store.individual == []  # no individual ingestion
        assert store.batches == []  # not flushed yet

    asyncio.run(run())


def test_buffer_flush_triggers_batch_ingest():
    """Flushing the buffer should create a batch ingest job."""
    runtime = Runtime(_settings(), FakeClient())
    store = FakeGraphitiStore()
    runtime.store = store  # type: ignore[assignment]

    async def run():
        await runtime._handle_group(
            _group(text="Message 1", message_id="M1", is_reply_group=False)
        )
        await runtime._handle_group(
            _group(text="Message 2", message_id="M2", is_reply_group=False)
        )
        assert runtime.ingest_buffer.pending_count == 2

        # Flush the buffer — this should enqueue a batch job
        await runtime.ingest_buffer.flush_all_now()
        assert runtime.ingest_buffer.pending_count == 0

        # Process the batch job
        await runtime.flush_ingest()
        assert len(store.batches) == 1
        assert len(store.batches[0]) == 2

    asyncio.run(run())


def test_reply_ingest_stays_immediate():
    """Bot replies should still go through the ingest queue immediately."""
    runtime = Runtime(_settings(groq_api_key="gsk_test"), FakeClient())
    store = FakeGraphitiStore()
    runtime.store = store  # type: ignore[assignment]

    async def run():
        msg = _group(text="When is hackathon", message_id="M1")
        # Simulate reply ingestion (what happens after bot replies)
        runtime._enqueue_ingest("reply", msg, "It's on Monday!")
        await runtime.flush_ingest()
        assert store.replies == ["bot-M1"]
        # Buffer should be empty — replies don't go through buffer
        assert runtime.ingest_buffer.pending_count == 0

    asyncio.run(run())


def test_config_defaults():
    s = _settings()
    assert s.ingest_batch_interval_seconds == 1800.0
    assert s.ingest_batch_max_messages == 15


def test_config_custom_values():
    s = _settings(
        ingest_batch_interval_seconds=3600.0,
        ingest_batch_max_messages=100,
    )
    assert s.ingest_batch_interval_seconds == 3600.0
    assert s.ingest_batch_max_messages == 100
