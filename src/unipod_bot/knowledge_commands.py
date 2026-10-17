"""Human-facing publishing commands. Call only after authenticating the sender."""
from __future__ import annotations

import math
import re

from unipod_bot.knowledge import KnowledgeEntry, KnowledgeLibrary

PAGE_SIZE = 2400
HELP = """Knowledge library commands (admin only):
knowledge draft Title | audience=members | effective=2026-11-01 | expires=2026-12-01 | replaces=ENTRY_ID | source=https://example.com/guide
Put the approved facts on the next line. Every option is optional; audience defaults to admin_only.

knowledge show ENTRY_ID [page] — review content and metadata
knowledge list [page] — list drafts and versions
knowledge publish ENTRY_ID — approve this exact draft
knowledge audience ENTRY_ID members|admin_only — classify a draft before publishing
knowledge retire ENTRY_ID — withdraw an entry

Send PDFs here or in the configured knowledge group to create drafts. PDFs require readable text and must be 10 MB or less. Dates without times use midnight Lagos. Publishing is explicit; ingestion acknowledgements do not mean publication."""


def preview(entry: KnowledgeEntry, page: int = 1) -> str:
    pages = max(1, math.ceil(len(entry.content) / PAGE_SIZE))
    if not 1 <= page <= pages:
        raise ValueError(f"Choose a content page from 1 to {pages}.")
    body = entry.content[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]
    return (f"Knowledge {entry.id}: {entry.title}\nStatus: {entry.status}\n"
            f"Audience: {entry.audience}\nEffective: {entry.effective_at}\n"
            f"Expires: {entry.expires_at or 'none'}\nReplaces: {entry.replaces or 'none'}\n"
            f"Superseded at: {entry.superseded_at or 'none'}\n"
            f"Source: {entry.filename or entry.source_url or 'admin text'}\n"
            f"Source message: {entry.source_message}\n"
            f"Submitted by: {entry.created_by} at {entry.created_at}\n"
            f"Published by: {entry.published_by or 'not approved'}\nContent ({page}/{pages}):\n{body}\n\n"
            f"Review all pages with: knowledge show {entry.id} PAGE\n"
            + (f"To approve: knowledge publish {entry.id}" if entry.status == "draft" else ""))


def handle_knowledge_command(library: KnowledgeLibrary, text: str, *, actor: str,
                             source_chat: str, source_message: str,
                             attachment: bytes | None = None, filename: str | None = None) -> str:
    text = text.strip()
    if text.lower() in {"knowledge", "knowledge help"}:
        return HELP
    if re.match(r"^knowledge draft(?:\s|$)", text, re.I):
        header, _, content = text.partition("\n")
        parts = re.sub(r"^knowledge draft\s*", "", header, flags=re.I).split("|")
        title = parts.pop(0).strip()
        options = {}
        names = {"audience": "audience", "effective": "effective_at", "expires": "expires_at",
                 "replaces": "replaces", "source": "source_url"}
        for part in parts:
            key, sep, value = part.strip().partition("=")
            if not sep or key not in names or names[key] in options or not value.strip():
                raise ValueError("Unknown option, duplicate option, or empty option. Send knowledge help.")
            options[names[key]] = value.strip()
        entry = library.create_draft(title=title, content=content, actor=actor,
            source_chat=source_chat, source_message=source_message,
            attachment=attachment, filename=filename, **options)
        return preview(entry)
    parts = text.split()
    if len(parts) >= 2 and parts[0].lower() == "knowledge":
        command = parts[1].lower()
        if command == "list" and len(parts) in {2, 3}:
            page = int(parts[2]) if len(parts) == 3 else 1
            if page < 1:
                raise ValueError("Choose a positive page number.")
            entries = library.list_entries(limit=15, offset=(page - 1) * 15)
            if not entries:
                return "No entries on this page. Use knowledge draft to submit knowledge."
            return "Knowledge library (page " + str(page) + "):\n" + "\n".join(
                f"{e.id} • {e.status}" +
                ("/inactive" if e.status == "published" and not library.active(e.id, allow_admin=True) else "") +
                f" • {e.audience} • {e.title}" for e in entries)
        if command == "show" and len(parts) in {3, 4}:
            return preview(library.get(parts[2]), int(parts[3]) if len(parts) == 4 else 1)
        if command == "publish" and len(parts) == 3:
            entry = library.publish(parts[2], actor=actor)
            return (f"Published {entry.id}: {entry.title}\nAudience: {entry.audience}\n"
                    f"Effective: {entry.effective_at}\nExpires: {entry.expires_at or 'none'}")
        if command == "audience" and len(parts) == 4:
            return preview(library.set_audience(parts[2], parts[3], actor=actor))
        if command == "retire" and len(parts) == 3:
            library.retire(parts[2], actor=actor)
            return f"Retired {parts[2]}. This entry will no longer support answers or document downloads."
    raise ValueError("Unknown knowledge command. Send knowledge help.")
