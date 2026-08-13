"""SQLite-Ablage der Nachrichten.

Eine Nachricht wird von mehreren Repeatern weitergereicht und vom Observer
mehrfach gehoert. Der Observer liefert dafuer einen ``hash``, der den Pfad
ausklammert — dieselbe Nachricht hat ueber alle Empfaenge denselben Wert. Genau
darauf liegt der eindeutige Index: der erste Empfang legt die Zeile an, jeder
weitere zaehlt nur noch den Zaehler hoch und verbessert ggf. SNR/RSSI.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    channel      TEXT    NOT NULL,
    packet_hash  TEXT    NOT NULL,
    sent_at      INTEGER NOT NULL,
    received_at  INTEGER NOT NULL,
    sender       TEXT,
    text         TEXT    NOT NULL,
    hops         INTEGER,
    snr          REAL,
    rssi         REAL,
    receptions   INTEGER NOT NULL DEFAULT 1,
    observers    TEXT    NOT NULL DEFAULT '[]'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_dedup ON messages (channel, packet_hash);
CREATE INDEX IF NOT EXISTS idx_messages_channel_time ON messages (channel, received_at DESC);
"""


@dataclass
class StoredMessage:
    id: int
    channel: str
    sent_at: int
    received_at: int
    sender: Optional[str]
    text: str
    hops: Optional[int]
    snr: Optional[float]
    rssi: Optional[float]
    receptions: int
    observers: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "channel": self.channel,
            "sent_at": self.sent_at,
            "received_at": self.received_at,
            "sender": self.sender,
            "text": self.text,
            "hops": self.hops,
            "snr": self.snr,
            "rssi": self.rssi,
            "receptions": self.receptions,
            "observers": self.observers,
        }


def _row_to_message(row: sqlite3.Row) -> StoredMessage:
    return StoredMessage(
        id=row["id"],
        channel=row["channel"],
        sent_at=row["sent_at"],
        received_at=row["received_at"],
        sender=row["sender"],
        text=row["text"],
        hops=row["hops"],
        snr=row["snr"],
        rssi=row["rssi"],
        receptions=row["receptions"],
        observers=json.loads(row["observers"]),
    )


class Store:
    """Duenne Schicht ueber SQLite. Ein Lock, weil der MQTT-Thread schreibt,
    waehrend der Webserver liest."""

    def __init__(self, path: str) -> None:
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def upsert(self, *, channel: str, packet_hash: str, sent_at: int, sender: Optional[str],
               text: str, hops: Optional[int], snr: Optional[float], rssi: Optional[float],
               observer: Optional[str], received_at: Optional[int] = None
               ) -> tuple[StoredMessage, bool]:
        """Nachricht anlegen oder einen weiteren Empfang einrechnen.

        Rueckgabe: (Zeile, is_new). Bei einem Wiederholempfang wird der beste
        SNR und der kuerzeste Weg behalten — das beschreibt die Nachricht besser
        als der zufaellig erste Empfang.
        """
        now = int(time.time()) if received_at is None else received_at
        with self._lock:
            cur = self._db.execute(
                "SELECT * FROM messages WHERE channel = ? AND packet_hash = ?",
                (channel, packet_hash),
            )
            row = cur.fetchone()

            if row is None:
                observers = [observer] if observer else []
                cur = self._db.execute(
                    """INSERT INTO messages
                       (channel, packet_hash, sent_at, received_at, sender, text,
                        hops, snr, rssi, receptions, observers)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                    (channel, packet_hash, sent_at, now, sender, text, hops, snr, rssi,
                     json.dumps(observers)),
                )
                self._db.commit()
                message = StoredMessage(
                    id=cur.lastrowid, channel=channel, sent_at=sent_at, received_at=now,
                    sender=sender, text=text, hops=hops, snr=snr, rssi=rssi,
                    receptions=1, observers=observers,
                )
                return message, True

            observers = json.loads(row["observers"])
            if observer and observer not in observers:
                observers.append(observer)
            best_snr = row["snr"] if snr is None else max(snr, row["snr"] if row["snr"] is not None else snr)
            best_rssi = row["rssi"] if rssi is None else max(rssi, row["rssi"] if row["rssi"] is not None else rssi)
            fewest_hops = row["hops"] if hops is None else min(hops, row["hops"] if row["hops"] is not None else hops)
            self._db.execute(
                """UPDATE messages
                   SET receptions = receptions + 1, observers = ?, snr = ?, rssi = ?, hops = ?
                   WHERE id = ?""",
                (json.dumps(observers), best_snr, best_rssi, fewest_hops, row["id"]),
            )
            self._db.commit()
            cur = self._db.execute("SELECT * FROM messages WHERE id = ?", (row["id"],))
            return _row_to_message(cur.fetchone()), False

    def recent(self, channel: Optional[str] = None, limit: int = 100,
               before_id: Optional[int] = None) -> list[StoredMessage]:
        sql = "SELECT * FROM messages"
        clauses: list[str] = []
        params: list[Any] = []
        if channel:
            clauses.append("channel = ?")
            params.append(channel)
        if before_id:
            clauses.append("id < ?")
            params.append(before_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        with self._lock:
            rows = self._db.execute(sql, params).fetchall()
        return [_row_to_message(row) for row in rows]

    def stats(self) -> dict[str, dict[str, Any]]:
        """Pro Kanal: Anzahl und juengster Empfang."""
        with self._lock:
            rows = self._db.execute(
                """SELECT channel, COUNT(*) AS count, MAX(received_at) AS last_seen
                   FROM messages GROUP BY channel"""
            ).fetchall()
        return {r["channel"]: {"count": r["count"], "last_seen": r["last_seen"]} for r in rows}

    def observers(self) -> list[str]:
        """Namen der Observer, ueber die Nachrichten hereingekommen sind.

        Kommt aus den Daten, nicht aus der Konfiguration: Wer mithoert, steht in
        jeder Zeile. So bleibt die Angabe richtig, auch wenn spaeter ein zweiter
        Observer dazukommt oder einer wegfaellt.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT DISTINCT observers FROM messages WHERE observers != '[]'"
            ).fetchall()
        names: set[str] = set()
        for row in rows:
            names.update(json.loads(row["observers"]))
        return sorted(names)

    def purge(self, retention_days: int) -> int:
        """Alles aelter als die Aufbewahrungsfrist loeschen. 0 = nie loeschen."""
        if retention_days <= 0:
            return 0
        cutoff = int(time.time()) - retention_days * 86400
        with self._lock:
            cur = self._db.execute("DELETE FROM messages WHERE received_at < ?", (cutoff,))
            self._db.commit()
            return cur.rowcount
