"""Authoritative content catalog. Conversation memory never grants publication rights."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlparse
import uuid
from zoneinfo import ZoneInfo

MAX_CONTENT = 80_000
MAX_PDF_BYTES = 10 * 1024 * 1024
_STOP = set("a an the is are was were do does did i you we our it this that what when where how who can please me of to for in on and or with about tell".split())


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value: str | None, *, default: datetime | None = None) -> str | None:
    if not value:
        return default.astimezone(timezone.utc).isoformat() if default else None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            dt = datetime.fromisoformat(value).replace(tzinfo=ZoneInfo("Africa/Lagos"))
        else:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                raise ValueError("timestamps need a timezone")
        return dt.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError) as exc:
        raise ValueError("Use YYYY-MM-DD (Lagos) or an ISO timestamp with timezone.") from exc


def tokens(text: str) -> set[str]:
    return {s for s in re.findall(r"\w+", text.casefold()) if len(s) > 1 and s not in _STOP}


@dataclass(frozen=True)
class KnowledgeEntry:
    id: str
    title: str
    content: str
    audience: str
    status: str
    effective_at: str
    expires_at: str | None
    superseded_at: str | None
    replaces: str | None
    source_chat: str
    source_message: str
    source_url: str | None
    filename: str | None
    created_by: str
    created_at: str
    published_by: str | None
    published_at: str | None


@dataclass(frozen=True)
class KnowledgeExcerpt:
    entry: KnowledgeEntry
    text: str
    index: int

    @property
    def source_id(self) -> str:
        return f"KB-{self.entry.id}-{self.index}"


_ENTRY_COLUMNS = ",".join(KnowledgeEntry.__dataclass_fields__)


class KnowledgeLibrary:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS knowledge (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL,
                    audience TEXT NOT NULL, status TEXT NOT NULL,
                    effective_at TEXT NOT NULL, expires_at TEXT, superseded_at TEXT,
                    replaces TEXT, source_chat TEXT NOT NULL, source_message TEXT NOT NULL,
                    source_url TEXT, filename TEXT, attachment BLOB,
                    created_by TEXT NOT NULL, created_at TEXT NOT NULL,
                    published_by TEXT, published_at TEXT,
                    UNIQUE(source_chat, source_message)
                );
                CREATE TABLE IF NOT EXISTS knowledge_audit (
                    sequence INTEGER PRIMARY KEY, entry_id TEXT NOT NULL,
                    action TEXT NOT NULL, actor TEXT NOT NULL, at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS knowledge_status ON knowledge(status, audience);
            """)
        self.path.chmod(0o600)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _entry(row) -> KnowledgeEntry:
        return KnowledgeEntry(**{k: row[k] for k in KnowledgeEntry.__dataclass_fields__})

    def get(self, entry_id: str) -> KnowledgeEntry:
        with self._db() as db:
            row = db.execute(f"SELECT {_ENTRY_COLUMNS} FROM knowledge WHERE id=?", (entry_id,)).fetchone()
        if row is None:
            raise ValueError("Knowledge entry not found.")
        return self._entry(row)

    def create_draft(self, *, title: str, content: str, actor: str, source_chat: str,
                     source_message: str, audience: str = "admin_only",
                     effective_at: str | None = None, expires_at: str | None = None,
                     replaces: str | None = None, source_url: str | None = None,
                     attachment: bytes | None = None, filename: str | None = None) -> KnowledgeEntry:
        title, content = title.strip(), content.strip()
        if not title or len(title) > 200 or not content or len(content) > MAX_CONTENT:
            raise ValueError("Provide a title (1–200 characters) and content (1–80,000 characters).")
        if audience not in {"members", "admin_only"}:
            raise ValueError("audience must be members or admin_only.")
        if attachment is not None and len(attachment) > MAX_PDF_BYTES:
            raise ValueError("PDF exceeds the 10 MB limit.")
        if source_url:
            parsed = urlparse(source_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
                raise ValueError("source must be a public HTTP(S) URL without credentials.")
        now = utc_now().isoformat()
        effective = timestamp(effective_at) or now
        expires = timestamp(expires_at)
        if expires and expires <= effective:
            raise ValueError("Expiry must be after the effective date.")
        if not source_chat or not source_message or not actor:
            raise ValueError("Source chat, message ID and submitting admin are required.")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(f"SELECT {_ENTRY_COLUMNS} FROM knowledge WHERE source_chat=? AND source_message=?",
                               (source_chat, source_message)).fetchone()
            if prior:
                return self._entry(prior)
            entry_id = uuid.uuid4().hex[:12]
            db.execute("""INSERT INTO knowledge
                (id,title,content,audience,status,effective_at,expires_at,replaces,
                 source_chat,source_message,source_url,filename,attachment,created_by,created_at)
                VALUES (?,?,?,?,'draft',?,?,?,?,?,?,?,?,?,?)""",
                (entry_id, title, content, audience, effective, expires, replaces, source_chat,
                 source_message, source_url, filename, attachment, actor, now))
            db.execute("INSERT INTO knowledge_audit(entry_id,action,actor,at) VALUES (?,'draft',?,?)",
                       (entry_id, actor, now))
        return self.get(entry_id)

    def publish(self, entry_id: str, *, actor: str, now: datetime | None = None) -> KnowledgeEntry:
        at = (now or utc_now()).astimezone(timezone.utc).isoformat()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(f"SELECT {_ENTRY_COLUMNS} FROM knowledge WHERE id=?", (entry_id,)).fetchone()
            if row is None or row["status"] != "draft":
                raise ValueError("Only an existing draft can be published.")
            if row["expires_at"] and row["expires_at"] <= at:
                raise ValueError("This draft has already expired; create a fresh version.")
            if row["replaces"]:
                old = db.execute(f"SELECT {_ENTRY_COLUMNS} FROM knowledge WHERE id=?", (row["replaces"],)).fetchone()
                if old is None or old["status"] != "published" or old["superseded_at"]:
                    raise ValueError("Predecessor is unavailable or already has a replacement.")
                if old["audience"] != row["audience"]:
                    raise ValueError("A replacement must have the same audience as its predecessor.")
                switch = max(at, row["effective_at"])
                if switch < old["effective_at"]:
                    raise ValueError("A replacement cannot precede the original effective date.")
                db.execute("UPDATE knowledge SET superseded_at=? WHERE id=?", (switch, old["id"]))
            db.execute("UPDATE knowledge SET status='published',published_by=?,published_at=? WHERE id=?",
                       (actor, at, entry_id))
            db.execute("INSERT INTO knowledge_audit(entry_id,action,actor,at) VALUES (?,'publish',?,?)",
                       (entry_id, actor, at))
        return self.get(entry_id)

    def retire(self, entry_id: str, *, actor: str, now: datetime | None = None) -> None:
        at = (now or utc_now()).astimezone(timezone.utc).isoformat()
        with self._db() as db:
            if db.execute("UPDATE knowledge SET status='retired' WHERE id=?", (entry_id,)).rowcount != 1:
                raise ValueError("Knowledge entry not found.")
            db.execute("INSERT INTO knowledge_audit(entry_id,action,actor,at) VALUES (?,'retire',?,?)",
                       (entry_id, actor, at))

    def set_audience(self, entry_id: str, audience: str, *, actor: str) -> KnowledgeEntry:
        if audience not in {"members", "admin_only"}:
            raise ValueError("audience must be members or admin_only.")
        with self._db() as db:
            updated = db.execute("UPDATE knowledge SET audience=? WHERE id=? AND status='draft'",
                                 (audience, entry_id)).rowcount
            if updated != 1:
                raise ValueError("Only a draft's audience can be changed. Retire or replace published content.")
            db.execute("INSERT INTO knowledge_audit(entry_id,action,actor,at) VALUES (?,?,?,?)",
                       (entry_id, f"audience:{audience}", actor, utc_now().isoformat()))
        return self.get(entry_id)

    @staticmethod
    def _accessible(entry: KnowledgeEntry, *, allow_admin: bool, now: datetime) -> bool:
        at = now.astimezone(timezone.utc).isoformat()
        return (entry.status == "published" and (allow_admin or entry.audience == "members")
                and entry.effective_at <= at and (not entry.expires_at or at < entry.expires_at)
                and (not entry.superseded_at or at < entry.superseded_at))

    def active(self, entry_id: str, *, allow_admin: bool = False, now: datetime | None = None) -> bool:
        try:
            return self._accessible(self.get(entry_id), allow_admin=allow_admin, now=now or utc_now())
        except ValueError:
            return False

    def list_entries(self, *, limit: int = 50, offset: int = 0) -> list[KnowledgeEntry]:
        with self._db() as db:
            rows = db.execute(f"SELECT {_ENTRY_COLUMNS} FROM knowledge ORDER BY created_at DESC,id LIMIT ? OFFSET ?",
                              (min(max(limit, 1), 5000), max(offset, 0))).fetchall()
        return [self._entry(row) for row in rows]

    def search(self, query: str, *, context: str = "", allow_admin: bool = False,
               now: datetime | None = None, limit: int = 6) -> list[KnowledgeExcerpt]:
        terms = tokens(query)
        # Short follow-ups can use conversational topic words; ordinary questions don't
        # drag unrelated historical announcements into retrieval.
        if not terms or re.search(r"\b(it|that|this|they|them|there)\b", query, re.I):
            terms |= tokens(context[-2500:])
        if not terms:
            return []
        at = now or utc_now()
        matches: list[tuple[int, KnowledgeExcerpt]] = []
        with self._db() as db:
            rows = db.execute(f"SELECT {_ENTRY_COLUMNS} FROM knowledge WHERE status='published'").fetchall()
        for row in rows:
            entry = self._entry(row)
            if not self._accessible(entry, allow_admin=allow_admin, now=at):
                continue
            title_score = len(terms & tokens(entry.title)) * 3
            # Overlap prevents text spanning a boundary from disappearing. Excerpts
            # remain literal source substrings for the renderer to validate.
            for index, start in enumerate(range(0, len(entry.content), 1500)):
                chunk = entry.content[start:start + 1800]
                score = title_score + len(terms & tokens(chunk))
                if score:
                    matches.append((score, KnowledgeExcerpt(entry, chunk, index)))
        matches.sort(key=lambda item: (-item[0], item[1].entry.id, item[1].index))
        return [item[1] for item in matches[:limit]]

    def document(self, entry_id: str, *, allow_admin: bool = False,
                 now: datetime | None = None) -> tuple[bytes, str] | None:
        with self._db() as db:
            row = db.execute(f"SELECT {_ENTRY_COLUMNS} FROM knowledge WHERE id=?", (entry_id,)).fetchone()
        if not row or not self._accessible(self._entry(row), allow_admin=allow_admin, now=now or utc_now()):
            return None
        with self._db() as db:
            blob = db.execute("SELECT attachment,filename FROM knowledge WHERE id=?", (entry_id,)).fetchone()
        return (bytes(blob["attachment"]), blob["filename"]) if blob and blob["attachment"] else None

    def audit(self, entry_id: str) -> list[dict]:
        with self._db() as db:
            return [dict(row) for row in db.execute(
                "SELECT action,actor,at FROM knowledge_audit WHERE entry_id=? ORDER BY sequence", (entry_id,))]
