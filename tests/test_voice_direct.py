"""Tests for robust voice note preparation and direct protobuf transmission."""
from __future__ import annotations

import asyncio
import io
import struct
import wave
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from unipod_bot.config import load_settings
from unipod_bot.runtime import Runtime
from unipod_bot.tts import get_wav_info, prepare_voice_note, wav_to_ogg_opus


def _make_dummy_wav(duration_s: float = 2.0, sample_rate: int = 16000) -> bytes:
    """Generate in-memory PCM 16-bit mono WAV audio."""
    buf = io.BytesIO()
    total_frames = int(duration_s * sample_rate)
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        # Generate non-zero amplitude samples
        samples = [int(10000 * ((i % 100) / 100.0)) for i in range(total_frames)]
        raw_frames = struct.pack(f"<{len(samples)}h", *samples)
        w.writeframes(raw_frames)
    return buf.getvalue()


def test_get_wav_info_calculates_duration_and_waveform():
    wav_bytes = _make_dummy_wav(duration_s=3.0, sample_rate=16000)
    duration, waveform = get_wav_info(wav_bytes)

    assert duration == 3
    assert len(waveform) == 64
    assert any(b > 0 for b in waveform)


def test_get_wav_info_fallback_on_corrupt_data():
    duration, waveform = get_wav_info(b"corrupt non-wav data")
    assert duration == 1
    assert waveform == b""


@pytest.mark.anyio
async def test_wav_to_ogg_opus_without_ffmpeg():
    wav_bytes = _make_dummy_wav(duration_s=1.0)
    with patch("shutil.which", return_value=None):
        out_bytes, mimetype = await wav_to_ogg_opus(wav_bytes)
        assert out_bytes == wav_bytes
        assert mimetype == "audio/wav"


@pytest.mark.anyio
async def test_prepare_voice_note_returns_waveform_and_duration():
    wav_bytes = _make_dummy_wav(duration_s=2.0)
    final_bytes, mimetype, duration, waveform = await prepare_voice_note(wav_bytes)

    assert duration == 2
    assert len(waveform) == 64
    assert isinstance(mimetype, str)
    assert len(final_bytes) > 0


@pytest.mark.anyio
async def test_send_voice_note_direct_upload_and_protobuf():
    settings = load_settings(dotenv=False)

    mock_upload_resp = MagicMock()
    mock_upload_resp.url = "https://mmg.whatsapp.net/mms/audio/test"
    mock_upload_resp.DirectPath = "/mms/audio/direct_test"
    mock_upload_resp.FileEncSHA256 = b"enc_sha" * 4
    mock_upload_resp.FileLength = 12345
    mock_upload_resp.FileSHA256 = b"file_sha" * 4
    mock_upload_resp.MediaKey = b"media_key" * 4

    mock_client = MagicMock()
    mock_client.upload = AsyncMock(return_value=mock_upload_resp)
    mock_client.send_message = AsyncMock()
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import ContextInfo
    fake_ctx = ContextInfo(stanzaID="STANZA123")
    mock_client._make_quoted_message = MagicMock(return_value=fake_ctx)

    runtime = Runtime(settings, mock_client)

    wav_bytes = _make_dummy_wav(duration_s=2.0)
    fake_event = MagicMock()

    sent = await runtime._send_voice_note("120363430416381566@g.us", wav_bytes, event_obj=fake_event)

    assert sent is True
    mock_client.upload.assert_awaited_once()
    mock_client.send_message.assert_awaited_once()

    # Verify message sent has AudioMessage with PTT=True and seconds
    sent_target, sent_proto = mock_client.send_message.await_args[0]
    assert sent_target == "120363430416381566@g.us"
    assert sent_proto.HasField("audioMessage")
    audio = sent_proto.audioMessage
    assert audio.PTT is True
    assert audio.seconds == 2
    assert len(audio.waveform) == 64
    assert audio.contextInfo.stanzaID == "STANZA123"
