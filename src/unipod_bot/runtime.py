from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
from pathlib import Path
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from neonize.client import ChatPresence, ChatPresenceMedia
from neonize.utils.jid import build_jid

from unipod_bot.admin import handle_admin_dm, is_admin_jid
from unipod_bot.config import Settings, format_feedback_contacts_str, format_helpers_str
from unipod_bot.daily_report import DailyReporter
from unipod_bot.error_tracker import ErrorTracker, ErrorTrackingHandler, Severity
from unipod_bot.fact_check import is_no_correction
from unipod_bot.groq_client import (
    CORRECTION_PROMPT,
    GroqReplyClient,
    assemble_correction_prompt,
    assemble_reply_prompt,
    is_unknown_reply,
    retrieval_query,
)
from unipod_bot.groq_pool import GroqKeyPool, GroqKeyPoolExhausted
from unipod_bot.health import HealthChecker
from unipod_bot.incoming import IncomingAdminDm, IncomingGroupMessage, IncomingUserDm
from unipod_bot.ingest import (
    parse_iso_time,
    pdf_episode_body,
    pdf_episode_name,
    pdf_source_description,
)
from unipod_bot.reminders import (
    InteractiveReminderTracker,
    ReminderStore,
    _CANCEL_RE,
    detect_manual_reminder,
    detect_meeting_link,
    extract_meeting_details,
    format_30m_reminder,
    format_active_reminders_list,
    format_manual_reminder_confirmation,
    format_reminder_interactive_prompt,
    format_start_reminder,
    format_unverified_meeting_alert,
    is_reminder_cancel_flow,
    is_reminder_cancel_request,
    is_reminder_help_or_empty,
    is_reminder_intent,
    is_reminder_list_request,
    parse_meeting_time,
)
from unipod_bot.ingest_buffer import IngestBuffer
from unipod_bot.jids import jid_str, jid_user, phone_from_jid
from unipod_bot.memory.graphiti_store import GraphitiStore
from unipod_bot.notify import format_unknown_dm, format_unknown_group, format_unverified_info
from unipod_bot.pdf_handler import (
    PdfExtraction,
    assemble_translation_prompt,
    extract_pdf_text,
    format_pdf_mention,
)
from unipod_bot.recent import RecentGroupLog
from unipod_bot.roster import RosterStore, fetch_group_roster, format_roster_context
from unipod_bot.routing import group_message_trigger
from unipod_bot.summary import (
    CONCISE_SUMMARY_PROMPT,
    DETAILED_SUMMARY_PROMPT,
    SKILL_RECOMMENDATION_TIP,
    SUMMARY_HELP_CARD,
    SummaryStore,
    SummaryWindow,
    format_messages_for_prompt,
    is_summary_request,
    is_today_question,
    parse_summary_time_range,
)
from unipod_bot.top100_activities import Top100ActivitiesClient, Top100Activity, is_activity_question
from unipod_bot.text_sanitizer import sanitize_for_chat, sanitize_for_tts
from unipod_bot.tts import PiperTTSClient, make_tts_client
from unipod_bot.whatsapp import is_bot_mentioned, is_reply_to_bot, looks_like_question

import uuid

logger = logging.getLogger("unipod_bot")

STUB_GROUP_REPLY = (
    "No community memory is connected yet. I logged this; I will answer in-group once facts exist."
)

BOT_IDENTITY_FACTS: tuple[str, ...] = (
    "Identity: The Top100 AFL assistant helps members with grounded answers from the configured test-group conversation and public Top100 activities.",
    "Purpose: The bot uses recent conversation context, approved community facts, and public Top100 activity records to provide factual answers.",
    "Skills - Events & Hackathons: Answers questions about hackathons, workshops, innovation programs, lab schedules, rules, and deadlines.",
    "Skills - Collaboration: Matches team-seekers and innovators with complementary skills for hackathons and projects.",
    "Skills - Summary: Generates detailed activity briefings for specific dates or timeframes via the /summary skill (e.g. '/summary 24 sept', '/summary today', '/summary last 2 days', '/summary week', '/summary month', '/summary 2 weeks').",
    "Skills - Reminders: Members can use the reminder skill (/reminder or tag @bot plainly) to set reminders interactively or directly for meetings, tasks, or deadlines (e.g. '/reminder 10m start meeting', '@bot set a reminder for the next 10 mins so that I can start my meeting', '/reminder list').",
    "Skills - Voice: Generates audio voice notes via the voice skill when requested with /voice anywhere in the message.",
    "Skills - Documents: Ingests shared PDFs and can share community documents on request via the document skill (e.g. 'send me the hackathon guide').",
    "Skills - Multilingual: Understands questions in English, Swahili, French, and other languages.",
    "Reliability: The bot never guesses or hallucinates. If it does not have a grounded answer, it stays silent in public groups and forwards the question to the admin team.",
    "Administration: The bot is maintained by the Top100 Africa Future Leaders team.",
)

_BOT_SELF_QUERY_RE = re.compile(
    r"\b("
    r"who are you|what are you|what can you do|what do you do|what does this bot do|what does the bot do|"
    r"what is this bot|what is the bot|who is this bot|who is the bot|"
    r"how does this bot work|how does the bot work|how do you work|who made you|who created you|"
    r"about the bot|about this bot|about bot|bot capabilities|bot features|what is your purpose|"
    r"how to use this bot|how to use the bot|how do i use this bot|can you do|are you a bot|your name|introduce yourself|"
    r"what is top100 afl bot|what is afl bot|tell me about yourself|help me bot|bot help|help bot|"
    # Swahili bot queries
    r"wewe ni nani|huyu bot ni nani|wewe ni bot|unafanya nini|unasaidiaje|unasaidia aje|"
    r"kazi ya bot|kazi yako ni nini|kuhusu bot|jinsi ya kutumia bot|"
    # French bot queries
    r"qui es-tu|qui etes-vous|qui est ce bot|qu'est-ce que ce bot|c'est quoi ce bot|"
    r"que peux-tu faire|que pouvez-vous faire|que fais-tu|que faites-vous|"
    r"comment fonctionne ce bot|a propos du bot|aide bot"
    r")\b",
    re.IGNORECASE,
)


def _is_bot_self_query(text: str) -> bool:
    if not text:
        return False
    if _BOT_SELF_QUERY_RE.search(text):
        return True
    lower = text.lower()
    if "bot" in lower and any(
        w in lower for w in ("do", "can", "work", "feature", "help", "who", "what", "purpose", "use", "ability")
    ):
        return True
    return False

Job = IncomingGroupMessage | IncomingAdminDm | IncomingUserDm
IngestJob = tuple[str, IncomingGroupMessage | None, str | None, list | None]


def _history_has_context(history: str) -> bool:
    lines = [
        line.strip()
        for line in (history or "").splitlines()
        if line.strip() and line.strip() != "(none)"
    ]
    return len(lines) > 1


import re as _re

_FACT_SOURCE_RE = _re.compile(r"\[from\s+@(\S+?)(?:\s+in\s+group\s+\S+)?\]")


def _extract_non_admin_facts(
    facts: list[str], admin_phones: tuple[str, ...]
) -> list[str]:
    """Return facts that were not confirmed by admin users.

    Facts with an admin source tag like '[from @<admin_phone>]' are verified.
    Facts with non-admin source tags or without source tags (unprovenanced edges/nodes)
    are treated as unverified. Dynamically added roster summary lines are ignored.
    """
    non_admin: list[str] = []
    admin_set = set(admin_phones)
    for fact in facts:
        match = _FACT_SOURCE_RE.search(fact)
        if not match:
            continue
        source = match.group(1)
        source_digits = "".join(ch for ch in source if ch.isdigit())
        if not source_digits or source_digits not in admin_set:
            non_admin.append(fact)
    return non_admin


