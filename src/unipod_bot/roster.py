"""Group roster management — member and admin awareness.

On connect, the bot fetches group participant lists via neonize's
get_group_info(). The roster is stored in-memory and ingested into Graphiti
as a structured episode so the bot can answer "who is @user?", "who are the
admins?", and "who is on [team]?" queries.

Roster data is refreshed on reconnect and can be updated when neonize fires
group-participant-change events (future enhancement).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from unipod_bot.jids import jid_str, jid_user

logger = logging.getLogger("unipod_bot")


@dataclass(frozen=True)
class MemberInfo:
    """A single group participant."""

    jid: str
    phone: str
    is_admin: bool = False
    is_super_admin: bool = False
    display_name: str = ""


def _extract_participants(group_info: Any) -> list[MemberInfo]:
    """Extract participants from neonize GroupInfo object."""
    participants = getattr(group_info, "Participants", None) or []
    members: list[MemberInfo] = []
    for p in participants:
        p_jid = getattr(p, "JID", None)
        if p_jid is None:
            continue
        jid_s = jid_str(p_jid)
        user = jid_user(p_jid)
        is_admin = bool(getattr(p, "IsAdmin", False))
        is_super_admin = bool(getattr(p, "IsSuperAdmin", False))
        members.append(
            MemberInfo(
                jid=jid_s,
                phone=user,
                is_admin=is_admin or is_super_admin,
                is_super_admin=is_super_admin,
                display_name="",
            )
        )
    return members


async def fetch_group_roster(client: Any, group_jid: Any) -> list[MemberInfo]:
    """Fetch group participants via neonize get_group_info()."""
    try:
        info = await client.get_group_info(group_jid)
    except Exception:
        logger.exception("event=roster_fetch_failed group=%s", jid_str(group_jid))
        return []
    name = getattr(info, "GroupName", None) or getattr(info, "Name", None) or ""
    members = _extract_participants(info)
    admin_count = sum(1 for m in members if m.is_admin)
    logger.info(
        "event=roster_fetched group=%s name=%s members=%s admins=%s",
        jid_str(group_jid),
        name,
        len(members),
        admin_count,
    )
    return members


def roster_to_episode(group_jid: str, members: list[MemberInfo]) -> str:
    """Format roster as a Graphiti episode body for ingestion.

    The episode is structured so Graphiti can extract entities (people)
    and relationships (member_of, admin_of).
    """
    lines = [f"Group roster for {group_jid}:"]
    admins = [m for m in members if m.is_admin]
    regulars = [m for m in members if not m.is_admin]
    if admins:
        lines.append(f"Admins ({len(admins)}): " + ", ".join(
            f"@{m.phone}" + (f" ({m.display_name})" if m.display_name else "")
            for m in admins
        ))
    if regulars:
        lines.append(f"Members ({len(regulars)}): " + ", ".join(
            f"@{m.phone}" + (f" ({m.display_name})" if m.display_name else "")
            for m in regulars
        ))
    lines.append(f"Total participants: {len(members)}")
    return "\n".join(lines)


def format_roster_context(members: list[MemberInfo], query_phone: str = "") -> str:
    """Format roster info for inclusion in LLM prompts.

    If query_phone is set, highlight that specific member's info.
    """
    if not members:
        return ""
    lines = ["Group members:"]
    for m in members:
        role = "super admin" if m.is_super_admin else ("admin" if m.is_admin else "member")
        name_part = f" ({m.display_name})" if m.display_name else ""
        highlight = " ← (queried)" if query_phone and m.phone == query_phone else ""
        lines.append(f"  @{m.phone}{name_part}: {role}{highlight}")
    return "\n".join(lines)


@dataclass
class RosterStore:
    """In-memory roster cache keyed by group JID."""

    _rosters: dict[str, list[MemberInfo]] = field(default_factory=dict)

    def update(self, group_jid: str, members: list[MemberInfo]) -> None:
        self._rosters[group_jid] = members

    def get(self, group_jid: str) -> list[MemberInfo]:
        return self._rosters.get(group_jid, [])

    def get_all_members(self) -> list[MemberInfo]:
        """All members across all groups (deduplicated by phone)."""
        seen: set[str] = set()
        result: list[MemberInfo] = []
        for members in self._rosters.values():
            for m in members:
                if m.phone not in seen:
                    seen.add(m.phone)
                    result.append(m)
        return result

    def find_member(self, phone_or_jid: str) -> MemberInfo | None:
        """Find a member by phone number or JID across all groups."""
        needle = "".join(ch for ch in phone_or_jid if ch.isdigit()) if phone_or_jid else ""
        if not needle:
            return None
        for members in self._rosters.values():
            for m in members:
                if m.phone == needle or m.jid.startswith(needle):
                    return m
        return None
