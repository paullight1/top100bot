from __future__ import annotations

from unipod_bot.incoming import IncomingGroupMessage, IncomingUserDm
from unipod_bot.jids import phone_from_jid


def format_sender_display(sender_jid: str, push_name: str = "") -> str:
    """Format sender as '@phone (Name)' or '@phone' or raw JID for easy identification."""
    phone = phone_from_jid(sender_jid)
    name = (push_name or "").strip()
    if phone:
        if name:
            return f"@{phone} ({name})"
        return f"@{phone}"
    if name:
        return f"{name} ({sender_jid})"
    return sender_jid


def format_unknown_group(msg: IncomingGroupMessage, trigger: str) -> str:
    sender_str = format_sender_display(msg.sender_jid, getattr(msg, "push_name", ""))
    return (
        "❓ *Unanswered Question*\n"
        "\n"
        f"*Trigger:* {trigger}\n"
        f"*Chat type:* Group\n"
        f"*Group:* {msg.chat_jid}\n"
        f"*Sender:* {sender_str}\n"
        f"*Message ID:* {msg.message_id}\n"
        f"*Time:* {msg.iso_time}\n"
        "\n"
        f"📩 *Message:*\n"
        f"{msg.text}"
    )


def format_unknown_dm(msg: IncomingUserDm) -> str:
    sender_str = format_sender_display(msg.sender_jid, getattr(msg, "push_name", ""))
    return (
        "❓ *Unanswered Question*\n"
        "\n"
        f"*Trigger:* DM\n"
        f"*Chat type:* Direct message\n"
        f"*Sender:* {sender_str}\n"
        f"*Chat:* {msg.chat_jid}\n"
        f"*Message ID:* {msg.message_id}\n"
        f"*Time:* {msg.iso_time}\n"
        "\n"
        f"📩 *Message:*\n"
        f"{msg.text}"
    )


def format_unverified_info(
    msg: IncomingGroupMessage | IncomingUserDm,
    reply: str,
    non_admin_facts: list[str],
) -> str:
    """Format a notification for admin when the bot used unverified facts.

    Sent to admin DMs so they can verify or correct the information.
    """
    facts_block = "\n".join(f"  • {fact}" for fact in non_admin_facts)
    is_group = isinstance(msg, IncomingGroupMessage) or "@g.us" in msg.chat_jid
    chat_label = "Group" if is_group else "DM Chat"
    source_hint = "in the group" if is_group else "or verify directly"
    sender_str = format_sender_display(msg.sender_jid, getattr(msg, "push_name", ""))
    return (
        "⚠️ *Unverified Info Used in Reply*\n"
        "\n"
        f"*{chat_label}:* {msg.chat_jid}\n"
        f"*Sender:* {sender_str}\n"
        f"*Message ID:* {msg.message_id}\n"
        f"*Time:* {msg.iso_time}\n"
        "\n"
        f"📩 *Original question:*\n"
        f"{msg.text}\n"
        "\n"
        f"📋 *Facts from non-admin sources (need verification):*\n"
        f"{facts_block}\n"
        "\n"
        f"🤖 *Bot reply sent:*\n"
        f"{reply}\n"
        "\n"
        f"_Please verify this information. If incorrect, post the correct "
        f"facts {source_hint} so the bot can learn._"
    )
