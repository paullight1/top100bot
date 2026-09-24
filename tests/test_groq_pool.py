"""Tests for Groq API key pool rotation and cooldown logic."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from unipod_bot.groq_pool import GroqKeyPool, GroqKeyPoolExhausted


class TestGroqKeyPool:
    """Core pool functionality."""

    def test_single_key_works(self):
        pool = GroqKeyPool(["gsk_one"])
        assert pool.total == 1
        assert pool.available_count == 1
        assert pool.get_key() == "gsk_one"
        assert pool.get_key() == "gsk_one"  # always returns the same key

    def test_round_robin(self):
        pool = GroqKeyPool(["a", "b", "c"])
        keys = [pool.get_key() for _ in range(6)]
        assert keys == ["a", "b", "c", "a", "b", "c"]

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="at least one"):
            GroqKeyPool([])

    def test_duplicates_preserved(self):
        """Duplicate keys in the list are kept as-is (pool trusts config parser)."""
        pool = GroqKeyPool(["a", "a", "b"])
        assert pool.total == 3

    def test_status_line(self):
        pool = GroqKeyPool(["a", "b"])
        assert pool.status_line() == "2/2 keys available"


class TestCooldown:
    """Rate-limit marking and cooldown expiry."""

    def test_mark_rate_limited_skips_key(self):
        pool = GroqKeyPool(["a", "b", "c"])
        pool.get_key()  # advance past "a"
        pool.mark_rate_limited("a", retry_after=600)

        # "a" should be skipped
        keys = [pool.get_key() for _ in range(4)]
        assert "a" not in keys
        assert pool.available_count == 2

    def test_cooldown_expiry(self):
        pool = GroqKeyPool(["a", "b"])

        # Mock monotonic so we control time
        base = time.monotonic()
        with patch("unipod_bot.groq_pool.time") as mock_time:
            mock_time.monotonic.return_value = base
            pool.mark_rate_limited("a", retry_after=10)

            # "a" is cooled down, "b" is returned
            key1 = pool.get_key()
            assert key1 == "b"
            assert pool.available_count == 1

            # Advance past cooldown
            mock_time.monotonic.return_value = base + 11
            assert pool.available_count == 2
            keys = {pool.get_key() for _ in range(4)}
            assert "a" in keys

    def test_all_keys_exhausted(self):
        pool = GroqKeyPool(["a", "b"])
        pool.mark_rate_limited("a", retry_after=600)
        pool.mark_rate_limited("b", retry_after=600)

        assert pool.available_count == 0
        with pytest.raises(GroqKeyPoolExhausted, match="All 2"):
            pool.get_key()

    def test_status_reflects_cooldown(self):
        pool = GroqKeyPool(["a", "b", "c"])
        pool.mark_rate_limited("b", retry_after=600)
        assert pool.status_line() == "2/3 keys available"

    def test_mark_unknown_key_does_not_crash(self):
        """Marking a key not in the pool shouldn't raise."""
        pool = GroqKeyPool(["a", "b"])
        # This key isn't in the pool, but mark_rate_limited should not crash
        # (it just sets a cooldown entry that's never looked up)
        pool.mark_rate_limited("unknown_key", retry_after=60)
        # Pool still works
        assert pool.get_key() in {"a", "b"}


class TestPeekKey:
    def test_peek_does_not_advance(self):
        pool = GroqKeyPool(["a", "b", "c"])
        first = pool.peek_key()
        second = pool.peek_key()
        assert first == second  # peek doesn't advance


class TestConfigParsing:
    """Test that config.parse_groq_keys handles various inputs."""

    def test_single_key(self):
        from unipod_bot.config import parse_groq_keys
        assert parse_groq_keys("gsk_abc123") == ("gsk_abc123",)

    def test_multiple_keys(self):
        from unipod_bot.config import parse_groq_keys
        result = parse_groq_keys("gsk_key1,gsk_key2,gsk_key3")
        assert result == ("gsk_key1", "gsk_key2", "gsk_key3")

    def test_whitespace_handling(self):
        from unipod_bot.config import parse_groq_keys
        result = parse_groq_keys(" gsk_a , gsk_b , gsk_c ")
        assert result == ("gsk_a", "gsk_b", "gsk_c")

    def test_empty_string(self):
        from unipod_bot.config import parse_groq_keys
        assert parse_groq_keys("") == ()
        assert parse_groq_keys(None) == ()

    def test_deduplication(self):
        from unipod_bot.config import parse_groq_keys
        result = parse_groq_keys("gsk_a,gsk_b,gsk_a")
        assert result == ("gsk_a", "gsk_b")

    def test_blank_segments_filtered(self):
        from unipod_bot.config import parse_groq_keys
        result = parse_groq_keys("gsk_a,,gsk_b, ,gsk_c")
        assert result == ("gsk_a", "gsk_b", "gsk_c")


class TestSettingsCompat:
    """Ensure backwards compatibility of Settings with the new groq_api_keys field."""

    def test_groq_api_key_property(self):
        """The .groq_api_key property returns the first key for backward compat."""
        from unipod_bot.config import Settings
        from pathlib import Path

        s = Settings(
            whatsapp_session_path=Path("/tmp/test.db"),
            test_group_jid=None,
            debug=False,
            log_level=20,
            falkordb_host="localhost",
            falkordb_port=6379,
            falkordb_database="test",
            falkordb_username=None,
            falkordb_password=None,
            graphiti_group_id="test",
            admin_phones=("123",),
            groq_api_keys=("gsk_first", "gsk_second"),
        )
        assert s.groq_api_key == "gsk_first"
        assert s.groq_enabled is True

    def test_no_keys_groq_disabled(self):
        from unipod_bot.config import Settings
        from pathlib import Path

        s = Settings(
            whatsapp_session_path=Path("/tmp/test.db"),
            test_group_jid=None,
            debug=False,
            log_level=20,
            falkordb_host="localhost",
            falkordb_port=6379,
            falkordb_database="test",
            falkordb_username=None,
            falkordb_password=None,
            graphiti_group_id="test",
            admin_phones=("123",),
            groq_api_keys=(),
        )
        assert s.groq_api_key is None
        assert s.groq_enabled is False
