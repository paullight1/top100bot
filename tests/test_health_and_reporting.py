"""Tests for health checks, daily reports, and error tracking."""

import asyncio
from datetime import datetime, timezone
import logging
from pathlib import Path
import time

from unipod_bot.daily_report import DailyCounters, DailyReporter
from unipod_bot.error_tracker import ErrorTracker, Severity, TrackedError
from unipod_bot.health import HealthChecker, HealthStatus


# --- HealthChecker ---

def test_health_status_all_ok():
    status = HealthStatus(
        falkordb_ok=True,
        graphiti_ok=True,
        groq_ok=True,
        whatsapp_connected=True,
    )
    assert status.all_ok


def test_health_status_not_ok():
    status = HealthStatus(falkordb_ok=False, graphiti_ok=True, groq_ok=True, whatsapp_connected=True)
    assert not status.all_ok


def test_health_status_errors_make_not_ok():
    status = HealthStatus(
        falkordb_ok=True, graphiti_ok=True, groq_ok=True, whatsapp_connected=True,
        errors=["test error"],
    )
    assert not status.all_ok


def test_health_checker_format_before_check():
    checker = HealthChecker(interval_seconds=300)
    assert "No health check" in checker.format_status()


def test_health_checker_counts():
    checker = HealthChecker(interval_seconds=300)
    assert checker.check_count == 0
    assert checker.fail_count == 0


# --- DailyReporter ---

def test_daily_counters_default():
    c = DailyCounters()
    assert c.messages_ingested == 0
    assert c.groq_avg_latency_ms == 0.0


def test_daily_counters_avg_latency():
    c = DailyCounters(groq_calls=10, groq_total_latency_ms=5000.0)
    assert c.groq_avg_latency_ms == 500.0


def test_daily_reporter_increment():
    reporter = DailyReporter()
    reporter.increment("messages_ingested", 5)
    assert reporter.counters.messages_ingested == 5
    reporter.increment("messages_ingested")
    assert reporter.counters.messages_ingested == 6


def test_daily_reporter_increment_group():
    reporter = DailyReporter()
    reporter.increment_group("group-A@g.us")
    reporter.increment_group("group-A@g.us")
    reporter.increment_group("group-B@g.us")
    assert reporter.counters.messages_per_group == {
        "group-A@g.us": 2,
        "group-B@g.us": 1,
    }


def test_daily_reporter_format_report():
    reporter = DailyReporter()
    reporter.increment("messages_ingested", 42)
    reporter.increment("questions_answered", 10)
    reporter.increment("groq_calls", 15)
    report = reporter.format_report()
    assert "42" in report
    assert "10" in report
    assert "Daily Bot Report" in report


def test_daily_reporter_mark_reported():
    reporter = DailyReporter()
    reporter.increment("messages_ingested", 100)
    reporter.mark_reported()
    assert reporter.counters.messages_ingested == 0
    assert reporter.report_count == 1


def test_daily_reporter_should_report_checks_date():
    """After marking reported, should_report_now returns False for same day."""
    reporter = DailyReporter(report_hour_utc=0)
    reporter.mark_reported()
    assert not reporter.should_report_now()


# --- ErrorTracker ---

def test_error_tracker_track():
    tracker = ErrorTracker(cooldown_seconds=600)
    error = tracker.track(Severity.ERROR, "test_type", "test message")
    assert tracker.total_errors == 1
    assert tracker.total_critical == 0
    assert error.error_type == "test_type"


def test_error_tracker_critical():
    tracker = ErrorTracker(cooldown_seconds=600)
    tracker.track(Severity.CRITICAL, "crash", "bot crashed")
    assert tracker.total_critical == 1


def test_error_tracker_rate_limiting():
    """Second critical alert of same type within cooldown should not trigger callback."""
    alerts_sent = []

    def mock_notify(body: str):
        alerts_sent.append(body)

    tracker = ErrorTracker(cooldown_seconds=600, admin_notify=mock_notify)
    tracker.track(Severity.CRITICAL, "falkordb_down", "FalkorDB unreachable")
    tracker.track(Severity.CRITICAL, "falkordb_down", "FalkorDB still unreachable")

    # Only first should have triggered the callback
    assert len(alerts_sent) == 1
    assert "CRITICAL" in alerts_sent[0]


def test_error_tracker_different_types_not_rate_limited():
    """Different error types should not be rate-limited against each other."""
    alerts_sent = []

    def mock_notify(body: str):
        alerts_sent.append(body)

    tracker = ErrorTracker(cooldown_seconds=600, admin_notify=mock_notify)
    tracker.track(Severity.CRITICAL, "falkordb_down", "FalkorDB")
    tracker.track(Severity.CRITICAL, "groq_down", "Groq API")

    assert len(alerts_sent) == 2


