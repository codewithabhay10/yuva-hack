"""What the bot remembers (P18): conversation state, a message log, and the data people send.

SQLite, so a restart of the server keeps pending confirmations and everything already saved.
The pilot moves these tables to PostgreSQL with row-level security per factory (P19).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from unitwatt.schemas import BillDocument

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (sender TEXT PRIMARY KEY, state TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY, at TEXT NOT NULL, sender TEXT NOT NULL, direction TEXT NOT NULL, kind TEXT NOT NULL, text TEXT
);
CREATE TABLE IF NOT EXISTS production (
    id INTEGER PRIMARY KEY, day TEXT NOT NULL, product TEXT NOT NULL, tonnes REAL NOT NULL, shifts INTEGER,
    sender TEXT NOT NULL, source TEXT NOT NULL, raw TEXT, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bills (
    id INTEGER PRIMARY KEY, period_start TEXT NOT NULL, period_end TEXT NOT NULL, total REAL NOT NULL, bill TEXT NOT NULL,
    sender TEXT NOT NULL, audit_entry INTEGER, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seen (message_id TEXT PRIMARY KEY, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS links (channel_id TEXT PRIMARY KEY, phone TEXT NOT NULL, at TEXT NOT NULL);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class BotStore:
    def __init__(self, path: str | Path = ":memory:"):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(_SCHEMA)

    def _write(self, sql: str, params: tuple = ()) -> int:
        with self.lock, self.conn:
            return self.conn.execute(sql, params).lastrowid

    # Conversation state ------------------------------------------------------------------

    def session(self, sender: str) -> dict:
        row = self.conn.execute("SELECT state FROM sessions WHERE sender = ?", (sender,)).fetchone()
        return json.loads(row[0]) if row else {}

    def save_session(self, sender: str, state: dict) -> None:
        self._write(
            "INSERT INTO sessions (sender, state, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(sender) DO UPDATE SET state = excluded.state, updated_at = excluded.updated_at",
            (sender, json.dumps(state, ensure_ascii=False, default=str), _now()),
        )

    def log(self, sender: str, direction: str, kind: str, text: str) -> None:
        self._write("INSERT INTO messages (at, sender, direction, kind, text) VALUES (?, ?, ?, ?, ?)",
                    (_now(), sender, direction, kind, text))

    def messages(self, sender: str | None = None, limit: int = 200) -> pd.DataFrame:
        sql = "SELECT id, at, sender, direction, kind, text FROM messages"
        params: tuple = ()
        if sender:
            sql, params = sql + " WHERE sender = ?", (sender,)
        return pd.read_sql_query(sql + " ORDER BY id DESC LIMIT ?", self.conn, params=params + (limit,)).iloc[::-1]

    def first_time(self, message_id: str) -> bool:
        """False if this delivery was already handled (WhatsApp retries webhooks)."""
        if not message_id:
            return True
        with self.lock, self.conn:
            cur = self.conn.execute("INSERT OR IGNORE INTO seen (message_id, at) VALUES (?, ?)", (message_id, _now()))
            return cur.rowcount == 1

    # Channel ids that are not phone numbers (Telegram chats) ------------------------------

    def link(self, channel_id: str, phone: str) -> None:
        self._write("INSERT OR REPLACE INTO links (channel_id, phone, at) VALUES (?, ?, ?)", (channel_id, phone, _now()))

    def linked_phone(self, channel_id: str) -> str | None:
        row = self.conn.execute("SELECT phone FROM links WHERE channel_id = ?", (channel_id,)).fetchone()
        return row[0] if row else None

    # Data people send ----------------------------------------------------------------------

    def add_production(self, day: date, tonnes: dict[str, float], shifts: int | None, sender: str, source: str,
                       raw: str = "") -> list[int]:
        return [
            self._write(
                "INSERT INTO production (day, product, tonnes, shifts, sender, source, raw, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (day.isoformat(), product, float(t), shifts, sender, source, raw, _now()),
            )
            for product, t in tonnes.items()
        ]

    def add_bill(self, bill: BillDocument, sender: str, audit_entry: int | None = None) -> int:
        return self._write(
            "INSERT INTO bills (period_start, period_end, total, bill, sender, audit_entry, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (bill.period_start.isoformat(), bill.period_end.isoformat(), bill.total_amount, bill.model_dump_json(),
             sender, audit_entry, _now()),
        )

    def production_frame(self) -> pd.DataFrame:
        return pd.read_sql_query("SELECT id, day, product, tonnes, shifts, sender, source, raw, at FROM production ORDER BY id",
                                 self.conn)

    def bills_frame(self) -> pd.DataFrame:
        return pd.read_sql_query("SELECT id, period_start, period_end, total, sender, audit_entry, at FROM bills ORDER BY id",
                                 self.conn)
