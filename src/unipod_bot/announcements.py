"""Persistent, preview-confirmed test-group announcements. Never retry ambiguous sends."""
from contextlib import contextmanager
import hashlib
import os
import secrets
import sqlite3
import time
import uuid
from pathlib import Path


class AnnouncementConflict(ValueError):
    pass


class AnnouncementStore:
    def __init__(self, path: Path):
        self.path = path
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        path.chmod(0o600)
        with self._db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS announcements (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL,
                    target TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
                    created_by TEXT NOT NULL, created_at REAL NOT NULL, request_id TEXT NOT NULL,
                    sent_by TEXT, sent_at REAL, preview_hash TEXT, preview_actor TEXT, preview_until REAL,
                    UNIQUE(created_by, request_id)
                );
                CREATE TABLE IF NOT EXISTS announcement_audit (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, announcement_id TEXT NOT NULL,
                    action TEXT NOT NULL, actor TEXT NOT NULL, at REAL NOT NULL
                );
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        for suffix in ('', '-wal', '-shm'):
            candidate = Path(str(self.path) + suffix)
            if candidate.exists():
                candidate.chmod(0o600)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _public(row):
        return {k: row[k] for k in row.keys() if not k.startswith('preview_') and k != 'request_id'}

    @staticmethod
    def _audit(db, entry_id, action, actor):
        db.execute("INSERT INTO announcement_audit(announcement_id,action,actor,at) VALUES (?,?,?,?)",
                   (entry_id, action, actor, time.time()))

    def create(self, *, title, content, target, actor, request_id):
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 200:
            raise ValueError('Provide a title of 1–200 characters.')
        if not isinstance(content, str) or not 1 <= len(content.strip()) <= 3500:
            raise ValueError('Provide announcement text of 1–3,500 characters.')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM announcements WHERE created_by=? AND request_id=?', (actor, request_id)).fetchone()
            if old:
                return self._public(old)
            entry_id = uuid.uuid4().hex[:12]
            db.execute('INSERT INTO announcements(id,title,content,target,created_by,created_at,request_id) VALUES (?,?,?,?,?,?,?)',
                       (entry_id, title.strip(), content, target, actor, time.time(), request_id))
            self._audit(db, entry_id, 'draft', actor)
        return self.get(entry_id)

    def get(self, entry_id):
        with self._db() as db:
            row = db.execute('SELECT * FROM announcements WHERE id=?', (entry_id,)).fetchone()
            if row is None:
                raise KeyError(entry_id)
            result = self._public(row)
            result['audit'] = [dict(x) for x in db.execute(
                'SELECT action,actor,at FROM announcement_audit WHERE announcement_id=? ORDER BY sequence', (entry_id,))]
            return result

    def list_entries(self, page=1):
        with self._db() as db:
            return [self._public(row) for row in db.execute(
                'SELECT * FROM announcements ORDER BY created_at DESC,id LIMIT 25 OFFSET ?', ((page - 1) * 25,))]

    def preview(self, entry_id, *, actor, target, now=None):
        now = time.time() if now is None else now
        token = secrets.token_urlsafe(32)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM announcements WHERE id=?', (entry_id,)).fetchone()
            if row is None:
                raise KeyError(entry_id)
            if row['status'] != 'draft' or row['target'] != target:
                raise AnnouncementConflict('Only an unsent draft for the current test group can be previewed.')
            db.execute('UPDATE announcements SET preview_hash=?,preview_actor=?,preview_until=? WHERE id=?',
                       (hashlib.sha256(token.encode()).hexdigest(), actor, now + 600, entry_id))
            self._audit(db, entry_id, 'preview', actor)
            return {**self._public(row), 'preview_token': token, 'preview_expires_at': now + 600}

    def claim(self, entry_id, *, actor, target, token, now=None):
        now = time.time() if now is None else now
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM announcements WHERE id=?', (entry_id,)).fetchone()
            if row is None:
                raise KeyError(entry_id)
            digest = hashlib.sha256(token.encode()).hexdigest() if isinstance(token, str) else ''
            if (row['status'] != 'draft' or row['target'] != target or row['preview_actor'] != actor
                    or not row['preview_until'] or row['preview_until'] <= now
                    or not secrets.compare_digest(row['preview_hash'] or '', digest)):
                raise AnnouncementConflict('Preview again before sending. This draft may already have been sent or the target changed.')
            db.execute("UPDATE announcements SET status='sending',sent_by=?,preview_hash=NULL WHERE id=?", (actor, entry_id))
            self._audit(db, entry_id, 'sending', actor)
            return self._public(row)

    def finish(self, entry_id, *, actor, sent):
        status = 'sent' if sent else 'unconfirmed'
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE announcements SET status=?,sent_at=? WHERE id=? AND status='sending'",
                       (status, time.time() if sent else None, entry_id))
            self._audit(db, entry_id, status, actor)
        return self.get(entry_id)
