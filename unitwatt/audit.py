"""Stage 5: a tamper-evident audit trail (P17) and consent records (P19).

Every uploaded document is hashed with SHA-256 and every entry is chained to the previous
one, so editing or deleting any row breaks the chain. The table is append-only: SQLite
triggers refuse UPDATE and DELETE. (A production deployment would use PostgreSQL with the
same append-only rules and row-level security.)
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

GENESIS = "0" * 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    sha256 TEXT,
    payload TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    entry_hash TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS entries_no_update BEFORE UPDATE ON entries
BEGIN SELECT RAISE(ABORT, 'audit trail is append-only'); END;
CREATE TRIGGER IF NOT EXISTS entries_no_delete BEFORE DELETE ON entries
BEGIN SELECT RAISE(ABORT, 'audit trail is append-only'); END;
"""


@dataclass(frozen=True)
class Entry:
    id: int
    kind: str
    name: str
    sha256: str | None
    entry_hash: str


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _entry_hash(prev_hash: str, recorded_at: str, kind: str, name: str, sha: str | None, payload: str) -> str:
    material = json.dumps([prev_hash, recorded_at, kind, name, sha, payload], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(self, path: str | Path = ":memory:"):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.executescript(_SCHEMA)

    def _last_hash(self) -> str:
        row = self.conn.execute("SELECT entry_hash FROM entries ORDER BY id DESC LIMIT 1").fetchone()
        return row[0] if row else GENESIS

    def _append(self, kind: str, name: str, sha: str | None, payload: dict) -> Entry:
        recorded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        prev = self._last_hash()
        h = _entry_hash(prev, recorded_at, kind, name, sha, body)
        cur = self.conn.execute(
            "INSERT INTO entries (recorded_at, kind, name, sha256, payload, prev_hash, entry_hash) VALUES (?,?,?,?,?,?,?)",
            (recorded_at, kind, name, sha, body, prev, h),
        )
        self.conn.commit()
        return Entry(int(cur.lastrowid), kind, name, sha, h)

    def add_document(self, kind: str, name: str, content: bytes, meta: dict | None = None) -> Entry:
        return self._append(f"document:{kind}", name, sha256_bytes(content), {"size_bytes": len(content), **(meta or {})})

    def add_event(self, kind: str, name: str, payload: dict | None = None) -> Entry:
        return self._append(f"event:{kind}", name, None, payload or {})

    def record_consent(self, report: str, recipient: str, granted_by: str, purpose: str) -> Entry:
        return self.add_event(
            "consent", report, {"recipient": recipient, "granted_by": granted_by, "purpose": purpose,
                                "basis": "explicit consent (Digital Personal Data Protection Act, 2023)"}
        )

    def verify(self) -> tuple[bool, int | None]:
        """Recompute the chain. Returns (ok, id of the first broken entry)."""
        prev = GENESIS
        for row in self.conn.execute(
            "SELECT id, recorded_at, kind, name, sha256, payload, prev_hash, entry_hash FROM entries ORDER BY id"
        ):
            entry_id, recorded_at, kind, name, sha, payload, prev_hash, entry_hash = row
            if prev_hash != prev or _entry_hash(prev, recorded_at, kind, name, sha, payload) != entry_hash:
                return False, entry_id
            prev = entry_hash
        return True, None

    def verify_document(self, entry_id: int, content: bytes) -> bool:
        row = self.conn.execute("SELECT sha256 FROM entries WHERE id = ?", (entry_id,)).fetchone()
        return bool(row) and row[0] == sha256_bytes(content)

    def frame(self) -> pd.DataFrame:
        return pd.read_sql_query("SELECT id, recorded_at, kind, name, sha256, payload, entry_hash FROM entries ORDER BY id", self.conn)

    def find(self, name: str) -> Entry | None:
        row = self.conn.execute(
            "SELECT id, kind, name, sha256, entry_hash FROM entries WHERE name = ? ORDER BY id DESC LIMIT 1", (name,)
        ).fetchone()
        return Entry(*row) if row else None
