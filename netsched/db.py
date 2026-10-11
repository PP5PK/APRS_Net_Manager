"""SQLite persistence: sent messages, replies and downloaded certificates."""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

from .util import base_call, iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS sends (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    callsign        TEXT NOT NULL,
    local_date      TEXT NOT NULL,
    text            TEXT NOT NULL,
    msgid           TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    last_attempt_at TEXT,
    acked_at        TEXT,
    UNIQUE (callsign, local_date, text)
);

CREATE TABLE IF NOT EXISTS replies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    send_id     INTEGER REFERENCES sends(id),
    src         TEXT NOT NULL,
    text        TEXT NOT NULL,
    msgno       TEXT,
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS certificates (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    reply_id   INTEGER NOT NULL REFERENCES replies(id),
    url        TEXT NOT NULL,
    status     TEXT NOT NULL,
    path       TEXT,
    error      TEXT,
    created_at TEXT NOT NULL
);
"""
# sends.status: pending | sent | acked | rejected | no_ack | unconfirmed
# certificates.status: downloaded | manual | failed


class Database:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ sends

    def is_done(self, callsign: str, day: dt.date, text: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sends WHERE callsign = ? AND local_date = ? AND text = ?",
            (callsign, day.isoformat(), text),
        ).fetchone()
        return row is not None

    def create_send(self, callsign: str, day: dt.date, text: str, now: dt.datetime) -> int | None:
        """Register a send; returns None if it was already registered."""
        try:
            with self.conn:
                cur = self.conn.execute(
                    "INSERT INTO sends (callsign, local_date, text, created_at) VALUES (?, ?, ?, ?)",
                    (callsign, day.isoformat(), text, iso(now)),
                )
        except sqlite3.IntegrityError:
            return None
        return cur.lastrowid

    def assign_msgid(self, send_id: int) -> str:
        msgid = str(send_id % 99999 + 1)  # APRS message numbers are at most 5 characters
        with self.conn:
            self.conn.execute("UPDATE sends SET msgid = ? WHERE id = ?", (msgid, send_id))
        return msgid

    def record_attempt(self, send_id: int, now: dt.datetime, status: str = "sent") -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE sends SET attempts = attempts + 1, status = ?, last_attempt_at = ? WHERE id = ?",
                (status, iso(now), send_id),
            )

    def set_status(self, send_id: int, status: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE sends SET status = ? WHERE id = ?", (status, send_id))

    def due_retries(self, max_attempts: int, interval: int, now: dt.datetime) -> list:
        """Sends still waiting for an ACK whose next attempt is due."""
        cutoff = iso(now - dt.timedelta(seconds=interval))
        oldest = iso(now - dt.timedelta(hours=6))
        return self.conn.execute(
            "SELECT * FROM sends WHERE status IN ('pending', 'sent') AND attempts < ? "
            "AND created_at >= ? AND (last_attempt_at IS NULL OR last_attempt_at <= ?)",
            (max_attempts, oldest, cutoff),
        ).fetchall()

    def expire_unacked(self, max_attempts: int, interval: int, now: dt.datetime) -> int:
        cutoff = iso(now - dt.timedelta(seconds=interval))
        with self.conn:
            cur = self.conn.execute(
                "UPDATE sends SET status = 'no_ack' WHERE status = 'sent' "
                "AND attempts >= ? AND last_attempt_at <= ?",
                (max_attempts, cutoff),
            )
        return cur.rowcount

    def mark_ack(self, msgid: str, src: str, now: dt.datetime, rejected: bool = False) -> bool:
        since = iso(now - dt.timedelta(days=3))
        rows = self.conn.execute(
            "SELECT id, callsign FROM sends WHERE msgid = ? "
            "AND status IN ('sent', 'no_ack', 'unconfirmed') AND created_at >= ? ORDER BY id DESC",
            (msgid, since),
        ).fetchall()
        for row in rows:
            if base_call(row["callsign"]) == base_call(src):
                with self.conn:
                    self.conn.execute(
                        "UPDATE sends SET status = ?, acked_at = ? WHERE id = ?",
                        ("rejected" if rejected else "acked", iso(now), row["id"]),
                    )
                return True
        return False

    def latest_send_for(self, src: str, now: dt.datetime, hours: int = 72):
        since = iso(now - dt.timedelta(hours=hours))
        rows = self.conn.execute(
            "SELECT * FROM sends WHERE created_at >= ? ORDER BY id DESC", (since,)
        ).fetchall()
        for row in rows:
            if base_call(row["callsign"]) == base_call(src):
                return row
        return None

    # ---------------------------------------------------------------- replies

    def reply_exists(self, src: str, text: str, now: dt.datetime, minutes: int = 30) -> bool:
        since = iso(now - dt.timedelta(minutes=minutes))
        row = self.conn.execute(
            "SELECT 1 FROM replies WHERE src = ? AND text = ? AND received_at >= ?",
            (src.upper(), text, since),
        ).fetchone()
        return row is not None

    def add_reply(self, send_id: int | None, src: str, text: str, msgno: str | None, now: dt.datetime) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO replies (send_id, src, text, msgno, received_at) VALUES (?, ?, ?, ?, ?)",
                (send_id, src.upper(), text, msgno, iso(now)),
            )
        return cur.lastrowid

    # ----------------------------------------------------------- certificates

    def add_certificate(
        self, reply_id: int, url: str, status: str, path: str | None, error: str | None, now: dt.datetime
    ) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO certificates (reply_id, url, status, path, error, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (reply_id, url, status, path, error, iso(now)),
            )
        return cur.lastrowid

    def open_certificates(self) -> list:
        """Certificates that need manual attention."""
        return self.conn.execute(
            "SELECT c.*, r.src, r.received_at AS reply_at FROM certificates c "
            "JOIN replies r ON r.id = c.reply_id "
            "WHERE c.status IN ('manual', 'failed') ORDER BY c.id DESC"
        ).fetchall()
