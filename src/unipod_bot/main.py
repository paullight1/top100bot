from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from neonize.aioze.client import NewAClient
from neonize.aioze.events import ConnectedEv, MessageEv, PairStatusEv

from unipod_bot.config import Settings, load_settings
from unipod_bot.incoming import SkipReason, classify_message
from unipod_bot.logging_setup import configure_logging
from unipod_bot.runtime import Runtime

logger = logging.getLogger("unipod_bot")


def register_handlers(client: NewAClient, settings: Settings, runtime: Runtime) -> None:
    @client.event(PairStatusEv)
    async def on_paired(_: NewAClient, event: PairStatusEv) -> None:
        identity = getattr(event, "ID", None)
        user = getattr(identity, "User", None)
        logger.info("event=paired user=%s", user or event)
        runtime.remember_own(identity)

    @client.event(ConnectedEv)
    async def on_connected(client: NewAClient, __: ConnectedEv) -> None:
        logger.info("event=connected")
        me = getattr(client, "me", None)
        runtime.remember_own(getattr(me, "ID", None) or me)
        try:
            device = await client.get_me()
            runtime.remember_own(device)
        except Exception:
            logger.exception("event=get_me_failed")
        logger.info("event=own_users users=%s", ",".join(sorted(runtime.own_users)) or "(none)")
        logger.info(
            "group_scope=%s reply_group=%s admins=%s debug=%s groq=%s graphiti=%s routing=step6_retrieval_heartbeat",
            "all_groups" if "*" in settings.test_group_jids else "test_allowlist" if settings.test_group_jids else "disabled",
            settings.test_group_jid or "(unset)",
            ",".join(settings.admin_phones) or "(none)",
            settings.debug,
            f"on:{settings.groq_model}" if settings.groq_enabled else "off",
            "on:retrieval" if runtime.store is not None else "off",
        )
        # Do not fetch participant rosters or private member profiles.

    @client.event(MessageEv)
    async def on_message(_: NewAClient, event: MessageEv) -> None:
        classified = classify_message(event, settings)
        if isinstance(classified, SkipReason):
            if classified is SkipReason.GROUP_NOT_ALLOWED:
                from unipod_bot.jids import jid_str

                source = getattr(getattr(event, "Info", None), "MessageSource", None)
                chat = getattr(source, "Chat", None)
                logger.info("event=group_ignored reason=%s group=%s", classified.value, jid_str(chat))
            else:
                logger.debug("event=message_skipped reason=%s", classified.value)
            return
        runtime.enqueue(classified)


def log_startup(settings: Settings) -> None:
    session_path = settings.whatsapp_session_path
    session_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("event=startup session=%s", session_path.resolve())
    logger.info(
        "event=config debug=%s log_level=%s test_group=%s admin_count=%s groq=%s graphiti=%s",
        settings.debug,
        logging.getLevelName(settings.log_level),
        settings.test_group_jid or "",
        len(settings.admin_phones),
        f"on({len(settings.groq_api_keys)} keys)" if settings.groq_enabled else "off",
        "on" if settings.graphiti_enabled else "off",
    )


async def async_main(settings: Settings) -> None:
    log_startup(settings)
    client = NewAClient(str(settings.whatsapp_session_path))
    runtime = Runtime(settings, client)
    register_handlers(client, settings, runtime)
    await runtime.start_backends()
    worker = asyncio.create_task(runtime.run_worker(), name="unipod-worker")
    ingest_worker = asyncio.create_task(runtime.run_ingest_worker(), name="unipod-ingest")
    ingest_flush = asyncio.create_task(runtime.run_ingest_flush_timer(), name="unipod-ingest-flush")
    heartbeat = asyncio.create_task(runtime.run_heartbeat(), name="unipod-heartbeat")
    health_check = asyncio.create_task(runtime.run_health_checks(), name="unipod-health")
    daily_report = asyncio.create_task(runtime.run_daily_report(), name="unipod-daily-report")
    reminders = asyncio.create_task(runtime.run_reminders(), name="unipod-reminders")
    logger.info("event=connecting hint=scan_qr_if_first_run")
    try:
        await client.connect()
        await client.idle()
    except asyncio.CancelledError:
        raise
    finally:
        logger.info("event=stopping")
        all_tasks = (worker, ingest_worker, ingest_flush, heartbeat, health_check, daily_report, reminders)
        for task in all_tasks:
            task.cancel()
        for task in all_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        try:
            await runtime.close()
        except Exception:
            logger.exception("event=runtime_close_failed")
        try:
            await client.stop()
        except Exception:
            logger.exception("event=stop_failed")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="top100-afl-bot")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Load config, configure logging, and exit without connecting to WhatsApp.",
    )
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    settings = load_settings()
    configure_logging(level=settings.log_level, debug=settings.debug)
    if args.check:
        log_startup(settings)
        logger.info("event=check_ok")
        return
    try:
        asyncio.run(async_main(settings))
    except KeyboardInterrupt:
        logger.info("event=interrupted")


if __name__ == "__main__":
    run(sys.argv[1:])
