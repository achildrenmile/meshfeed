"""MQTT-Ingest.

Haengt am lokalen Broker des Observer-Stacks und liest dessen ``packets``-Topic
mit. Der Feed sendet nie etwas — weder an den Broker noch an den Node. Er ist
auch kein zweiter Client am Companion: der Node vertraegt nur zwei TCP-Clients,
und die sind mit Observer-Bridge und meshinfra belegt.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Optional

import paho.mqtt.client as mqtt

from .config import Settings
from .decode import decode_group_text
from .store import StoredMessage, Store

logger = logging.getLogger("meshfeed.collector")

# Callback: (Nachricht, is_new)
MessageHandler = Callable[[StoredMessage, bool], None]
# Callback: (Uebergang, Pubkey, Name, alter Name) — "neu" oder "umbenannt"
NodeHandler = Callable[[str, str, Optional[str], Optional[str]], None]


def _as_float(value: object) -> Optional[float]:
    """Zahlen aus dem Observer kommen als Strings, teils als "Unknown"."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


class Collector:
    def __init__(self, settings: Settings, store: Store,
                 on_message: Optional[MessageHandler] = None,
                 on_node: Optional[NodeHandler] = None) -> None:
        self.settings = settings
        self.store = store
        self.on_message = on_message
        self.on_node = on_node
        self.connected = threading.Event()
        self.packets_seen = 0
        self.messages_decoded = 0
        self.adverts_seen = 0
        self.last_error: Optional[str] = None

        # Wann zuletzt ueberhaupt etwas ueber MQTT hereinkam — vor jedem
        # Filter, unabhaengig von Pakettyp und Kanal.
        #
        # Warum nicht die Zeit der letzten Kanalnachricht: Ein ruhiger Kanal
        # ist normal, auf #kf vergeht auch mal ein Tag. Der Observer dagegen
        # hoert dauernd etwas, Adverts allein schon alle paar Minuten. Bleibt
        # es hier still, ist die Leitung tot und nicht das Funknetz.
        #
        # Startzeit als Grundlinie, damit ein frisch gestarteter Container
        # nicht sofort Alarm ausloest, bevor das erste Paket da sein kann.
        self.started_at = time.time()
        self.last_packet_at: Optional[float] = None
        self.messages_received = 0

        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=settings.mqtt_client_id,
            clean_session=True,
        )
        if settings.mqtt_user:
            self._client.username_pw_set(settings.mqtt_user, settings.mqtt_pass)
        if settings.mqtt_tls:
            self._client.tls_set()
        self._client.on_connect = self._handle_connect
        self._client.on_disconnect = self._handle_disconnect
        self._client.on_message = self._handle_message
        self._client.reconnect_delay_set(min_delay=1, max_delay=60)

    # --- Lebenszyklus ---------------------------------------------------

    def start(self) -> None:
        self._client.connect_async(self.settings.mqtt_host, self.settings.mqtt_port, keepalive=60)
        self._client.loop_start()

    def stop(self) -> None:
        self._client.loop_stop()
        try:
            self._client.disconnect()
        except Exception:  # pragma: no cover - beim Herunterfahren egal
            pass

    # --- Callbacks ------------------------------------------------------

    def _handle_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code != 0:
            self.last_error = f"Broker weist ab: {reason_code}"
            logger.error("MQTT-Verbindung abgelehnt: %s", reason_code)
            return
        self.connected.set()
        self.last_error = None
        for topic in self.settings.mqtt_topics:
            client.subscribe(topic, qos=0)
            logger.info("Abonniert: %s", topic)

    def _handle_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        self.connected.clear()
        logger.warning("MQTT getrennt (%s), paho verbindet selbst wieder", reason_code)

    def _handle_message(self, client, userdata, msg: mqtt.MQTTMessage) -> None:
        # Zuerst der Lebenszeichen-Stempel, dann erst die Verarbeitung: Auch
        # ein Paket, das gleich verworfen wird oder beim Auspacken kracht,
        # beweist, dass die Leitung steht.
        self.last_packet_at = time.time()
        self.messages_received += 1
        try:
            self.ingest(msg.payload)
        except Exception as exc:  # ein kaputtes Paket darf den Ingest nicht killen
            self.last_error = str(exc)
            logger.exception("Paket nicht verarbeitbar: %s", exc)

    # --- Zustand der Quelle ---------------------------------------------

    def quiet_seconds(self, now: Optional[float] = None) -> float:
        """Sekunden seit dem letzten Paket, ersatzweise seit dem Start."""
        now = time.time() if now is None else now
        return now - (self.last_packet_at or self.started_at)

    # --- Verarbeitung ---------------------------------------------------

    def ingest(self, payload: bytes | str) -> Optional[StoredMessage]:
        """Ein Observer-Paket verarbeiten. Rueckgabe nur bei Kanalnachrichten."""
        data = json.loads(payload)
        if data.get("type") != "PACKET":
            return None

        decoded = data.get("decoded") or {}

        # Adverts tragen Namen und Public Key der Knoten. Sie laufen ohnehin
        # ueber dieselbe Verbindung; mitgeschrieben werden sie, damit die
        # Pfad-Praefixe in den Nachrichten spaeter Namen bekommen.
        if data.get("packet_type") == "4":
            self._record_advert(decoded)
            return None

        if data.get("packet_type") != "5":
            return None

        raw_hex = data.get("raw")
        if not raw_hex:
            return None
        self.packets_seen += 1

        path = decoded.get("path")
        if path is None and data.get("path"):
            path = str(data["path"]).split(",")

        message = decode_group_text(bytes.fromhex(raw_hex), self.settings.channels, path)
        if message is None:
            return None  # fremder Kanal, dessen Schluessel wir nicht haben

        self.messages_decoded += 1
        stored, is_new = self.store.upsert(
            channel=message.channel.slug,
            packet_hash=data.get("hash") or raw_hex[:32],
            sent_at=message.sent_at,
            sender=message.sender,
            text=message.text,
            hops=len(path) if path else None,
            snr=_as_float(data.get("SNR")),
            rssi=_as_float(data.get("RSSI")),
            observer=data.get("origin"),
            path=path,
        )
        if self.on_message:
            self.on_message(stored, is_new)
        return stored

    def _record_advert(self, decoded: dict) -> None:
        """Knoten aus einem Advert vermerken.

        Der Observer dekodiert Adverts bereits selbst, inklusive Signaturpruefung
        (``advert_parse_ok``). Ohne dieses Gutzeichen wird nichts uebernommen —
        ein halb gelesenes Advert soll keinen falschen Namen in die Tabelle
        schreiben.
        """
        if not decoded.get("advert_parse_ok"):
            return
        pubkey = decoded.get("public_key")
        if not isinstance(pubkey, str) or len(pubkey) < 8:
            return
        seen = decoded.get("advert_time")
        name = (decoded.get("name") or "").strip() or None
        uebergang = self.store.record_node(
            pubkey=pubkey,
            name=name,
            mode=decoded.get("mode"),
            seen_at=int(seen) if isinstance(seen, (int, float)) else None,
        )
        self.adverts_seen += 1
        if uebergang and self.on_node:
            art, alter_name = uebergang
            self.on_node(art, pubkey, name, alter_name)