class AdminInbox:
    """Prefer the live WhatsApp chat object from an admin DM; else phone JIDs."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._by_phone: dict[str, Any] = {}
        for phone in settings.admin_phones:
            self._by_phone[phone] = build_jid(phone)
        self._extra: dict[str, Any] = {}
        for raw in settings.admin_jids:
            user, _, server = raw.partition("@")
            if not user:
                continue
            obj = build_jid(user, server or "s.whatsapp.net")
            digits = "".join(ch for ch in user if ch.isdigit())
            if digits and digits in settings.admin_phones:
                continue
            self._extra[raw.lower()] = obj

    def remember(self, chat_obj: Any, chat_jid: str, sender_jid: str) -> None:
        if chat_obj is None:
            return
        phone = phone_from_jid(sender_jid) or phone_from_jid(chat_obj)
        if phone and phone in self._settings.admin_phones:
            self._by_phone[phone] = chat_obj
            return
        if chat_jid:
            self._extra[chat_jid.lower()] = chat_obj

    def targets(self) -> list[Any]:
        seen: set[str] = set()
        out: list[Any] = []
        for target in (*self._by_phone.values(), *self._extra.values()):
            key = jid_str(target).lower()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(target)
        return out


class Runtime:
    def __init__(self, settings: Settings, client: Any, groq: GroqReplyClient | None = None) -> None:
        self.settings = settings
        self.client = client
        maxsize = max(int(settings.queue_maxsize), 1)
        self.queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=maxsize)
        self.ingest_queue: asyncio.Queue[IngestJob] = asyncio.Queue(maxsize=maxsize)
        self.own_users: set[str] = set()
        if settings.bot_phone:
            self.own_users.add(settings.bot_phone)
            self.own_users.add(f"{settings.bot_phone}@s.whatsapp.net")
        self.recent = RecentGroupLog(settings.short_term_max_messages)
        self.top100_activities = Top100ActivitiesClient(settings.top100_api_base_url)
        self.inbox = AdminInbox(settings)
        self.stub_known = bool(settings.groq_enabled)
        self.ingest_count = 0
        self.notify_count = 0
        self.graphiti_ingest_count = 0
        self.store: GraphitiStore | None = None
        self.roster = RosterStore()

        # Shared key pool for Groq API rotation
        self.groq_pool: GroqKeyPool | None = None
        if settings.groq_api_keys:
            self.groq_pool = GroqKeyPool(list(settings.groq_api_keys))

        self.groq = groq
        if self.groq is None and settings.groq_enabled:
            self.groq = GroqReplyClient(settings, pool=self.groq_pool)
            logger.info("event=groq_ready model=%s keys=%d", settings.groq_model, self.groq_pool.total if self.groq_pool else 0)
        elif self.groq is None:
            logger.info("event=groq_disabled reason=no_api_key")

        # Piper TTS client (optional, for /voice requests)
        self.tts_client: PiperTTSClient | None = make_tts_client(
            settings.piper_tts_url if settings.tts_enabled else None,
            api_key=settings.piper_tts_api_key,
            voice=settings.piper_tts_voice,
            timeout_seconds=settings.tts_timeout_seconds,
            cache_enabled=settings.tts_cache_enabled,
            cache_dir=settings.tts_cache_dir,
            cache_max_files=settings.tts_cache_max_files,
        )
        if self.tts_client:
            logger.info("event=tts_ready url=%s", settings.piper_tts_url)
        else:
            logger.info("event=tts_disabled")

        # --- Batched ingestion buffer ---
        self.ingest_buffer = IngestBuffer(
            max_messages=settings.ingest_batch_max_messages,
            interval_seconds=settings.ingest_batch_interval_seconds,
            on_flush=self._on_batch_flush,
        )

        # --- New subsystems ---
        self.daily_reporter = DailyReporter(
            report_hour_utc=settings.daily_report_hour_utc,
            report_minute_utc=settings.daily_report_minute_utc,
            state_file=Path("data/daily_report_state.json"),
        )
        self.error_tracker = ErrorTracker(
            cooldown_seconds=settings.critical_alert_cooldown_seconds,
            admin_notify=self._sync_admin_alert,
        )
        self.health_checker: HealthChecker | None = None  # wired after start_backends
        # Correction back-off: track (sender, topic_hash) -> last_correction_time
        self._correction_cooldowns: dict[str, float] = defaultdict(float)
        self._CORRECTION_COOLDOWN = 300.0  # 5 min cooldown per user per topic
        # Track whether admin has been notified about pool exhaustion (reset on resume)
        self._groq_paused_notified: bool = False
        # PDF send requests tracking
        self._pdf_store: dict[str, tuple[bytes, str]] = {}  # msg_id -> (pdf_bytes, filename)
        # Persistent meeting reminders store
        self.reminder_store = ReminderStore()
        self.interactive_reminders = InteractiveReminderTracker()
        # Persistent message journal for date-range summaries
        self.summary_store = SummaryStore()
        self.admin_ingest_mode: bool = True

        # Install error tracking handler on the logger (replace any previous one
        # so multiple Runtime instances in tests don't stack handlers).
        bot_logger = logging.getLogger("unipod_bot")
        for h in list(bot_logger.handlers):
            if isinstance(h, ErrorTrackingHandler):
                bot_logger.removeHandler(h)
        self._error_handler = ErrorTrackingHandler(self.error_tracker)
        bot_logger.addHandler(self._error_handler)

    def remember_own(self, jid: Any) -> None:
        if jid is None:
            return
        before = set(self.own_users)
        self._remember_own_once(jid)
        for attr in ("JID", "LID", "ID", "Id", "Lid", "jid", "lid"):
            nested = getattr(jid, attr, None)
            if nested is not None and nested is not jid:
                self._remember_own_once(nested)
        added = self.own_users - before
        if added:
            logger.info("event=own_jid added=%s all=%s", ",".join(sorted(added)), ",".join(sorted(self.own_users)))

    def _remember_own_once(self, jid: Any) -> None:
        user = jid_user(jid)
        if user:
            self.own_users.add(user)
        text = jid_str(jid)
        if text and "@" in text:
            self.own_users.add(text.split("@", 1)[0])
        elif text and text.isdigit():
            self.own_users.add(text)

    def _sync_admin_alert(self, body: str) -> None:
        """Synchronous callback for ErrorTracker to schedule admin alerts."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._send_admin_alert(body))
        except RuntimeError:
            logger.warning("event=alert_no_loop body=%s", body[:100])

    async def _send_admin_alert(self, body: str) -> None:
        """Send a critical alert to all admin DMs (never to groups)."""
        targets = self.inbox.targets()
        for target in targets:
            try:
                await self.client.send_message(target, body)
            except Exception:
                logger.exception("event=alert_send_failed")

    @asynccontextmanager
    async def _typing_presence(self, chat_obj: Any, is_voice: bool = False):
        """Show 'typing...' or 'recording audio...' presence indicator while bot is processing."""
        if not hasattr(self.client, "send_chat_presence") or chat_obj is None:
            yield
            return

        target_jid = chat_obj
        if isinstance(chat_obj, str):
            from unipod_bot.jids import is_group_jid
            user, _, server = chat_obj.partition("@")
            target_jid = build_jid(user, server or ("g.us" if is_group_jid(chat_obj) else "s.whatsapp.net"))

        media = (
            ChatPresenceMedia.CHAT_PRESENCE_MEDIA_AUDIO
            if is_voice
            else ChatPresenceMedia.CHAT_PRESENCE_MEDIA_TEXT
        )

        async def _call_presence(state: Any) -> None:
            try:
                res = self.client.send_chat_presence(target_jid, state, media)
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                logger.debug("event=presence_update_failed state=%s", state, exc_info=True)

        await _call_presence(ChatPresence.CHAT_PRESENCE_COMPOSING)
        try:
            yield
        finally:
            await _call_presence(ChatPresence.CHAT_PRESENCE_PAUSED)

    async def _send_or_reply(
        self, chat_obj: Any, text: str, event_obj: Any = None
    ) -> None:
        """Send a text reply, quoting the original message if event_obj is available."""
        if event_obj is not None and hasattr(self.client, "reply_message"):
            try:
                await self.client.reply_message(text, event_obj, to=chat_obj)
                return
            except Exception:
                logger.warning(
                    "event=reply_message_failed fallback=send_message", exc_info=True
                )
        await self.client.send_message(chat_obj, text)

    async def _send_voice_note(
        self,
        chat_obj: Any,
        audio_bytes: bytes,
        event_obj: Any = None,
    ) -> bool:
        """Send a PTT voice note directly via neonize upload & AudioMessage protobuf.

        Bypasses neonize's built-in build_audio_message which crashes on ffprobe
        and float duration parsing.
        """
        from unipod_bot.tts import prepare_voice_note

        final_bytes, mimetype, duration, waveform = await prepare_voice_note(audio_bytes)

        # 1. Prefer direct protobuf assembly if client supports upload + send_message
        if hasattr(self.client, "upload") and hasattr(self.client, "send_message"):
            try:
                from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import (
                    AudioMessage,
                    Message as WaMessage,
                )
                from neonize.utils.enum import MediaType

                upload = await self.client.upload(final_bytes, MediaType.MediaAudio)
                audio_kwargs: dict[str, Any] = {
                    "URL": upload.url,
                    "directPath": upload.DirectPath,
                    "fileEncSHA256": upload.FileEncSHA256,
                    "fileLength": upload.FileLength,
                    "fileSHA256": upload.FileSHA256,
                    "mediaKey": upload.MediaKey,
                    "seconds": duration,
                    "mimetype": mimetype,
                    "PTT": True,
                }
                if waveform:
                    audio_kwargs["waveform"] = waveform

                audio_msg = AudioMessage(**audio_kwargs)
                msg_proto = WaMessage(audioMessage=audio_msg)

                if event_obj is not None and hasattr(self.client, "_make_quoted_message"):
                    try:
                        ctx = self.client._make_quoted_message(event_obj)
                        msg_proto.audioMessage.contextInfo.MergeFrom(ctx)
                    except Exception:
                        logger.warning("event=voice_quote_failed unquoted=yes", exc_info=True)

                await self.client.send_message(chat_obj, msg_proto)
                return True
            except Exception:
                logger.warning("event=direct_voice_send_failed trying_send_audio=yes", exc_info=True)

        # 2. Fallback to client.send_audio (for mock clients or custom wrappers)
        if hasattr(self.client, "send_audio"):
            try:
                await self.client.send_audio(
                    chat_obj,
                    final_bytes,
                    ptt=True,
                    quoted=event_obj,
                )
                return True
            except Exception:
                logger.warning("event=send_audio_quoted_failed fallback_unquoted=yes", exc_info=True)
                await self.client.send_audio(
                    chat_obj,
                    final_bytes,
                    ptt=True,
                )
                return True

        return False

    async def _send_voice_or_fallback(
        self,
        chat_obj: Any,
        text: str,
        event_obj: Any = None,
    ) -> bool:
        """Synthesize and send a voice note (PTT). Falls back to text if disabled/failed."""
        if self.tts_client is not None and (hasattr(self.client, "send_audio") or hasattr(self.client, "upload")):
            try:
                tts_text = sanitize_for_tts(text)
                audio_bytes = await self.tts_client.synthesize(tts_text)
                sent = await self._send_voice_note(chat_obj, audio_bytes, event_obj=event_obj)
                if sent:
                    logger.info("event=voice_reply_sent bytes=%d", len(audio_bytes))
                    return True
            except Exception:
                logger.warning(
                    "event=tts_voice_failed fallback_to_text=yes", exc_info=True
                )
        # Fallback to chat text
        clean_chat_text = sanitize_for_chat(text)
        await self._send_or_reply(chat_obj, clean_chat_text, event_obj)
        return False

    def _is_groq_paused(self) -> bool:
        """Check if the Groq key pool is paused. Resets notification flag on resume."""
        if self.groq_pool is None:
            return False
        if self.groq_pool.is_paused:
            return True
        # Pool resumed (midnight UTC passed) — reset notification flag
        if self._groq_paused_notified:
            self._groq_paused_notified = False
            logger.info("event=groq_pool_resumed notification_flag_reset")
        return False

    async def _handle_groq_exhausted(self, exc: GroqKeyPoolExhausted) -> None:
        """Notify admin once when all Groq keys are exhausted for the day."""
        if self._groq_paused_notified:
            return  # already notified this pause cycle
        self._groq_paused_notified = True
        wait_s = 0.0
        if self.groq_pool:
            wait_s = max(self.groq_pool._paused_until - time.monotonic(), 0) if self.groq_pool._paused_until > 0 else 0
        hours = int(wait_s // 3600)
        mins = int((wait_s % 3600) // 60)
        body = (
            f"\u26a0\ufe0f *Groq API — All Keys Exhausted*\n\n"
            f"All {self.groq_pool.total if self.groq_pool else '?'} API keys have hit their daily rate limits.\n"
            f"The bot will **not reply** to any messages until the limits reset.\n\n"
            f"\u23f3 Estimated resume: ~{hours}h {mins}m (midnight UTC)\n\n"
            f"Messages will continue to be ingested silently. "
            f"To resume earlier, add fresh keys to GROQ_API_KEY and restart."
        )
        logger.critical(
            "event=groq_all_keys_exhausted keys=%s resume_in=%dh%dm",
            self.groq_pool.total if self.groq_pool else "?",
            hours, mins,
        )
        targets = self.inbox.targets()
        for target in targets:
            try:
                await self.client.send_message(target, body)
            except Exception:
                logger.exception("event=groq_exhausted_notify_failed")

    async def start_backends(self) -> None:
        if not self.settings.graphiti_enabled:
            if not self.settings.graphiti_wanted:
                logger.info("event=graphiti_disabled reason=GRAPHITI_ENABLED=false")
            else:
                logger.warning(
                    "event=graphiti_disabled reason=need_GROQ_API_KEY_and_OPENAI_API_KEY"
                )
            return
        store = GraphitiStore(self.settings, pool=self.groq_pool)
        try:
            await store.start()
        except Exception:
            logger.exception(
                "event=graphiti_start_failed continuing_without_long_term_memory"
            )
            self.error_tracker.track(
                Severity.CRITICAL, "graphiti_start_failed",
                "Graphiti failed to start — running without long-term memory",
            )
            return
        self.store = store

        # Initialize health checker now that backends are ready
        self.health_checker = HealthChecker(
            interval_seconds=self.settings.health_check_interval_seconds,
            store=self.store,
            groq_client=self.groq,
            worker_queue=self.queue,
            ingest_queue=self.ingest_queue,
        )

    async def close(self) -> None:
        # Flush any remaining buffered messages before shutdown
        try:
            flushed = await self.ingest_buffer.flush_all_now()
            if flushed:
                await self.flush_ingest()
                logger.info("event=shutdown_flush messages=%s", flushed)
        except Exception:
            logger.exception("event=shutdown_flush_failed")
        if self.store is not None:
            await self.store.close()
            self.store = None

    async def fetch_and_store_roster(self, client: Any, group_jid: str | None) -> None:
        """Fetch group roster on connect and ingest into Graphiti."""
        if not group_jid:
            logger.info("event=roster_skip reason=no_test_group_jid")
            return
        try:
            from neonize.utils.jid import build_jid

            jid_parts = group_jid.split("@")
            if len(jid_parts) == 2:
                gid = build_jid(jid_parts[0], jid_parts[1])
            else:
                gid = build_jid(group_jid)
            members = await fetch_group_roster(client, gid)
            if members:
                self.roster.update(group_jid, members)
                logger.info(
                    "event=roster_stored group=%s members=%s",
                    group_jid,
                    len(members),
                )
        except Exception:
            logger.exception("event=roster_fetch_failed group=%s", group_jid)

    def enqueue(self, job: Job) -> None:
        try:
            self.queue.put_nowait(job)
        except asyncio.QueueFull:
            logger.error("event=worker_queue_full dropping id=%s", getattr(job, "message_id", ""))

    async def run_heartbeat(self) -> None:
        interval = max(float(self.settings.heartbeat_seconds), 10.0)
        while True:
            await asyncio.sleep(interval)
            pool_status = self.groq_pool.status_line() if self.groq_pool else "n/a"
            logger.info(
                "event=heartbeat worker_queue=%s ingest_queue=%s graphiti=%s groq=%s "
                "logged=%s graphiti_episodes=%s notifies=%s groq_keys=%s batch=%s",
                self.queue.qsize(),
                self.ingest_queue.qsize(),
                self.store is not None,
                self.groq is not None,
                self.ingest_count,
                self.graphiti_ingest_count,
                self.notify_count,
                pool_status,
                self.ingest_buffer.status_line(),
            )

    async def run_worker(self) -> None:
        while True:
            job = await self.queue.get()
            try:
                if isinstance(job, IncomingAdminDm):
                    await self._handle_admin_dm(job)
                elif isinstance(job, IncomingUserDm):
                    await self._handle_user_dm(job)
                else:
                    await self._handle_group(job)
            except Exception:
                logger.exception("event=worker_failed")
            finally:
                self.queue.task_done()

    async def run_ingest_worker(self) -> None:
        while True:
            kind, msg, reply, batch = await self.ingest_queue.get()
            try:
                if kind == "batch":
                    await self._ingest_graphiti_batch(batch or [])
                elif kind == "reply":
                    await self._ingest_graphiti_reply(msg, reply or "")
                else:
                    await self._ingest_graphiti_message(msg)
            except Exception:
                mid = msg.message_id if msg else "batch"
                logger.exception("event=ingest_worker_failed id=%s", mid)
            finally:
                self.ingest_queue.task_done()

    def _enqueue_ingest(self, kind: str, msg: IncomingGroupMessage, reply: str | None = None) -> None:
        if self.store is None:
            return
        try:
            self.ingest_queue.put_nowait((kind, msg, reply, None))
        except asyncio.QueueFull:
            logger.error("event=ingest_queue_full dropping id=%s", msg.message_id)

    async def flush_ingest(self) -> None:
        """Test helper: run queued Graphiti jobs on the current task."""
        while not self.ingest_queue.empty():
            kind, msg, reply, batch = self.ingest_queue.get_nowait()
            try:
                if kind == "batch":
                    await self._ingest_graphiti_batch(batch or [])
                elif kind == "reply":
                    await self._ingest_graphiti_reply(msg, reply or "")
                else:
                    await self._ingest_graphiti_message(msg)
            finally:
                self.ingest_queue.task_done()

    async def _handle_admin_dm(self, msg: IncomingAdminDm) -> None:
        logger.info(
            "event=admin_dm sender=%s chat=%s id=%s text=%s",
            msg.sender_jid,
            msg.chat_jid,
            msg.message_id,
            msg.text,
        )
        self.inbox.remember(msg.chat_obj, msg.chat_jid, msg.sender_jid)
        result = handle_admin_dm(
            msg.text,
            self.settings,
            stub_known=self.stub_known,
            recent_formatter=self.recent.format_lines,
            ingest_count=self.ingest_count,
            notify_count=self.notify_count,
            graphiti_ready=self.store is not None,
            graphiti_ingest=self.graphiti_ingest_count,
            admin_ingest_enabled=self.admin_ingest_mode,
        )
        if result.stub_known is not None:
            self.stub_known = result.stub_known
        if result.toggle_ingest is not None:
            self.admin_ingest_mode = result.toggle_ingest

        is_voice = getattr(msg, "is_voice_request", False)
        async with self._typing_presence(msg.chat_obj, is_voice=is_voice):
            if result.run_ingest_text:
                await self._ingest_admin_fact(msg, result.run_ingest_text)
                reply = (
                    f"✅ *Fact Ingested into Knowledge Graph*\n\n"
                    f"\"{result.run_ingest_text}\"\n\n"
                    f"_Added as verified admin knowledge._ 🧠"
                )
            elif result.run_health:
                reply = await self._admin_health_reply()
            elif result.run_report:
                pool_status = self.groq_pool.status_line() if self.groq_pool else ""
                reply = self.daily_reporter.format_report(pool_status=pool_status)
            elif result.run_errors > 0:
                reply = self._admin_errors_reply(result.run_errors)
            elif result.use_groq:
                if self.admin_ingest_mode and not looks_like_question(msg.text) and not is_voice:
                    await self._ingest_admin_fact(msg, msg.text)
                    reply = (
                        f"✅ *Ingested into Knowledge Graph*\n\n"
                        f"\"{msg.text}\"\n\n"
                        f"_Added from admin chat._ 🧠"
                    )
                else:
                    reply = await self._admin_groq_reply(msg.text, is_voice=is_voice)
                    if is_voice:
                        await self._send_voice_or_fallback(msg.chat_obj, reply, getattr(msg, "event_obj", None))
                        logger.info("event=admin_dm_voice_replied chat=%s", msg.chat_jid)
                        return
                    reply = sanitize_for_chat(reply)
            else:
                reply = result.reply
            try:
                await self._send_or_reply(msg.chat_obj, reply, getattr(msg, "event_obj", None))
                logger.info("event=admin_dm_replied chat=%s", msg.chat_jid)
            except Exception:
                logger.exception("event=admin_dm_reply_failed chat=%s", msg.chat_jid)

    async def _ingest_admin_fact(self, msg: IncomingAdminDm, text: str) -> None:
        """Ingest a fact directly from an admin chat as a verified admin episode."""
        sender_digits = "".join(ch for ch in msg.sender_jid.split("@")[0] if ch.isdigit())
        source_tag = f"[from @{sender_digits}]" if sender_digits else "[from admin]"
        fact_with_provenance = f"{source_tag} {text}"
        self.recent.add(
            iso_time=msg.iso_time or datetime.now(timezone.utc).isoformat(),
            group_jid=self.settings.test_group_jid or "admin_chat",
            sender_jid=msg.sender_jid,
            text=fact_with_provenance,
        )
        self.ingest_count += 1
        self.daily_reporter.increment("messages_ingested")
        synthetic_msg = IncomingGroupMessage(
            chat_jid=self.settings.test_group_jid or "top100_afl_admin",
            sender_jid=msg.sender_jid,
            message_id=msg.message_id or uuid.uuid4().hex[:12],
            text=fact_with_provenance,
            is_reply_group=True,
            iso_time=msg.iso_time or datetime.now(timezone.utc).isoformat(),
            push_name=getattr(msg, "push_name", "") or "Admin",
        )
        self._buffer_for_ingest(synthetic_msg)
        logger.info("event=admin_fact_ingested sender=%s text=%s", msg.sender_jid, text)

    async def _admin_health_reply(self) -> str:
        """Run an on-demand health check and return formatted result."""
        if self.health_checker is None:
            return "🩺 *Health Check*\n\nHealth checker not initialized yet."
        try:
            await self.health_checker.check_once()
        except Exception:
            logger.exception("event=admin_health_check_failed")
            return "🩺 *Health Check*\n\n❌ Health check failed to run. See logs."
        return self.health_checker.format_status()

    def _admin_errors_reply(self, n: int) -> str:
        """Format recent errors for the admin."""
        summary = self.error_tracker.format_summary()
        recent = self.error_tracker.format_recent(limit=n)
        return f"{summary}\n\n{recent}"

    async def _admin_groq_reply(self, question: str, is_voice: bool = False) -> str:
        if self.groq is None:
            return "GROQ_API_KEY is not set. Send recent, status, or ping."
        history = self.recent.format_lines(30)
        graphiti_facts, activity_facts = await self._retrieve_answer_context(
            question, history
        )
        facts = [*graphiti_facts, *activity_facts]
        if _is_bot_self_query(question):
            facts = list(dict.fromkeys((*BOT_IDENTITY_FACTS, *facts)))
        if self.store is not None and not (
            self.store.is_grounded(facts) or _history_has_context(history)
        ):
            return (
                "No grounded answer in Graphiti or the recent group log. "
                "State a fact in a connected group, then ask again."
            )
        prompt = assemble_reply_prompt(
            facts=facts,
            history=history,
            sender_jid="admin",
            question=question,
            feedback_contacts=format_feedback_contacts_str(self.settings.feedback_contacts),
            helpers_text=format_helpers_str(self.settings.helpers),
            is_voice=is_voice,
        )
        try:
            text = await self.groq.generate(prompt)
        except GroqKeyPoolExhausted as exc:
            await self._handle_groq_exhausted(exc)
            return "\u26a0\ufe0f All Groq API keys are exhausted for today. Bot will resume at midnight UTC."
        except Exception:
            logger.exception("event=groq_failed source=admin_dm")
            return "Groq call failed. Send recent to see the raw log."
        if is_unknown_reply(text):
            if self.store is not None:
                return "No grounded answer in Graphiti or the recent group log."
            return (
                "No grounded answer in the recent group log (this process only). "
                "Graphiti is not connected yet."
            )
        return text

    async def _handle_group(self, msg: IncomingGroupMessage) -> None:
        display = msg.text or "(non-text)"
        self.ingest_count += 1
        self.daily_reporter.increment("messages_ingested")
        self.daily_reporter.increment_group(msg.chat_jid)
        self.recent.add(
            iso_time=msg.iso_time,
            group_jid=msg.chat_jid,
            sender_jid=msg.sender_jid,
            text=msg.text,
        )
        if msg.text:
            self.summary_store.add_message(
                message_id=msg.message_id,
                chat_jid=msg.chat_jid,
                sender_jid=msg.sender_jid,
                text=msg.text,
                iso_time=msg.iso_time,
                is_group=True,
            )
        logger.info(
            "event=group_message ingest=yes reply_eligible=%s group=%s sender=%s id=%s text=%s",
            msg.is_reply_group,
            msg.chat_jid,
            msg.sender_jid,
            msg.message_id,
            display,
        )

        # PDF document handling — ingest text in background task to avoid blocking worker
        if msg.has_document and msg.document_mimetype.startswith("application/pdf"):
            if self.settings.pdf_ingestion_enabled:
                asyncio.create_task(
                    self._handle_pdf_document(msg),
                    name=f"pdf-ingest-{msg.message_id}",
                )

        # Check for PDF send requests (user asking for a document)
        if self._is_pdf_request(msg.text):
            await self._handle_pdf_request(msg)

        # Check for meeting link and schedule reminders
        if msg.text:
            await self._check_meeting_reminders(msg)

        # Check interactive and manual reminder requests
        if msg.text:
            handled = await self._handle_reminder_flow(
                chat_jid=msg.chat_jid,
                sender_jid=msg.sender_jid,
                message_id=msg.message_id,
                text=msg.text,
                chat_obj=msg.chat_obj,
                event_obj=msg.event_obj,
                iso_time=msg.iso_time,
                is_direct=False,
            )
            if handled:
                self._buffer_for_ingest(msg)
                return

        # Check summary skill requests
        if msg.text and is_summary_request(msg.text):
            await self._handle_summary_flow(
                chat_jid=msg.chat_jid,
                sender_jid=msg.sender_jid,
                message_id=msg.message_id,
                text=msg.text,
                chat_obj=msg.chat_obj,
                event_obj=msg.event_obj,
                iso_time=msg.iso_time,
                is_direct=False,
                is_voice=getattr(msg, "is_voice_request", False),
            )
            self._buffer_for_ingest(msg)
            return

        if not msg.is_reply_group:
            self._buffer_for_ingest(msg)
            return
        trigger = group_message_trigger(msg, self.own_users)
        if trigger is None:
            self._buffer_for_ingest(msg)
            logger.debug("event=ingest_only group=%s id=%s", msg.chat_jid, msg.message_id)
            return
        if trigger == "statement_check":
            # Fact-check pipeline: only correct if Graphiti contradicts
            if self.stub_known:
                await self._check_statement(msg)
            self._buffer_for_ingest(msg)
            return
        if self.stub_known:
            await self._known_reply(msg, trigger)
        else:
            await self._notify_admins(msg, trigger)
        self._buffer_for_ingest(msg)

    async def _known_reply(self, msg: IncomingGroupMessage, trigger: str) -> None:
        is_voice = getattr(msg, "is_voice_request", False) or trigger == "voice"
        if is_voice and not (msg.text or "").strip() and not getattr(msg, "quoted_text", ""):
            voice_help = (
                "🎙️ *Voice Mode Active*\n\n"
                "Please include your question with `/voice`. For example:\n"
                "• `/voice what are the upcoming hackathon deadlines?`\n"
                "• `/voice /summary today`"
            )
            await self._send_voice_or_fallback(msg.chat_obj, voice_help, msg.event_obj)
            return

        async with self._typing_presence(msg.chat_obj, is_voice=is_voice):
            reply, used_facts = await self._generate_known_reply(
                chat_jid=msg.chat_jid,
                sender_jid=msg.sender_jid,
                message_id=msg.message_id,
                text=msg.text,
                allow_stub=True,
                is_voice=is_voice,
                quoted_text=getattr(msg, "quoted_text", ""),
            )
            if not reply:
                logger.info(
                    "event=known_became_unknown trigger=%s group=%s id=%s",
                    trigger,
                    msg.chat_jid,
                    msg.message_id,
                )
                self.daily_reporter.increment("questions_forwarded_to_admin")
                await self._notify_admins(msg, trigger)
                return
            clean_reply = sanitize_for_chat(reply)
            try:
                if is_voice:
                    await self._send_voice_or_fallback(msg.chat_obj, reply, msg.event_obj)
                else:
                    await self._send_or_reply(msg.chat_obj, clean_reply, msg.event_obj)
                self.recent.add_bot_reply(msg.chat_jid, clean_reply, private=False)
                self._enqueue_ingest("reply", msg, clean_reply)
                self.daily_reporter.increment("questions_answered")
                logger.info(
                    "event=group_replied trigger=%s group=%s id=%s voice=%s",
                    trigger,
                    msg.chat_jid,
                    msg.message_id,
                    is_voice,
                )
                # Forward unverified facts to admin for verification
                if used_facts:
                    await self._notify_unverified_info(msg, clean_reply, used_facts)
            except Exception:
                logger.exception(
                    "event=group_reply_failed group=%s id=%s",
                    msg.chat_jid,
                    msg.message_id,
                )

    async def _check_statement(self, msg: IncomingGroupMessage) -> None:
        """Fact-check a statement against Graphiti. Reply only if contradiction found."""
        if self.groq is None or self.store is None:
            return
        correction = await self._generate_correction(msg)
        if not correction:
            logger.debug(
                "event=no_correction group=%s id=%s", msg.chat_jid, msg.message_id
            )
            return
        clean_correction = sanitize_for_chat(correction)
        try:
            await self._send_or_reply(msg.chat_obj, clean_correction, msg.event_obj)
            self.recent.add_bot_reply(msg.chat_jid, clean_correction, private=False)
            self._enqueue_ingest("reply", msg, clean_correction)
            logger.info(
                "event=fact_correction group=%s id=%s",
                msg.chat_jid,
                msg.message_id,
            )
        except Exception:
            logger.exception(
                "event=fact_correction_failed group=%s id=%s",
                msg.chat_jid,
                msg.message_id,
            )

    def _correction_on_cooldown(self, sender_jid: str, text: str) -> bool:
        """Check if we've recently corrected this sender on a similar topic.

        Prevents the bot from arguing when users push back.
        """
        key = f"{sender_jid}:{hash(text[:50])}"
        last = self._correction_cooldowns.get(key, 0.0)
        return (time.monotonic() - last) < self._CORRECTION_COOLDOWN

    def _mark_correction_sent(self, sender_jid: str, text: str) -> None:
        key = f"{sender_jid}:{hash(text[:50])}"
        self._correction_cooldowns[key] = time.monotonic()

    async def _generate_correction(self, msg: IncomingGroupMessage) -> str | None:
        """Generate a polite correction if Graphiti facts contradict the statement.

        Stricter than regular replies: requires ≥ retrieval_min_correction_facts
        and backs off if the user has been corrected recently on this topic.
        """
        if self.groq is None:
            return None
        # Short-circuit: pool is paused until daily reset
        if self._is_groq_paused():
            logger.debug("event=groq_paused_skip_correction id=%s", msg.message_id)
            return None

        # Back-off: don't argue with users who pushed back
        if self._correction_on_cooldown(msg.sender_jid, msg.text):
            logger.info(
                "event=correction_cooldown sender=%s id=%s",
                msg.sender_jid, msg.message_id,
            )
            self.daily_reporter.increment("fact_corrections_suppressed")
            return None

        facts = await self._retrieve_facts(retrieval_query(msg.text, ""))
        # Stricter grounding: need ≥ min_correction_facts
        min_facts = self.settings.retrieval_min_correction_facts
        if len(facts) < min_facts:
            # Not enough facts to be absolutely sure — stay silent
            return None

        # Include recent chat so the LLM can detect if user already pushed back
        recent_chat = self.recent.format_chat(msg.chat_jid, n=5)
        prompt = assemble_correction_prompt(
            statement=msg.text,
            sender_jid=msg.sender_jid,
            facts=facts,
            feedback_contacts=format_feedback_contacts_str(self.settings.feedback_contacts),
        )
        # Append recent context so LLM can see if user already insisted
        if recent_chat and recent_chat != "(none)":
            prompt += f"\n\nRecent chat context (check if user already pushed back):\n{recent_chat}\n"

        try:
            text = await self.groq.chat_completion(
                messages=[
                    {"role": "system", "content": CORRECTION_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                max_completion_tokens=512,
                reasoning_effort="low",
            )
        except GroqKeyPoolExhausted as exc:
            await self._handle_groq_exhausted(exc)
            return None
        except Exception:
            logger.exception("event=groq_correction_failed id=%s", msg.message_id)
            return None
        if is_no_correction(text):
            return None
        self._mark_correction_sent(msg.sender_jid, msg.text)
        self.daily_reporter.increment("fact_corrections_issued")
        return text

    async def _retrieve_facts(self, query: str) -> list[str]:
        if self.store is None:
            return []
        try:
            facts = await self.store.search_facts(query)
        except Exception:
            logger.exception("event=graphiti_search_failed")
            return []
        logger.info("event=graphiti_retrieved count=%s", len(facts))
        return facts

    async def _retrieve_answer_context(
        self, question: str, history: str, quoted_text: str = ""
    ) -> tuple[list[str], list[str]]:
        """Return community-memory facts and current public activity facts separately."""
        graphiti_facts = await self._retrieve_facts(
            retrieval_query(question, history, quoted_text=quoted_text)
        )
        activity_facts: list[str] = []
        if is_activity_question(question, f"{history}\n{quoted_text}"):
            try:
                activities = await self.top100_activities.fetch_relevant(
                    question, f"{history}\n{quoted_text}"
                )
                activity_facts = [self._format_top100_activity(activity) for activity in activities]
            except Exception as exc:
                # Keep this log metadata-only; never include the message or API response.
                logger.warning("event=top100_api_unavailable error=%s", type(exc).__name__)
        return graphiti_facts, activity_facts

    @staticmethod
    def _format_top100_activity(activity: Top100Activity) -> str:
        details = [f"[Top100 public events API] {activity.title}"]
        if activity.summary:
            details.append(f"Summary: {activity.summary}")
        if activity.description:
            details.append(f"Description: {activity.description}")
        if activity.start_at:
            try:
                from zoneinfo import ZoneInfo

                zone = ZoneInfo(activity.timezone or "Africa/Lagos")
                start = activity.start_at.astimezone(zone)
                end = activity.end_at.astimezone(zone) if activity.end_at else None
                time_text = start.strftime("%a %d %b %Y, %I:%M %p %Z")
                if end:
                    time_text += f" to {end.strftime('%I:%M %p %Z')}"
                details.append(f"Time ({activity.timezone or 'Africa/Lagos'}): {time_text}")
            except Exception:
                # A malformed optional timezone should not discard otherwise valid event data.
                details.append(f"Time: {activity.start_at.isoformat()}")
        if activity.location:
            details.append(f"Location: {activity.location}")
        if activity.programme_label:
            details.append(f"Programme: {activity.programme_label}")
        if activity.session_number is not None:
            details.append(f"Session: {activity.session_number}")
        details.append(f"Activity link: {activity.activity_url}")
        return " | ".join(details)

    def _can_answer(self, facts: list[str]) -> bool:
        if self.store is None:
            return True
        return self.store.is_grounded(facts)

    async def _generate_known_reply(
        self,
        *,
        chat_jid: str,
        sender_jid: str,
        message_id: str,
        text: str,
        allow_stub: bool,
        is_voice: bool = False,
        quoted_text: str = "",
    ) -> tuple[str | None, list[str]]:
        """Generate a grounded reply. Returns (reply_text, non_admin_facts).

        non_admin_facts contains facts from non-admin sources that were used,
        so the caller can forward them to admin for verification.
        """
        if self.groq is None:
            return (STUB_GROUP_REPLY if allow_stub else None), []
        # Short-circuit: pool is paused until daily reset
        if self._is_groq_paused():
            logger.debug("event=groq_paused_skip id=%s", message_id)
            return None, []
        history = self.recent.format_chat(chat_jid)
        # Cross-group context: recent messages from other groups
        cross_group = self.recent.format_cross_group(exclude_jid=chat_jid)
        graphiti_facts, activity_facts = await self._retrieve_answer_context(
            text, history, quoted_text
        )
        facts = [*graphiti_facts, *activity_facts]
        self.daily_reporter.increment("graphiti_search_count")
        if _is_bot_self_query(text) or (quoted_text and _is_bot_self_query(quoted_text)):
            facts = list(dict.fromkeys((*BOT_IDENTITY_FACTS, *facts)))

        # Check if this is an open conversational question about today's activity
        if is_today_question(text):
            now_utc = datetime.now(timezone.utc)
            today_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
            today_msgs = self.summary_store.get_messages_in_range(today_start, now_utc)
            today_text = format_messages_for_prompt(today_msgs)
            if not today_text.strip():
                recent_lines = [l for l in self.recent._lines if l.sender_jid != "bot"]
                if recent_lines:
                    today_text = "\n".join(f"{l.sender_jid}: {l.text}" for l in recent_lines)
            if not today_text.strip() and facts:
                today_text = "\n".join(facts)

            if today_text.strip():
                if activity_facts:
                    today_text = f"{today_text}\n\nCurrent public Top100 activities:\n" + "\n".join(activity_facts)
                short_prompt = CONCISE_SUMMARY_PROMPT.format(messages_text=today_text)
                if activity_facts:
                    short_prompt += "\nUse activity details only when supported by the supplied public records and include the activity link verbatim."
                if is_voice:
                    short_prompt += "\n\nNote: The user requested voice mode. Provide fluid spoken conversational prose without raw bullets."
                try:
                    short_summary = await self.groq.generate(short_prompt)
                    if not is_unknown_reply(short_summary):
                        if is_voice:
                            spoken_tip = " For an in-depth, structured report, you can also use the summary skill."
                            return f"{short_summary.strip()}{spoken_tip}", []
                        else:
                            return f"{short_summary.strip()}\n\n{SKILL_RECOMMENDATION_TIP}", []
                except Exception:
                    logger.exception("event=today_concise_summary_failed")

        has_history = _history_has_context(history) or _history_has_context(cross_group)
        if not self._can_answer(facts) and not has_history:
            logger.info(
                "event=retrieval_miss id=%s facts=%s has_history=%s",
                message_id,
                len(facts),
                has_history,
            )
            return None, []
        # Identify non-admin-sourced facts for verification forwarding
        non_admin_facts = _extract_non_admin_facts(graphiti_facts, self.settings.admin_phones)
        # Supplement with roster context if available
        roster_members = self.roster.get(chat_jid)
        if roster_members:
            roster_ctx = format_roster_context(roster_members)
            if roster_ctx:
                facts.append(roster_ctx)
        prompt = assemble_reply_prompt(
            facts=facts,
            history=history,
            sender_jid=sender_jid,
            question=text,
            feedback_contacts=format_feedback_contacts_str(self.settings.feedback_contacts),
            admin_phones=self.settings.admin_phones,
            cross_group_context=cross_group,
            helpers_text=format_helpers_str(self.settings.helpers),
            is_voice=is_voice,
            quoted_text=quoted_text,
        )
        t0 = time.monotonic()
        try:
            reply = await self.groq.generate(prompt)
        except GroqKeyPoolExhausted as exc:
            await self._handle_groq_exhausted(exc)
            self.daily_reporter.increment("groq_errors")
            return None, []
        except Exception:
            logger.exception("event=groq_failed id=%s", message_id)
            self.daily_reporter.increment("groq_errors")
            return None, []
        finally:
            elapsed_ms = (time.monotonic() - t0) * 1000
            self.daily_reporter.increment("groq_calls")
            self.daily_reporter.counters.groq_total_latency_ms += elapsed_ms
        if is_unknown_reply(reply):
            logger.info("event=groq_unknown id=%s", message_id)
            return None, []
        return reply, non_admin_facts

    async def _handle_user_dm(self, msg: IncomingUserDm) -> None:
        if not (msg.text or "").strip():
            return
        self.recent.add(
            iso_time=msg.iso_time,
            group_jid=msg.chat_jid,
            sender_jid=msg.sender_jid,
            text=msg.text,
            private=True,
        )
        if msg.text:
            self.summary_store.add_message(
                message_id=msg.message_id,
                chat_jid=msg.chat_jid,
                sender_jid=msg.sender_jid,
                text=msg.text,
                iso_time=msg.iso_time,
                is_group=False,
            )
        logger.info(
            "event=user_dm sender=%s chat=%s id=%s text=%s",
            msg.sender_jid,
            msg.chat_jid,
            msg.message_id,
            msg.text,
        )
        # Check for PDF requests in DM
        if self._is_pdf_request(msg.text):
            if self._pdf_store:
                await self._handle_pdf_request(msg)
                return

        # Check interactive and manual reminder requests in DM
        handled = await self._handle_reminder_flow(
            chat_jid=msg.chat_jid,
            sender_jid=msg.sender_jid,
            message_id=msg.message_id,
            text=msg.text,
            chat_obj=msg.chat_obj,
            event_obj=msg.event_obj,
            iso_time=msg.iso_time,
            is_direct=True,
        )
        if handled:
            return

        # Check summary skill requests in DM
        if is_summary_request(msg.text):
            await self._handle_summary_flow(
                chat_jid=msg.chat_jid,
                sender_jid=msg.sender_jid,
                message_id=msg.message_id,
                text=msg.text,
                chat_obj=msg.chat_obj,
                event_obj=msg.event_obj,
                iso_time=msg.iso_time,
                is_direct=True,
                is_voice=getattr(msg, "is_voice_request", False),
            )
            return

        if not self.stub_known:
            await self._notify_admins_dm(msg)
            return
        is_voice = getattr(msg, "is_voice_request", False)
        if is_voice and not (msg.text or "").strip():
            voice_help = (
                "🎙️ *Voice Mode Active*\n\n"
                "Please include your question with `/voice`, e.g.:\n"
                "`/voice what are the upcoming deadlines?`"
            )
            await self._send_voice_or_fallback(msg.chat_obj, voice_help, msg.event_obj)
            return

        async with self._typing_presence(msg.chat_obj, is_voice=is_voice):
            reply, used_facts = await self._generate_known_reply(
                chat_jid=msg.chat_jid,
                sender_jid=msg.sender_jid,
                message_id=msg.message_id,
                text=msg.text,
                allow_stub=False,
                is_voice=is_voice,
            )
            if not reply:
                await self._notify_admins_dm(msg)
                return
            clean_reply = sanitize_for_chat(reply)
            try:
                if is_voice:
                    await self._send_voice_or_fallback(msg.chat_obj, reply, msg.event_obj)
                else:
                    await self._send_or_reply(msg.chat_obj, clean_reply, msg.event_obj)
                self.recent.add_bot_reply(msg.chat_jid, clean_reply, private=True)
                logger.info(
                    "event=user_dm_replied chat=%s id=%s voice=%s",
                    msg.chat_jid,
                    msg.message_id,
                    is_voice,
                )
                if used_facts:
                    await self._notify_unverified_info(msg, clean_reply, used_facts)
            except Exception:
                logger.exception("event=user_dm_reply_failed chat=%s", msg.chat_jid)

    async def _ingest_graphiti_message(self, msg: IncomingGroupMessage) -> None:
        if self.store is None or not msg.text:
            return
        try:
            if await self.store.ingest_message(msg):
                self.graphiti_ingest_count += 1
                self.daily_reporter.increment("graphiti_episodes_created")
        except Exception:
            logger.exception(
                "event=graphiti_ingest_failed id=%s group=%s",
                msg.message_id,
                msg.chat_jid,
            )

    async def _ingest_graphiti_reply(self, msg: IncomingGroupMessage, reply: str) -> None:
        if self.store is None or not reply:
            return
        try:
            if await self.store.ingest_reply(msg, reply):
                self.graphiti_ingest_count += 1
                self.daily_reporter.increment("graphiti_episodes_created")
        except Exception:
            logger.exception(
                "event=graphiti_reply_ingest_failed id=%s group=%s",
                msg.message_id,
                msg.chat_jid,
            )

    async def _ingest_graphiti_batch(self, messages: list) -> None:
        """Ingest a batch of buffered messages as a single Graphiti episode."""
        if self.store is None or not messages:
            return
        try:
            if await self.store.ingest_batch(messages):
                self.graphiti_ingest_count += 1
                self.daily_reporter.increment("graphiti_episodes_created")
                self.daily_reporter.increment("graphiti_batch_episodes")
        except Exception:
            logger.exception(
                "event=graphiti_batch_ingest_failed count=%s",
                len(messages),
            )

    def _buffer_for_ingest(self, msg: IncomingGroupMessage) -> None:
        """Add a message to the ingest buffer instead of immediate ingestion.

        If the buffer for this group is full, flush it immediately via the
        ingest queue.
        """
        if self.store is None:
            return
        should_flush = self.ingest_buffer.add(msg)
        if should_flush:
            # Buffer is full for this group — trigger immediate flush
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.ingest_buffer.flush_group(msg.chat_jid))
            except RuntimeError:
                logger.warning("event=buffer_flush_no_loop group=%s", msg.chat_jid)

    async def _on_batch_flush(self, messages: list) -> None:
        """Callback from IngestBuffer — enqueue a batch job for the ingest worker."""
        try:
            self.ingest_queue.put_nowait(("batch", None, None, messages))
        except asyncio.QueueFull:
            logger.error(
                "event=ingest_queue_full_batch dropping count=%s",
                len(messages),
            )

    async def run_ingest_flush_timer(self) -> None:
        """Periodic timer that flushes aged ingest buffers.

        Runs every 60 seconds, flushing any group buffer older than
        ``ingest_batch_interval_seconds``.
        """
        interval = min(self.settings.ingest_batch_interval_seconds, 60.0)
        logger.info(
            "event=ingest_flush_timer_started check_interval=%.0fs batch_window=%.0fs max_batch=%s",
            interval,
            self.settings.ingest_batch_interval_seconds,
            self.settings.ingest_batch_max_messages,
        )
        while True:
            await asyncio.sleep(interval)
            try:
                flushed = await self.ingest_buffer.flush_all()
                if flushed:
                    logger.info("event=ingest_flush_timer_flushed messages=%s", flushed)
            except Exception:
                logger.exception("event=ingest_flush_timer_failed")

    # --- PDF handling ---

    async def _handle_pdf_document(self, msg: IncomingGroupMessage) -> None:
        """Download, extract, and ingest PDF text. Mention existence in group."""
        if not msg.has_document or not msg.document_filename:
            return
        logger.info(
            "event=pdf_received filename=%s group=%s sender=%s id=%s",
            msg.document_filename, msg.chat_jid, msg.sender_jid, msg.message_id,
        )
        # Download PDF bytes via neonize
        try:
            msg_proto = (
                getattr(msg.event_obj, "Message", None)
                if msg.event_obj is not None
                else None
            )
            target = msg_proto if msg_proto is not None else msg.chat_obj
            pdf_bytes = await self.client.download_any(target)
        except Exception:
            logger.exception("event=pdf_download_failed id=%s", msg.message_id)
            return
        if not pdf_bytes:
            logger.warning("event=pdf_download_empty id=%s", msg.message_id)
            return

        # Store for later retrieval if user requests
        self._pdf_store[msg.message_id] = (pdf_bytes, msg.document_filename)

        # Extract text
        extraction = extract_pdf_text(pdf_bytes, msg.document_filename)
        if extraction is None:
            logger.info("event=pdf_no_extractable_text id=%s", msg.message_id)
            return

        # Ingest each chunk into Graphiti
        if self.store is not None:
            from unipod_bot.ingest import parse_iso_time

            for i, chunk in enumerate(extraction.chunks):
                name = pdf_episode_name(msg.message_id, i)
                try:
                    await self.store._add_episode(
                        name=name,
                        body=pdf_episode_body(
                            msg.sender_jid, msg.document_filename, chunk, i
                        ),
                        source_description=pdf_source_description(
                            msg.chat_jid, msg.sender_jid,
                            msg.message_id, msg.document_filename,
                        ),
                        reference_time=parse_iso_time(msg.iso_time),
                    )
                    self.daily_reporter.increment("graphiti_episodes_created")
                except Exception:
                    logger.exception(
                        "event=pdf_ingest_failed chunk=%s id=%s", i, msg.message_id
                    )

            self.daily_reporter.increment("pdfs_ingested")
            self.daily_reporter.counters.pdf_pages_processed += extraction.page_count

            # Optional translation: only if explicitly enabled
            if (
                self.settings.pdf_translation_enabled
                and extraction.language == "en"
                and self.groq is not None
            ):
                await self._ingest_pdf_translation(
                    msg,
                    extraction,
                    target_lang=self.settings.pdf_translation_lang or "fr",
                )

        logger.info(
            "event=pdf_ingested filename=%s pages=%s chunks=%s lang=%s id=%s",
            extraction.filename, extraction.page_count,
            len(extraction.chunks), extraction.language, msg.message_id,
        )

    async def _ingest_pdf_translation(
        self, msg: IncomingGroupMessage, extraction: PdfExtraction, target_lang: str
    ) -> None:
        """Translate PDF text and ingest the translation into Graphiti."""
        if self.groq is None or self.store is None:
            return
        try:
            for i, chunk in enumerate(extraction.chunks):
                prompt = assemble_translation_prompt(chunk, target_lang)
                translated = await self.groq.chat_completion(
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,
                    max_completion_tokens=2048,
                    reasoning_effort=None,
                )
                if not translated:
                    continue
                name = pdf_episode_name(msg.message_id, len(extraction.chunks) + i)
                await self.store._add_episode(
                    name=name,
                    body=pdf_episode_body(
                        msg.sender_jid,
                        f"{extraction.filename} ({target_lang})",
                        translated, i,
                    ),
                    source_description=pdf_source_description(
                        msg.chat_jid, msg.sender_jid,
                        msg.message_id,
                        f"{extraction.filename} (translated:{target_lang})",
                    ),
                    reference_time=datetime.now(timezone.utc),
                )
                self.daily_reporter.increment("pdf_translations")
        except Exception:
            logger.exception("event=pdf_translation_failed id=%s", msg.message_id)

    def _is_pdf_request(self, text: str) -> bool:
        """Check if the message is requesting a PDF document."""
        if not text:
            return False
        lower = text.lower().strip()
        keywords = (
            "send me the pdf", "send the pdf", "send me the document",
            "send the document", "can i get the pdf", "can i get the document",
            "share the pdf", "share the document", "i want the pdf",
            "i want the document", "give me the pdf", "give me the document",
        )
        return any(kw in lower for kw in keywords)

    async def _handle_pdf_request(
        self, msg: IncomingGroupMessage | IncomingUserDm
    ) -> None:
        """Send a stored PDF if the user requests it (group or DM)."""
        if not self._pdf_store:
            return
        # Find the most recent PDF
        for msg_id in reversed(list(self._pdf_store.keys())):
            pdf_bytes, filename = self._pdf_store[msg_id]
            try:
                # Send via neonize document message
                from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import (
                    Message as WaMessage,
                )

                quoted_ev = getattr(msg, "event_obj", None)
                try:
                    await self.client.send_document(
                        msg.chat_obj,
                        pdf_bytes,
                        filename=filename,
                        mimetype="application/pdf",
                        quoted=quoted_ev,
                    )
                except Exception:
                    logger.warning(
                        "event=send_document_quoted_failed fallback_unquoted=yes",
                        exc_info=True,
                    )
                    await self.client.send_document(
                        msg.chat_obj,
                        pdf_bytes,
                        filename=filename,
                        mimetype="application/pdf",
                    )
                logger.info(
                    "event=pdf_sent filename=%s to=%s",
                    filename, msg.chat_jid,
                )
            except Exception:
                logger.exception("event=pdf_send_failed filename=%s", filename)
    async def _handle_reminder_flow(
        self,
        *,
        chat_jid: str,
        sender_jid: str,
        message_id: str,
        text: str,
        chat_obj: Any,
        event_obj: Any = None,
        iso_time: str = "",
        is_direct: bool = False,
    ) -> bool:
        """Handle interactive and manual reminders in groups and DMs. Return True if handled."""
        if not text:
            return False
        base_time = parse_iso_time(iso_time)
        user_phone = "".join(ch for ch in sender_jid.split("@")[0] if ch.isdigit())
        user_tag = f"@{user_phone}" if user_phone else ""

        # Case 1: User is in pending interactive reminder setup
        if self.interactive_reminders.has_pending(chat_jid, sender_jid):
            if is_reminder_cancel_flow(text):
                self.interactive_reminders.clear_pending(chat_jid, sender_jid)
                await self._send_or_reply(chat_obj, "❌ *Reminder setup cancelled.*", event_obj)
                return True
            sched_time = parse_meeting_time(text, base_time)
            if sched_time:
                from unipod_bot.reminders import ManualReminder, extract_reminder_topic
                topic = extract_reminder_topic(text)
                user_digits = "".join(ch for ch in sender_jid.split("@")[0] if ch.isdigit())
                target_tag = f"@{user_digits}" if user_digits else "everyone"
                manual_rem = ManualReminder(
                    scheduled_time=sched_time,
                    topic=topic,
                    sender_jid=sender_jid,
                    target_tag=target_tag,
                )
                self.reminder_store.add_meeting(
                    chat_jid=chat_jid,
                    sender_jid=sender_jid,
                    meeting_link="",
                    meeting_type="reminder",
                    topic=manual_rem.topic,
                    scheduled_time=manual_rem.scheduled_time,
                    status="scheduled",
                )
                self.interactive_reminders.clear_pending(chat_jid, sender_jid)
                confirm_reply = format_manual_reminder_confirmation(manual_rem, now=base_time)
                await self._send_or_reply(chat_obj, confirm_reply, event_obj)
                logger.info(
                    "event=interactive_reminder_created chat=%s user=%s topic=%s time=%s",
                    chat_jid,
                    sender_jid,
                    manual_rem.topic,
                    manual_rem.scheduled_time.isoformat(),
                )
                return True
            else:
                prompt = (
                    f"⏰ *Reminder Setup for {user_tag}*\n\n"
                    f"I couldn't detect the scheduled time from that.\n"
                    f"Please specify *when* (e.g. `in 10 mins`, `at 4:00 PM`) and the topic,\n"
                    f"or reply `cancel` to exit."
                )
                await self._send_or_reply(chat_obj, prompt, event_obj)
                return True

        has_reminder_trigger = (
            is_direct
            or is_bot_mentioned(text, (), self.own_users)
            or "@bot" in text.lower()
            or bool(re.search(r"(?:^|\s)/(?:reminder|remind)\b", text, re.IGNORECASE))
        )
        if not has_reminder_trigger and not is_reminder_intent(text):
            return False

        if not is_direct and not has_reminder_trigger:
            return False

        # Case 2: List active reminders
        if is_reminder_list_request(text):
            active = self.reminder_store.get_active_meetings(chat_jid)
            reply = format_active_reminders_list(active, now=base_time)
            await self._send_or_reply(chat_obj, reply, event_obj)
            return True

        # Case 3: Cancel reminder
        cancel_query = is_reminder_cancel_request(text)
        if cancel_query:
            cancelled = self.reminder_store.cancel_by_query(chat_jid, cancel_query)
            if cancelled:
                reply = (
                    f"✅ *Reminder Cancelled!* 🗑️\n\n"
                    f"• *Topic:* {cancelled.topic}\n"
                    f"• *ID:* `{cancelled.id[:8]}`"
                )
            else:
                reply = f"⚠️ *Reminder Not Found*\n\nNo active reminder matching `{cancel_query}` was found in this chat."
            await self._send_or_reply(chat_obj, reply, event_obj)
            return True

        # Case 4: Complete one-shot reminder
        manual_rem = detect_manual_reminder(text, sender_jid, base_time=base_time)
        if manual_rem:
            self.reminder_store.add_meeting(
                chat_jid=chat_jid,
                sender_jid=sender_jid,
                meeting_link="",
                meeting_type="reminder",
                topic=manual_rem.topic,
                scheduled_time=manual_rem.scheduled_time,
                status="scheduled",
            )
            self.interactive_reminders.clear_pending(chat_jid, sender_jid)
            confirm_reply = format_manual_reminder_confirmation(manual_rem, now=base_time)
            await self._send_or_reply(chat_obj, confirm_reply, event_obj)
            logger.info(
                "event=manual_reminder_created chat=%s user=%s topic=%s time=%s",
                chat_jid,
                sender_jid,
                manual_rem.topic,
                manual_rem.scheduled_time.isoformat(),
            )
            return True

        # Case 5: Empty reminder, help request, or reminder intent without time
        if is_reminder_help_or_empty(text) or is_reminder_intent(text):
            self.interactive_reminders.set_pending(chat_jid, sender_jid)
            prompt = format_reminder_interactive_prompt(user_tag)
            await self._send_or_reply(chat_obj, prompt, event_obj)
            return True

        return False

    # --- Summary Skill ---

    async def _handle_summary_flow(
        self,
        *,
        chat_jid: str,
        sender_jid: str,
        message_id: str,
        text: str,
        chat_obj: Any,
        event_obj: Any = None,
        iso_time: str = "",
        is_direct: bool = False,
        is_voice: bool = False,
    ) -> bool:
        """Handle /summary skill requests for specific dates or timeframes."""
        if not text or not is_summary_request(text):
            return False

        base_time = parse_iso_time(iso_time)
        window = parse_summary_time_range(text, base_time=base_time)
        is_voice = is_voice or "/voice" in text.lower() or getattr(event_obj, "is_voice_request", False)

        if window is None:
            # User sent empty /summary, help, or unknown range
            await self._send_or_reply(chat_obj, SUMMARY_HELP_CARD, event_obj)
            return True

        if window.is_future:
            reply = f"No activity recorded for {window.label} because this date is in the future."
            await self._send_or_reply(chat_obj, reply, event_obj)
            return True

        async with self._typing_presence(chat_obj, is_voice=is_voice):
            # 1. Fetch messages from SummaryStore
            messages = self.summary_store.get_messages_in_range(
                start_time=window.start,
                end_time=window.end,
                only_groups=not is_direct,
            )

            # 2. Also check FalkorDB episodes from GraphitiStore if available
            graphiti_texts: list[str] = []
            if self.store is not None and hasattr(self.store, "get_episodes_in_range"):
                try:
                    graphiti_texts = await self.store.get_episodes_in_range(window.start, window.end)
                except Exception:
                    logger.exception("event=summary_graphiti_episodes_failed")

            # 3. Combine message lines
            formatted_msgs = format_messages_for_prompt(messages)
            if graphiti_texts:
                if formatted_msgs:
                    combined_context = f"{formatted_msgs}\n\n[Archived Community Episodes]\n" + "\n".join(graphiti_texts)
                else:
                    combined_context = "\n".join(graphiti_texts)
            else:
                combined_context = formatted_msgs

            # 4. Fallback: check pending ingest buffer and recent lines if empty
            if not combined_context.strip():
                buf_msgs = []
                for g_buf in self.ingest_buffer._buffers.values():
                    for bm in g_buf.messages:
                        bm_dt = parse_iso_time(bm.iso_time)
                        if window.start <= bm_dt <= window.end:
                            buf_msgs.append({"sender_jid": bm.sender_jid, "iso_time": bm.iso_time, "text": bm.text})
                if buf_msgs:
                    combined_context = format_messages_for_prompt(buf_msgs)

            if not combined_context.strip():
                recent_matches = []
                for line in self.recent._lines:
                    if line.sender_jid != "bot":
                        line_dt = parse_iso_time(line.iso_time)
                        if window.start <= line_dt <= window.end:
                            recent_matches.append({"sender_jid": line.sender_jid, "iso_time": line.iso_time, "text": line.text})
                if recent_matches:
                    combined_context = format_messages_for_prompt(recent_matches)

            if not combined_context.strip():
                reply = f"No community messages or activity were recorded for {window.label}."
                if is_voice:
                    await self._send_voice_or_fallback(chat_obj, reply, event_obj)
                else:
                    await self._send_or_reply(chat_obj, reply, event_obj)
                return True

            if self.groq is None:
                reply = f"Here are the recorded messages for {window.label}:\n\n{combined_context[:1000]}"
                await self._send_or_reply(chat_obj, reply, event_obj)
                return True

            prompt = DETAILED_SUMMARY_PROMPT.format(
                label=window.label,
                messages_text=combined_context,
            )
            if is_voice:
                prompt += "\n\nNote: The user requested voice mode. Provide fluid spoken prose without raw bullets, symbols, or URLs."

            try:
                summary_raw = await self.groq.generate(prompt)
            except GroqKeyPoolExhausted as exc:
                await self._handle_groq_exhausted(exc)
                summary_raw = "⚠️ All Groq API keys are exhausted for today. The summary skill will resume at midnight UTC."
            except Exception:
                logger.exception("event=summary_groq_failed")
                summary_raw = "Failed to generate community summary due to an LLM error."

            clean_reply = sanitize_for_chat(summary_raw)
            if is_voice:
                await self._send_voice_or_fallback(chat_obj, summary_raw, event_obj)
            else:
                await self._send_or_reply(chat_obj, clean_reply, event_obj)

            self.recent.add_bot_reply(chat_jid, clean_reply, private=is_direct)
            self.daily_reporter.increment("questions_answered")
            logger.info(
                "event=summary_replied label=%s voice=%s chat=%s id=%s",
                window.label,
                is_voice,
                chat_jid,
                message_id,
            )
            return True

    # --- Meeting Reminders ---

    def _target_jid(self, chat_jid: str) -> Any:
        parts = chat_jid.split("@")
        if len(parts) == 2:
            return build_jid(parts[0], parts[1])
        return build_jid(chat_jid)

    async def _check_meeting_reminders(self, msg: IncomingGroupMessage) -> None:
        if not msg.text:
            return
        link_info = detect_meeting_link(msg.text)
        if not link_info:
            return
        link, platform = link_info

        # Check if the message indicates a cancellation or opposition
        if _CANCEL_RE.search(msg.text):
            cancelled = self.reminder_store.cancel_by_link(msg.chat_jid, link)
            if cancelled:
                logger.info("event=meeting_cancelled chat=%s link=%s count=%s", msg.chat_jid, link, cancelled)
            alert = format_unverified_meeting_alert(
                chat_jid=msg.chat_jid,
                sender_jid=msg.sender_jid,
                link=link,
                text=msg.text,
                reason="Meeting cancellation or opposition expressed by a member.",
            )
            await self._send_admin_notify(
                alert, trigger="meeting_cancelled", chat=msg.chat_jid, message_id=msg.message_id
            )
            return

        base_time = parse_iso_time(msg.iso_time)
        meeting = extract_meeting_details(msg.text, base_time=base_time)
        if not meeting:
            return

        if meeting.needs_admin_confirmation or meeting.scheduled_time is None:
            # Not enough material to verify or ambiguous time — store and alert admin
            far_future = datetime.now(timezone.utc) + timedelta(days=365)
            self.reminder_store.add_meeting(
                chat_jid=msg.chat_jid,
                sender_jid=msg.sender_jid,
                meeting_link=meeting.meeting_link,
                meeting_type=meeting.meeting_type,
                topic=meeting.topic,
                scheduled_time=far_future,
                status="needs_admin_confirmation",
            )
            alert = format_unverified_meeting_alert(
                chat_jid=msg.chat_jid,
                sender_jid=msg.sender_jid,
                link=meeting.meeting_link,
                text=msg.text,
                reason=meeting.confirmation_reason,
            )
            await self._send_admin_notify(
                alert, trigger="meeting_unverified", chat=msg.chat_jid, message_id=msg.message_id
            )
        else:
            self.reminder_store.add_meeting(
                chat_jid=msg.chat_jid,
                sender_jid=msg.sender_jid,
                meeting_link=meeting.meeting_link,
                meeting_type=meeting.meeting_type,
                topic=meeting.topic,
                scheduled_time=meeting.scheduled_time,
                status="scheduled",
            )
            logger.info(
                "event=meeting_reminder_created chat=%s time=%s type=%s",
                msg.chat_jid, meeting.scheduled_time.isoformat(), meeting.meeting_type
            )

    async def run_reminders(self) -> None:
        """Periodic loop to deliver 30m advance notices and start reminders to groups."""
        while True:
            await asyncio.sleep(10.0)
            try:
                now = datetime.now(timezone.utc)
                # 1. 30-minute advance notices
                due_30m = self.reminder_store.get_due_30m(now)
                for meeting in due_30m:
                    body = format_30m_reminder(meeting)
                    target = self._target_jid(meeting.chat_jid)
                    try:
                        await self.client.send_message(target, body)
                        self.reminder_store.mark_30m_sent(meeting.id)
                        logger.info("event=meeting_30m_notice_sent id=%s chat=%s", meeting.id, meeting.chat_jid)
                    except Exception:
                        logger.exception("event=meeting_30m_notice_failed id=%s", meeting.id)

                # 2. Starting-now notices
                due_start = self.reminder_store.get_due_start(now)
                for meeting in due_start:
                    body = format_start_reminder(meeting)
                    target = self._target_jid(meeting.chat_jid)
                    try:
                        await self.client.send_message(target, body)
                        self.reminder_store.mark_start_sent(meeting.id)
                        logger.info("event=meeting_start_notice_sent id=%s chat=%s", meeting.id, meeting.chat_jid)
                    except Exception:
                        logger.exception("event=meeting_start_notice_failed id=%s", meeting.id)
            except Exception:
                logger.exception("event=run_reminders_loop_error")

    # --- Health check & daily report background tasks ---

    async def run_health_checks(self) -> None:
        """Periodic health check loop with initial delay.

        Waits ``health_check_initial_delay_seconds`` before the first check
        to let all backends stabilise after startup. Subsequent checks run
        every ``health_check_interval_seconds``. Admin is only notified on
        failures (no spam on clean startup).
        """
        if self.health_checker is None:
            # Initialize even without Graphiti
            self.health_checker = HealthChecker(
                interval_seconds=self.settings.health_check_interval_seconds,
                store=self.store,
                groq_client=self.groq,
                worker_queue=self.queue,
                ingest_queue=self.ingest_queue,
            )
        initial_delay = max(self.settings.health_check_initial_delay_seconds, 10.0)
        interval = self.settings.health_check_interval_seconds

        # Wait for backends to stabilise before the first check
        logger.info(
            "event=health_check_scheduled initial_delay=%.0fs interval=%.0fs",
            initial_delay,
            interval,
        )
        await asyncio.sleep(initial_delay)

        while True:
            try:
                status = await self.health_checker.check_once()
                if status.all_ok:
                    self.daily_reporter.increment("health_checks_passed")
                else:
                    self.daily_reporter.increment("health_checks_failed")
                    # Send critical alert for backend failures
                    for err in status.errors:
                        self.error_tracker.track(
                            Severity.CRITICAL,
                            f"health_{err.lower().replace(' ', '_')}",
                            err,
                        )
            except Exception:
                logger.exception("event=health_check_failed")
                self.daily_reporter.increment("health_checks_failed")
            await asyncio.sleep(interval)

    async def run_daily_report(self) -> None:
        """Daily report loop — sends to admin DMs only, NEVER to groups."""
        while True:
            await asyncio.sleep(60)  # Check every minute
            if not self.daily_reporter.should_report_now():
                continue
            pool_status = self.groq_pool.status_line() if self.groq_pool else ""
            report = self.daily_reporter.format_report(pool_status=pool_status)
            # SAFETY: Only send to admin targets, never to groups
            targets = self.inbox.targets()
            if not targets:
                logger.warning("event=daily_report_skipped reason=no_admin_target")
                self.daily_reporter.mark_reported()
                continue
            for target in targets:
                target_str = jid_str(target).lower()
                # HARDCODED SAFETY: Reject group JIDs
                if target_str.endswith("@g.us"):
                    logger.error(
                        "event=daily_report_blocked reason=group_jid target=%s",
                        target_str,
                    )
                    continue
                try:
                    await self.client.send_message(target, report)
                except Exception:
                    logger.exception(
                        "event=daily_report_send_failed target=%s", target_str
                    )
            self.daily_reporter.mark_reported()

    # --- Admin notification ---

    async def _notify_admins_dm(self, msg: IncomingUserDm) -> None:
        await self._send_admin_notify(format_unknown_dm(msg), trigger="dm", chat=msg.chat_jid, message_id=msg.message_id)

    async def _notify_admins(self, msg: IncomingGroupMessage, trigger: str) -> None:
        await self._send_admin_notify(
            format_unknown_group(msg, trigger),
            trigger=trigger,
            chat=msg.chat_jid,
            message_id=msg.message_id,
        )

    async def _notify_unverified_info(
        self,
        msg: IncomingGroupMessage | IncomingUserDm,
        reply: str,
        non_admin_facts: list[str],
    ) -> None:
        """Forward unverified (non-admin-sourced) facts to admin for verification.

        Called after a successful group reply that cited non-admin sources.
        """
        body = format_unverified_info(msg, reply, non_admin_facts)
        targets = self.inbox.targets()
        if not targets:
            logger.warning("event=unverified_notify_skipped reason=no_admin_target")
            return
        sent = 0
        for target in targets:
            try:
                await self.client.send_message(target, body)
                sent += 1
            except Exception:
                logger.exception("event=unverified_notify_failed target=%s", jid_str(target))
        if sent:
            self.daily_reporter.increment("unverified_info_forwarded")
            logger.info(
                "event=unverified_info_forwarded group=%s id=%s facts=%s targets=%s",
                msg.chat_jid,
                msg.message_id,
                len(non_admin_facts),
                sent,
            )

    async def _send_admin_notify(
        self,
        body: str,
        *,
        trigger: str,
        chat: str,
        message_id: str,
    ) -> None:
        targets = self.inbox.targets()
        if not targets:
            logger.warning("event=admin_notify_skipped reason=no_admin_target")
            return
        sent = 0
        for target in targets:
            try:
                await self.client.send_message(target, body)
                sent += 1
            except Exception:
                logger.exception("event=admin_notify_failed target=%s", jid_str(target))
        if sent:
            self.notify_count += 1
            logger.info(
                "event=admin_notified trigger=%s group=%s id=%s targets=%s",
                trigger,
                chat,
                message_id,
                sent,
            )
