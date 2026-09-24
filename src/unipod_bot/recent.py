from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass(frozen=True)
class GroupLine:
    iso_time: str
    group_jid: str
    sender_jid: str
    text: str


class RecentGroupLog:
    def __init__(self, max_messages: int = 50) -> None:
        self._max = max(max_messages, 1)
        self._lines: deque[GroupLine] = deque(maxlen=self._max)
        self._by_chat: dict[str, deque[GroupLine]] = defaultdict(
            lambda: deque(maxlen=self._max)
        )

    def add(
        self,
        *,
        iso_time: str,
        group_jid: str,
        sender_jid: str,
        text: str,
        private: bool = False,
    ) -> None:
        body = (text or "").strip()
        if not body:
            return
        line = GroupLine(
            iso_time=iso_time,
            group_jid=group_jid,
            sender_jid=sender_jid,
            text=body,
        )
        if not private:
            self._lines.append(line)
        self._by_chat[group_jid].append(line)

    def add_bot_reply(self, group_jid: str, text: str, *, private: bool = False) -> None:
        self.add(
            iso_time="",
            group_jid=group_jid,
            sender_jid="bot",
            text=text,
            private=private,
        )

    def __len__(self) -> int:
        return len(self._lines)

    def format_lines(self, n: int = 15) -> str:
        items = list(self._lines)[-max(1, min(n, 50)) :]
        if not items:
            return "(no group text logged yet this process)"
        return "\n".join(
            f"{line.iso_time} {line.group_jid} {line.sender_jid}: {line.text}"
            for line in items
        )

    def format_chat(self, group_jid: str, n: int = 15) -> str:
        items = list(self._by_chat.get(group_jid, ()))[-max(1, min(n, 50)) :]
        if not items:
            return "(none)"
        lines = []
        for line in items:
            if line.sender_jid == "bot":
                lines.append(f"bot: {line.text}")
            else:
                phone = "".join(ch for ch in line.sender_jid.split("@")[0] if ch.isdigit())
                sender = f"@{phone}" if phone else line.sender_jid
                lines.append(f"{sender}: {line.text}")
        return "\n".join(lines)

    def format_cross_group(self, exclude_jid: str, n: int = 10) -> str:
        """Return recent messages from ALL groups except `exclude_jid`.

        Used for cross-group context blending so the LLM can reference
        knowledge shared in other community groups.
        """
        items = [
            line for line in self._lines
            if line.group_jid != exclude_jid
        ][-max(1, min(n, 30)):]
        if not items:
            return ""
        lines = []
        for line in items:
            if line.sender_jid == "bot":
                lines.append(f"[{line.group_jid}] bot: {line.text}")
            else:
                phone = "".join(ch for ch in line.sender_jid.split("@")[0] if ch.isdigit())
                sender = f"@{phone}" if phone else line.sender_jid
                lines.append(f"[{line.group_jid}] {sender}: {line.text}")
        return "\n".join(lines)

