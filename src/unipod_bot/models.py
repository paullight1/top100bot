from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class IncomingMessage:
    chat_jid: str
    sender_jid: str
    sender_user: str
    message_id: str
    text: str
    timestamp: datetime
    is_group: bool
    is_from_me: bool
    mentioned_users: tuple[str, ...]
    raw: object

    @property
    def iso_time(self) -> str:
        ts = self.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.isoformat()