def test_error_tracker_get_recent():
    tracker = ErrorTracker(cooldown_seconds=600)
    tracker.track(Severity.WARNING, "slow", "slow query")
    tracker.track(Severity.ERROR, "fail", "ingest failed")
    tracker.track(Severity.CRITICAL, "crash", "crash")

    recent = tracker.get_recent()
    assert len(recent) == 3

    errors_only = tracker.get_recent(severity=Severity.ERROR)
    assert len(errors_only) == 1
    assert errors_only[0].error_type == "fail"


def test_error_tracker_format_summary():
    tracker = ErrorTracker(cooldown_seconds=600)
    tracker.track(Severity.CRITICAL, "a", "a")
    tracker.track(Severity.ERROR, "b", "b")
    tracker.track(Severity.WARNING, "c", "c")
    tracker.track(Severity.WARNING, "d", "d")

    summary = tracker.format_summary()
    assert "Critical: 1" in summary
    assert "Errors: 1" in summary
    assert "Warnings: 2" in summary


def test_error_tracker_trims_old_errors():
    """Tracker should trim errors when exceeding 500 entries."""
    tracker = ErrorTracker(cooldown_seconds=600)
    for i in range(600):
        tracker.track(Severity.WARNING, f"type_{i}", f"msg_{i}")

    recent = tracker.get_recent(limit=1000)
    assert len(recent) <= 500


# --- New Tests for Fixed Report Time, Persistence & Health Check ---

def test_parse_daily_report_time():
    from unipod_bot.config import parse_daily_report_time

    assert parse_daily_report_time("18:00") == (18, 0)
    assert parse_daily_report_time("20:30") == (20, 30)
    assert parse_daily_report_time("9:05") == (9, 5)
    assert parse_daily_report_time("21") == (21, 0)
    assert parse_daily_report_time("invalid") == (0, 0)
    assert parse_daily_report_time(None, default_hour=19, default_minute=15) == (19, 15)
    assert parse_daily_report_time("25:99") == (23, 59)


def test_daily_reporter_startup_skip_if_time_passed(tmp_path: Path):
    """If bot starts up after the scheduled report time, today's report should be skipped on startup."""
    now = datetime.now(timezone.utc)
    # Schedule report 1 hour before current time (already passed today)
    past_hour = (now.hour - 1) % 24
    if now.hour > 0:
        reporter = DailyReporter(report_hour_utc=past_hour, report_minute_utc=0, state_file=tmp_path / "state.json")
        today = now.strftime("%Y-%m-%d")
        assert reporter.last_report_date == today
        # should_report_now should return False because startup skipped today
        assert not reporter.should_report_now()


def test_daily_reporter_persistence(tmp_path: Path):
    """Counters and last_report_date should persist across reporter restarts."""
    state_file = tmp_path / "daily_state.json"
    reporter1 = DailyReporter(report_hour_utc=23, report_minute_utc=59, state_file=state_file)
    reporter1.increment("messages_ingested", 15)
    reporter1.increment_group("group1@g.us")

    # Second instance loads from same state file
    reporter2 = DailyReporter(report_hour_utc=23, report_minute_utc=59, state_file=state_file)
    assert reporter2.counters.messages_ingested == 15
    assert reporter2.counters.messages_per_group == {"group1@g.us": 1}


def test_daily_counters_serialization():
    c = DailyCounters(messages_ingested=20, groq_calls=5, groq_total_latency_ms=1000.0)
    d = c.to_dict()
    assert d["messages_ingested"] == 20
    c2 = DailyCounters.from_dict(d)
    assert c2.messages_ingested == 20
    assert c2.groq_avg_latency_ms == 200.0


def test_logging_handler_ignores_tracked_error_cascade():
    """ErrorTrackingHandler must ignore event=tracked_error logs to avoid infinite or cascading critical alerts."""
    from unipod_bot.error_tracker import ErrorTrackingHandler

    alerts = []
    tracker = ErrorTracker(cooldown_seconds=600, admin_notify=lambda msg: alerts.append(msg))
    handler = ErrorTrackingHandler(tracker)

    # Directly emit a record with event=tracked_error
    record = logging.LogRecord(
        name="unipod_bot",
        level=logging.CRITICAL,
        pathname=__file__,
        lineno=10,
        msg="event=tracked_error severity=critical type=health_graphiti_search_unresponsive msg=Graphiti search unresponsive",
        args=(),
        exc_info=None,
    )
    handler.emit(record)

    # Should not have tracked or alerted
    assert len(alerts) == 0
    assert tracker.total_errors == 0


def test_admin_dm_report_command():
    from unipod_bot.admin import handle_admin_dm
    from unipod_bot.config import Settings

    settings = Settings(
        whatsapp_session_path=Path("session.db"),
        test_group_jid=None,
        debug=False,
        log_level=logging.INFO,
        falkordb_host="localhost",
        falkordb_port=6379,
        falkordb_database="test",
        falkordb_username=None,
        falkordb_password=None,
        graphiti_group_id="test",
        admin_phones=("2348011111111",),
    )
    result = handle_admin_dm(
        "report",
        settings,
        stub_known=True,
        recent_formatter=lambda n: "",
        ingest_count=0,
        notify_count=0,
    )
    assert result.run_report is True

