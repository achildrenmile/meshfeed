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

-- Ein Empfang derselben Nachricht. Mehrfachempfaenge unterscheiden sich im Weg,
-- den das Paket genommen hat — genau das ist der Grund, sie einzeln zu halten
-- statt sie wie die Zaehlspalte oben zu einer Zeile zu verdichten.
CREATE TABLE IF NOT EXISTS receptions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id   INTEGER NOT NULL,
    observer     TEXT,
    path         TEXT,
    hops         INTEGER,
    snr          REAL,
    rssi         REAL,
    received_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_receptions_message ON receptions (message_id, id);

-- Was wir ueber die Knoten im Netz wissen. Gefuellt aus Adverts, die ohnehin
-- ueber dieselbe MQTT-Verbindung hereinkommen. Der Pfad eines Pakets nennt nur
-- ein Praefix des Public Key, hier steht der ganze — aufgeloest wird beim
-- Ausliefern, damit ein spaeter eintreffendes Advert auch alte Wege benennt.
CREATE TABLE IF NOT EXISTS nodes (
    pubkey     TEXT PRIMARY KEY,
    name       TEXT,
    mode       TEXT,
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL
);
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
               observer: Optional[str], received_at: Optional[int] = None,
               path: Optional[list[str]] = None
               ) -> tuple[StoredMessage, bool]:
        """Nachricht anlegen oder einen weiteren Empfang einrechnen.

        Rueckgabe: (Zeile, is_new). Die Zeile in ``messages`` bleibt die
        verdichtete Sicht — bester SNR, kuerzester Weg —, weil das die Nachricht
        besser beschreibt als der zufaellig erste Empfang. Jeder Empfang wird
        zusaetzlich einzeln in ``receptions`` festgehalten, samt seinem Pfad.
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
                message_id = cur.lastrowid
                self._insert_reception(message_id, observer, path, hops, snr, rssi, now)
                self._db.commit()
                message = StoredMessage(
                    id=message_id, channel=channel, sent_at=sent_at, received_at=now,
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
            self._insert_reception(row["id"], observer, path, hops, snr, rssi, now)
            self._db.commit()
            cur = self._db.execute("SELECT * FROM messages WHERE id = ?", (row["id"],))
            return _row_to_message(cur.fetchone()), False

    def _insert_reception(self, message_id: int, observer: Optional[str],
                          path: Optional[list[str]], hops: Optional[int],
                          snr: Optional[float], rssi: Optional[float], now: int) -> None:
        """Einen einzelnen Empfang festhalten. Immer unter dem Lock aufrufen."""
        self._db.execute(
            """INSERT INTO receptions (message_id, observer, path, hops, snr, rssi, received_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (message_id, observer, json.dumps(path) if path else None, hops, snr, rssi, now),
        )

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

    # --- Knoten und Wege -------------------------------------------------

    def record_node(self, pubkey: str, name: Optional[str],
                    mode: Optional[str] = None, seen_at: Optional[int] = None
                    ) -> Optional[tuple[str, Optional[str]]]:
        """Einen Knoten aus einem Advert vermerken.

        Der Name darf sich aendern, deshalb wird er bei jedem Advert
        ueberschrieben. Ein leerer Name laesst den bisherigen stehen: ein Advert
        ohne Namensfeld soll einen bekannten Namen nicht loeschen.

        Rueckgabe ist der **Uebergang**, falls es einen gab: ``("neu", None)``
        oder ``("umbenannt", alter_name)``, sonst ``None``. Aufrufer, die das
        nicht brauchen, ignorieren den Wert. Der Advert-Strom selbst ist mit
        rund 650 Meldungen am Tag nichts, was man ansehen will — die paar
        Uebergaenge darin schon.
        """
        pubkey = pubkey.lower()
        now = int(time.time()) if seen_at is None else seen_at
        with self._lock:
            vorher = self._db.execute(
                "SELECT name FROM nodes WHERE pubkey = ?", (pubkey,)
            ).fetchone()
            self._db.execute(
                """INSERT INTO nodes (pubkey, name, mode, first_seen, last_seen)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(pubkey) DO UPDATE SET
                       name      = COALESCE(excluded.name, nodes.name),
                       mode      = COALESCE(excluded.mode, nodes.mode),
                       last_seen = MAX(excluded.last_seen, nodes.last_seen)""",
                (pubkey, name or None, mode or None, now, now),
            )
            self._db.commit()

        if vorher is None:
            return ("neu", None)
        alt = vorher["name"]
        # Nur echte Umbenennungen. Ein Advert ohne Namensfeld loescht nichts und
        # ist deshalb auch keine Umbenennung.
        if name and alt and name != alt:
            return ("umbenannt", alt)
        return None

    def resolve_prefixes(self, prefixes: set[str]) -> dict[str, list[dict[str, Any]]]:
        """Pfad-Praefixe auf bekannte Knoten abbilden.

        Ein Pfadeintrag nennt nur die ersten Bytes des Public Key, und wie viele
        das sind, schwankt: in denselben Daten kommen ein Byte und zwei Byte vor.
        Bei einem Byte gibt es nur 256 moegliche Werte, Doppelbelegungen sind
        also normal und kein Fehler — deshalb eine *Liste* je Praefix.

        Weitergereicht wird ein Paket nur von Repeatern. Passt mehr als ein
        Knoten und ist genau einer davon ein Repeater, ist die Sache damit
        entschieden; sonst bleiben alle Kandidaten stehen und die Anzeige sagt,
        dass es mehrdeutig ist. Geraten wird nicht.
        """
        if not prefixes:
            return {}
        out: dict[str, list[dict[str, Any]]] = {}
        with self._lock:
            for prefix in prefixes:
                clean = prefix.lower().strip()
                if not clean or not all(c in "0123456789abcdef" for c in clean):
                    continue
                rows = self._db.execute(
                    "SELECT pubkey, name, mode FROM nodes WHERE pubkey LIKE ? ORDER BY name",
                    (clean + "%",),
                ).fetchall()
                if not rows:
                    continue
                candidates = [
                    {"pubkey": r["pubkey"], "name": r["name"], "mode": r["mode"]}
                    for r in rows
                ]
                repeaters = [c for c in candidates if (c["mode"] or "").lower() == "repeater"]
                if len(repeaters) == 1 and len(candidates) > 1:
                    candidates = repeaters
                out[clean] = candidates
        return out

    def receptions_for(self, message_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
        """Die einzelnen Empfaenge zu mehreren Nachrichten, in Empfangsreihenfolge."""
        if not message_ids:
            return {}
        placeholders = ",".join("?" for _ in message_ids)
        with self._lock:
            rows = self._db.execute(
                f"""SELECT message_id, observer, path, hops, snr, rssi, received_at
                    FROM receptions WHERE message_id IN ({placeholders}) ORDER BY id""",
                message_ids,
            ).fetchall()
        out: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            out.setdefault(row["message_id"], []).append({
                "observer": row["observer"],
                "path": json.loads(row["path"]) if row["path"] else None,
                "hops": row["hops"],
                "snr": row["snr"],
                "rssi": row["rssi"],
                "received_at": row["received_at"],
            })
        return out

    def known_nodes(self) -> int:
        with self._lock:
            row = self._db.execute("SELECT COUNT(*) AS n FROM nodes").fetchone()
        return row["n"] if row else 0

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
            # Erst die Empfaenge: SQLite erzwingt Fremdschluessel per Vorgabe
            # nicht, verwaiste Zeilen wuerden sonst stehenbleiben.
            self._db.execute(
                """DELETE FROM receptions WHERE message_id IN
                   (SELECT id FROM messages WHERE received_at < ?)""",
                (cutoff,),
            )
            cur = self._db.execute("DELETE FROM messages WHERE received_at < ?", (cutoff,))
            self._db.commit()
            return cur.rowcount
