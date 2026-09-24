from __future__ import annotations

from typing import Any

from neonize.utils.jid import Jid2String

GROUP_SERVER = "g.us"


def jid_str(jid: Any) -> str:
    if jid is None:
        return ""
    user = getattr(jid, "User", None)
    server = getattr(jid, "Server", None)
    if user and server:
        return f"{user}@{server}"
    try:
        return Jid2String(jid)
    except Exception:
        return str(jid)


def jid_user(jid: Any) -> str:
    if jid is None:
        return ""
    user = getattr(jid, "User", None)
    if user:
        return str(user)
    text = jid_str(jid)
    return text.split("@", 1)[0] if text else ""


def is_group_jid(jid: Any) -> bool:
    server = getattr(jid, "Server", None)
    if server:
        return server == GROUP_SERVER
    return jid_str(jid).endswith(f"@{GROUP_SERVER}")


def phone_from_jid(jid: Any) -> str:
    user = getattr(jid, "User", None)
    if user:
        return "".join(ch for ch in str(user) if ch.isdigit())
    text = jid_str(jid)
    local = text.split("@", 1)[0]
    return "".join(ch for ch in local if ch.isdigit())
