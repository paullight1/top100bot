from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from unipod_bot.config import load_settings
from unipod_bot.tts import AudioCache, PiperTTSClient, make_tts_client


def test_audio_cache_key_generation():
    with tempfile.TemporaryDirectory() as tmpdir:
        cache = AudioCache(tmpdir)
        key1 = cache.key("Hello world", "en_US-lessac-medium")
        key2 = cache.key("Hello world", "en_US-lessac-medium")
        key3 = cache.key("Hello world", "en_US-amy-medium")
        key4 = cache.key("Goodbye world", "en_US-lessac-medium")

        assert key1 == key2
        assert len(key1) == 64  # SHA-256 hex digest length
        assert key1 != key3
        assert key1 != key4


def test_audio_cache_put_and_get():
    with tempfile.TemporaryDirectory() as tmpdir:
        cache = AudioCache(tmpdir)
        text = "Welcome to Unipod Innovators community!"
        voice = "en_US-lessac-medium"
        data = b"\x00\x01\x02OGG_FAKE_AUDIO_DATA"

        # Initially miss
        assert cache.get(text, voice) is None

        # Put data
        cache.put(text, voice, data)

        # Hit
        retrieved = cache.get(text, voice)
        assert retrieved == data


def test_audio_cache_pruning():
    with tempfile.TemporaryDirectory() as tmpdir:
        # Small max_files limit of 10
        cache = AudioCache(tmpdir, max_files=10)

        # Store 15 items
        for i in range(15):
            cache.put(f"Message number {i}", "voice", f"audio_{i}".encode())

        # Verify pruning keeps total files <= 10
        files = list(Path(tmpdir).glob("*.ogg"))
        assert len(files) <= 10


import asyncio


def test_piper_tts_client_cache_hit_bypasses_http():
    with tempfile.TemporaryDirectory() as tmpdir:
        client = PiperTTSClient(
            url="http://fake-tts.local",
            voice="en_US-lessac-medium",
            cache_enabled=True,
            cache_dir=tmpdir,
        )

        text = "Repeatable greeting message"
        fake_audio = b"CACHED_AUDIO_BYTES_TEST"

        # Pre-seed the cache
        client._cache.put(text, client._voice, fake_audio)

        # synthesize should return cached audio without making any HTTP requests
        with patch("httpx.AsyncClient.post") as mock_post:
            result = asyncio.run(client.synthesize(text))
            assert result == fake_audio
            mock_post.assert_not_called()


def test_piper_tts_client_cache_miss_stores_to_cache():
    with tempfile.TemporaryDirectory() as tmpdir:
        client = PiperTTSClient(
            url="http://fake-tts.local",
            voice="en_US-lessac-medium",
            cache_enabled=True,
            cache_dir=tmpdir,
        )

        text = "Fresh query to synthesize"
        fresh_audio = b"FRESH_SYNTHESIZED_AUDIO_BYTES"

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "audio/ogg;codecs=opus"}
        mock_resp.content = fresh_audio

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp) as mock_post:
            # First call: cache miss, triggers HTTP post and stores to cache
            result1 = asyncio.run(client.synthesize(text))
            assert result1 == fresh_audio
            assert mock_post.call_count == 1

            # Second call: cache hit, bypasses HTTP post
            result2 = asyncio.run(client.synthesize(text))
            assert result2 == fresh_audio
            assert mock_post.call_count == 1  # Still 1!


def test_piper_tts_client_cache_disabled():
    with tempfile.TemporaryDirectory() as tmpdir:
        client = PiperTTSClient(
            url="http://fake-tts.local",
            voice="en_US-lessac-medium",
            cache_enabled=False,
            cache_dir=tmpdir,
        )

        assert client._cache is None

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "audio/ogg;codecs=opus"}
        mock_resp.content = b"AUDIO_NO_CACHE"

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp) as mock_post:
            asyncio.run(client.synthesize("Hello"))
            asyncio.run(client.synthesize("Hello"))
            assert mock_post.call_count == 2


def test_settings_tts_cache_configuration(monkeypatch):
    monkeypatch.setenv("TTS_CACHE_ENABLED", "false")
    monkeypatch.setenv("TTS_CACHE_DIR", "custom/cache/dir")
    monkeypatch.setenv("TTS_CACHE_MAX_FILES", "500")

    s = load_settings(dotenv=False)
    assert s.tts_cache_enabled is False
    assert s.tts_cache_dir == Path("custom/cache/dir")
    assert s.tts_cache_max_files == 500


def test_make_tts_client_passes_cache_config():
    with tempfile.TemporaryDirectory() as tmpdir:
        client = make_tts_client(
            "http://tts.local",
            cache_enabled=True,
            cache_dir=tmpdir,
            cache_max_files=200,
        )
        assert client is not None
        assert client._cache is not None
        assert client._cache.cache_dir == Path(tmpdir)
        assert client._cache.max_files == 200
