"""Piper TTS client — converts text to OGG/Opus audio for WhatsApp voice notes.

The bot sends the sanitized (TTS-ready) text to a self-hosted Piper HTTP
server and receives raw audio bytes that neonize sends as a voice note.

Configuration (via .env):
    TTS_ENABLED=true
    PIPER_TTS_URL=http://<vps-ip>:<port>/synthesize
    TTS_TIMEOUT_SECONDS=30   # optional, default 30

Piper HTTP server expected API:
    POST /synthesize
    Content-Type: application/json
    {"text": "...", "voice": "en_US-lessac-medium"}   (or omit voice to use server default)

    Response: audio/ogg;codecs=opus  (binary body)

If PIPER_TTS_URL is not set or TTS_ENABLED=false, the async client raises
TTSDisabled so the caller can fall back to a text reply gracefully.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
from pathlib import Path
import shutil
import struct
from typing import TYPE_CHECKING
import wave

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger("unipod_bot")


class TTSDisabled(Exception):
    """Raised when TTS is not configured or disabled."""


class TTSError(Exception):
    """Raised when Piper TTS call fails after retries."""


class AudioCache:
    """Persistent on-disk cache for synthesized audio files.

    Keyed by SHA-256(voice + clean_text) to eliminate redundant audio synthesis
    and conserve CPU credits on T4g ARM64 instances.
    """

    def __init__(self, cache_dir: Path | str, max_files: int = 1000) -> None:
        self.cache_dir = Path(cache_dir)
        self.max_files = max(max_files, 10)
        self._ensure_dir()

    def _ensure_dir(self) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            logger.exception("event=tts_cache_mkdir_failed dir=%s", self.cache_dir)

    def key(self, text: str, voice: str = "") -> str:
        content = f"{voice.strip()}:{text.strip()}".encode("utf-8")
        return hashlib.sha256(content).hexdigest()

    def get(self, text: str, voice: str = "") -> bytes | None:
        cache_key = self.key(text, voice)
        file_path = self.cache_dir / f"{cache_key}.ogg"
        if file_path.is_file():
            try:
                data = file_path.read_bytes()
                if data:
                    try:
                        file_path.touch(exist_ok=True)
                    except Exception:
                        pass
                    return data
            except Exception:
                logger.warning("event=tts_cache_read_failed file=%s", file_path, exc_info=True)
        return None

    def put(self, text: str, voice: str, data: bytes) -> None:
        if not data:
            return
        self._ensure_dir()
        self._prune_if_needed()
        cache_key = self.key(text, voice)
        file_path = self.cache_dir / f"{cache_key}.ogg"
        try:
            temp_path = self.cache_dir / f"{cache_key}.tmp"
            temp_path.write_bytes(data)
            temp_path.replace(file_path)
        except Exception:
            logger.warning("event=tts_cache_write_failed file=%s", file_path, exc_info=True)

    def _prune_if_needed(self) -> None:
        """Evict oldest accessed files when exceeding max_files."""
        try:
            files = list(self.cache_dir.glob("*.ogg"))
            if len(files) >= self.max_files:
                files.sort(key=lambda p: p.stat().st_mtime)
                to_remove = files[: max(1, len(files) - int(self.max_files * 0.9))]
                for f in to_remove:
                    try:
                        f.unlink(missing_ok=True)
                    except Exception:
                        pass
        except Exception:
            logger.debug("event=tts_cache_prune_error", exc_info=True)


class PiperTTSClient:
    """Async client for the self-hosted Piper TTS HTTP server.

    Supports on-disk SHA-256 audio caching to avoid repeated synthesis.

    Usage::

        client = PiperTTSClient(url="http://15.240.69.248.nip.io", api_key="...")
        audio_bytes = await client.synthesize("Hello, community!")
    """

    def __init__(
        self,
        url: str,
        *,
        api_key: str | None = None,
        voice: str = "",
        timeout_seconds: float = 30.0,
        cache_enabled: bool = True,
        cache_dir: Path | str | None = "data/tts_cache",
        cache_max_files: int = 1000,
    ) -> None:
        raw_url = url.rstrip("/")
        # If url doesn't include an endpoint path, default to /api/tts
        if not any(raw_url.endswith(p) for p in ("/api/tts", "/synthesize", "/tts")):
            raw_url = f"{raw_url}/api/tts"
        self._url = raw_url
        self._api_key = api_key
        self._voice = voice
        self._timeout = timeout_seconds
        self._cache: AudioCache | None = (
            AudioCache(cache_dir, max_files=cache_max_files)
            if cache_enabled and cache_dir
            else None
        )

    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def synthesize(self, text: str) -> bytes:
        """Send text to Piper TTS and return audio bytes.

        Checks local on-disk cache first to eliminate latency and CPU load.

        Raises:
            TTSError: On HTTP errors or unexpected response content.
        """
        clean_text = text.strip()
        if not clean_text:
            raise TTSError("Empty text passed to TTS synthesizer.")

        # Cap at 2000 chars to satisfy API schema limits
        if len(clean_text) > 2000:
            clean_text = clean_text[:1990] + "..."

        voice_key = self._voice or "default"

        # Check local cache first
        if self._cache is not None:
            cached = self._cache.get(clean_text, voice_key)
            if cached:
                logger.info(
                    "event=tts_cache_hit key=%s bytes=%d chars=%d",
                    self._cache.key(clean_text, voice_key)[:12],
                    len(cached),
                    len(clean_text),
                )
                return cached

        payload: dict = {"text": clean_text}
        # Only include voice if explicitly configured and not default
        if self._voice and self._voice != "en_US-lessac-medium":
            payload["voice"] = self._voice

        headers: dict[str, str] = {
            "Accept": "audio/wav,audio/ogg;codecs=opus,audio/*",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        logger.debug(
            "event=tts_synthesize chars=%d url=%s", len(clean_text), self._url
        )

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as http:
                response = await http.post(
                    self._url,
                    json=payload,
                    headers=headers,
                )
        except httpx.TimeoutException as exc:
            raise TTSError(f"Piper TTS request timed out after {self._timeout}s") from exc
        except httpx.HTTPError as exc:
            raise TTSError(f"Piper TTS HTTP error: {exc}") from exc

        if response.status_code != 200:
            raise TTSError(
                f"Piper TTS returned HTTP {response.status_code}: {response.text[:200]}"
            )

        content_type = response.headers.get("content-type", "")
        if not (content_type.startswith("audio/") or content_type.startswith("application/octet")):
            raise TTSError(
                f"Piper TTS returned unexpected content-type: {content_type!r}"
            )

        audio_bytes = response.content
        if not audio_bytes:
            raise TTSError("Piper TTS returned empty audio response.")

        logger.info(
            "event=tts_synthesized bytes=%d chars=%d", len(audio_bytes), len(clean_text)
        )

        # Cache synthesized audio for subsequent calls
        if self._cache is not None:
            self._cache.put(clean_text, voice_key, audio_bytes)
            logger.debug(
                "event=tts_cached key=%s bytes=%d",
                self._cache.key(clean_text, voice_key)[:12],
                len(audio_bytes),
            )

        return audio_bytes


def make_tts_client(
    url: str | None,
    *,
    api_key: str | None = None,
    voice: str = "",
    timeout_seconds: float = 30.0,
    cache_enabled: bool = True,
    cache_dir: Path | str | None = "data/tts_cache",
    cache_max_files: int = 1000,
) -> PiperTTSClient | None:
    """Factory: return a PiperTTSClient if url is set, else None."""
    if not url:
        return None
    return PiperTTSClient(
        url=url,
        api_key=api_key,
        voice=voice,
        timeout_seconds=timeout_seconds,
        cache_enabled=cache_enabled,
        cache_dir=cache_dir,
        cache_max_files=cache_max_files,
    )


def get_wav_info(wav_bytes: bytes, num_bars: int = 64) -> tuple[int, bytes]:
    """Extract audio duration in seconds and 64-bar waveform byte array from WAV bytes.

    Uses the standard library wave and struct modules without spawning external ffprobe.
    Returns:
        (duration_seconds, waveform_bytes)
    """
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            n_frames = w.getnframes()
            rate = w.getframerate()
            duration = max(1, int(round(n_frames / float(rate))))
            width = w.getsampwidth()
            channels = w.getnchannels()
            if width != 2 or n_frames == 0:
                return duration, b""
            frames = w.readframes(n_frames)
            total_samples = len(frames) // (width * channels)
            chunk_size = max(1, total_samples // num_bars)
            wf = bytearray()
            for i in range(num_bars):
                start = i * chunk_size * width * channels
                end = min(len(frames), (i + 1) * chunk_size * width * channels)
                chunk = frames[start:end]
                if not chunk:
                    wf.append(0)
                    continue
                samples = struct.unpack(f"<{len(chunk)//2}h", chunk)
                peak = max(abs(s) for s in samples[::channels])
                level = min(100, int((peak / 32767.0) * 100))
                wf.append(level)
            return duration, bytes(wf)
    except Exception:
        return 1, b""


async def wav_to_ogg_opus(wav_bytes: bytes) -> tuple[bytes, str]:
    """Convert WAV audio bytes to Ogg Opus (audio/ogg; codecs=opus) using ffmpeg if available.

    WhatsApp Push-To-Talk (PTT) voice notes require OGG container with Opus audio.
    If ffmpeg is unavailable or fails, returns (wav_bytes, "audio/wav").
    """
    if not wav_bytes or not wav_bytes.startswith(b"RIFF"):
        if wav_bytes.startswith(b"OggS"):
            return wav_bytes, "audio/ogg; codecs=opus"
        return wav_bytes, "audio/ogg; codecs=opus"

    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        return wav_bytes, "audio/wav"

    cmd = [
        ffmpeg_bin,
        "-y",
        "-f",
        "wav",
        "-i",
        "pipe:0",
        "-c:a",
        "libopus",
        "-b:a",
        "32k",
        "-vbr",
        "on",
        "-application",
        "voip",
        "-f",
        "ogg",
        "pipe:1",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(input=wav_bytes)
        if proc.returncode == 0 and stdout:
            return stdout, "audio/ogg; codecs=opus"
        logger.warning(
            "event=ffmpeg_opus_conversion_failed rc=%d stderr=%s",
            proc.returncode,
            stderr.decode(errors="replace")[:200],
        )
    except Exception:
        logger.warning("event=ffmpeg_opus_conversion_error", exc_info=True)

    return wav_bytes, "audio/wav"


async def prepare_voice_note(audio_bytes: bytes) -> tuple[bytes, str, int, bytes]:
    """Prepare synthesized audio for WhatsApp PTT voice note transmission.

    Returns:
        (final_audio_bytes, mimetype, duration_seconds, waveform_bytes)
    """
    duration, waveform = get_wav_info(audio_bytes)
    converted_bytes, mimetype = await wav_to_ogg_opus(audio_bytes)
    return converted_bytes, mimetype, duration, waveform

