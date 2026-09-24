"""Tests for cross-group content retrieval."""

from unipod_bot.recent import RecentGroupLog


def test_format_cross_group_excludes_current_chat():
    """format_cross_group should return messages from other groups only."""
    log = RecentGroupLog(50)
    log.add(iso_time="t1", group_jid="group-A@g.us", sender_jid="alice", text="Hello from A")
    log.add(iso_time="t2", group_jid="group-B@g.us", sender_jid="bob", text="Hello from B")
    log.add(iso_time="t3", group_jid="group-A@g.us", sender_jid="alice", text="Another from A")

    result = log.format_cross_group(exclude_jid="group-A@g.us")
    assert "Hello from B" in result
    assert "Hello from A" not in result
    assert "Another from A" not in result


def test_format_cross_group_includes_group_jid():
    """Cross-group output should include the source group JID prefix."""
    log = RecentGroupLog(50)
    log.add(iso_time="t1", group_jid="group-B@g.us", sender_jid="bob", text="Info here")

    result = log.format_cross_group(exclude_jid="group-A@g.us")
    assert "[group-B@g.us]" in result


def test_format_cross_group_empty_when_all_same():
    """If all messages are from the excluded group, result should be empty."""
    log = RecentGroupLog(50)
    log.add(iso_time="t1", group_jid="group-A@g.us", sender_jid="alice", text="Only A")

    result = log.format_cross_group(exclude_jid="group-A@g.us")
    assert result == ""


def test_format_cross_group_respects_limit():
    """Cross-group output should respect the n limit."""
    log = RecentGroupLog(50)
    for i in range(20):
        log.add(
            iso_time=f"t{i}",
            group_jid="group-B@g.us",
            sender_jid="bob",
            text=f"Message {i}",
        )

    result = log.format_cross_group(exclude_jid="group-A@g.us", n=5)
    lines = [l for l in result.strip().splitlines() if l.strip()]
    assert len(lines) == 5
    # Should have the most recent messages
    assert "Message 19" in result
    assert "Message 15" in result


def test_format_chat_still_per_group():
    """format_chat should still only show the requested group."""
    log = RecentGroupLog(50)
    log.add(iso_time="t1", group_jid="group-A@g.us", sender_jid="alice", text="A msg")
    log.add(iso_time="t2", group_jid="group-B@g.us", sender_jid="bob", text="B msg")

    result = log.format_chat("group-A@g.us")
    assert "A msg" in result
    assert "B msg" not in result
