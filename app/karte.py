"""Quelle: Pakete von der Karten-API holen statt vom eigenen Broker.

Der MQTT-Weg haengt an **einem** Observer. Die Karte sammelt dieselben Pakete
von mehreren und gibt sie ueber HTTP heraus — mehr Abdeckung, dafuer bis zu
einem Abrufintervall Verzoegerung und die Abhaengigkeit von einem Dienst, den
wir nicht betreiben.

**Entschluesselt wird trotzdem hier.** Die Karte liefert zwar bei manchen
Kanaelen fertigen Klartext mit, aber nicht bei allen — ``#oeradio`` kennt sie
nicht. Wir nehmen deshalb nur ``raw_hex`` und lassen ``decode_group_text``
darueber laufen, genau wie beim MQTT-Weg. Eine Dekodierstelle, ein Satz
Schluessel, unabhaengig davon, was die Karte gerade kann.

Gemessen am 01.09.2026: ``since`` wird ausgewertet, ``after_id``,
``payload_type`` und ``channel`` **nicht** — die aendern die Antwort nicht.
Ein Fuenf-Minuten-Fenster sind rund 11 KB.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .collector import MessageHandler, NodeHandler, Quelle
from .config import Settings
from .store import Store

logger = logging.getLogger("meshfeed.karte")

KOPF = {"User-Agent": "meshfeed (+CarinthiaMesh)"}


def _als_liste(wert: Any) -> Optional[list[str]]:
    """``path_json`` kommt als JSON-Zeichenkette, manchmal fehlt es ganz."""
    if isinstance(wert, list):
        return [str(x) for x in wert] or None
    if isinstance(wert, str) and wert.strip():
        try:
            geladen = json.loads(wert)
        except ValueError:
            return None
        if isinstance(geladen, list):
            return [str(x) for x in geladen] or None
    return None


def _mode_aus_flags(flags: Any) -> Optional[str]:
    """Die Karte gibt Rollen als Schalter, der Observer als Wort."""
    if not isinstance(flags, dict):
        return None
    for schalter, name in (("repeater", "repeater"), ("room", "room_server"),
                           ("sensor", "sensor"), ("chat", "companion")):
        if flags.get(schalter):
            return name
    return None


def als_observer_paket(eintrag: dict) -> Optional[dict]:
    """Einen Karteneintrag ins Observer-Format uebersetzen.

    Reine Funktion, kein Netz — damit sie sich gegen eine gespeicherte Antwort
    pruefen laesst. Rueckgabe ``None``, wenn der Eintrag nichts hergibt, was
    ``Quelle.ingest`` verarbeiten wuerde.

    Der ``hash`` wird **klein geschrieben**: Der Observer liefert ihn in
    Grossbuchstaben, die Karte klein. Ohne diese Angleichung stuende dieselbe
    Nachricht zweimal in der Datenbank, sobald jemand die Quelle wechselt.
    """
    roh = eintrag.get("raw_hex")
    typ = eintrag.get("payload_type")
    if typ is None:
        return None

    paket: dict[str, Any] = {
        "type": "PACKET",
        "packet_type": str(typ),          # die Karte zaehlt als Zahl, der Observer als Wort
        "raw": roh,
        "hash": (eintrag.get("hash") or "").lower() or None,
        "SNR": eintrag.get("snr"),
        "RSSI": eintrag.get("rssi"),
        "origin": eintrag.get("observer_name"),
        "decoded": {},
    }

    pfad = _als_liste(eintrag.get("path_json"))
    if pfad:
        paket["decoded"]["path"] = pfad

    # Der Kanal-Hash der Karte wird durchgereicht, nicht selbst aus dem
    # Rohpaket geholt: dessen Position haengt von der Pfadlaenge ab und ist
    # ohne passenden Schluessel nicht eindeutig. Fuer die Kanalwacht ist der
    # gemeldete Wert die verlaesslichere Angabe.
    if str(typ) == "5":
        try:
            d = json.loads(eintrag.get("decoded_json") or "{}")
            if d.get("channelHashHex"):
                paket["decoded"]["channel_hash"] = d["channelHashHex"]
        except ValueError:
            pass

    if str(typ) == "4":
        try:
            d = json.loads(eintrag.get("decoded_json") or "{}")
        except ValueError:
            return None
        if d.get("type") != "ADVERT":
            return None
        paket["decoded"].update({
            # Die Karte prueft die Signatur selbst und sagt es im Klartext.
            # Ohne gueltige Signatur uebernehmen wir keinen Namen — dieselbe
            # Schranke wie beim Observer.
            "advert_parse_ok": bool(d.get("signatureValid")),
            "public_key": d.get("pubKey"),
            "name": d.get("name"),
            "mode": _mode_aus_flags(d.get("flags")),
            "advert_time": d.get("timestamp"),
        })
    elif str(typ) == "5" and not roh:
        return None

    return paket


class KartenQuelle(Quelle):
    """Holt die Pakete im Takt von der Karte.

    ``abruf`` laesst sich fuer Tests ersetzen: eine Funktion, die eine URL
    bekommt und die geparste Antwort zurueckgibt. So braucht kein Test einen
    Server.
    """

    def __init__(self, settings: Settings, store: Store,
                 on_message: Optional[MessageHandler] = None,
                 on_node: Optional[NodeHandler] = None,
                 abruf: Optional[Callable[[str], dict]] = None) -> None:
        super().__init__(settings, store, on_message=on_message, on_node=on_node)
        self.abruf = abruf or self._hole
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Der erste Lauf holt **nichts** nach. Nach einem Neustart soll keine
        # alte Zeile mehr auftauchen — dieselbe Regel wie beim MQTT-Weg, wo
        # clean_session dafuer sorgt.
        self.seit = time.time()
        self.letzter_abruf: Optional[float] = None
        self.abrufe = 0
        self.verworfen_deckel = 0

    # --- Lebenszyklus ----------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._schleife, name="karte", daemon=True)
        self._thread.start()
        logger.info("Karten-Quelle an: %s alle %.0fs",
                    self.settings.karte_url, self.settings.karte_intervall_s)

    def stop(self) -> None:
        self._stop.set()

    # --- Abruf -----------------------------------------------------------

    def _hole(self, url: str) -> dict:
        with urllib.request.urlopen(urllib.request.Request(url, headers=KOPF),
                                    timeout=self.settings.karte_zeitlimit_s) as antwort:
            return json.load(antwort)

    def _url(self, seit: float, offset: int) -> str:
        stempel = datetime.fromtimestamp(seit, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        frage = urllib.parse.urlencode({
            "since": stempel,
            "limit": self.settings.karte_limit,
            "offset": offset,
        })
        return f"{self.settings.karte_url.rstrip('/')}/api/packets?{frage}"

    def _schleife(self) -> None:
        while not self._stop.is_set():
            try:
                self.runde()
            except Exception as exc:
                self.last_error = str(exc)
                self.connected.clear()
                logger.warning("Karte nicht erreichbar: %s", exc)
            if self._stop.wait(self.settings.karte_intervall_s):
                return

    def runde(self) -> int:
        """Ein Durchgang: abholen, uebersetzen, verarbeiten. Gibt die Anzahl zurueck.

        Das Fenster beginnt eine Ueberlappung vor dem juengsten bisher
        gesehenen Paket. Grund: Pakete koennen verspaetet einsortiert werden,
        und ein Fenster, das exakt an der letzten Sekunde ansetzt, verliert sie
        still. Die dabei doppelt geholten faengt der Dedup ab.
        """
        seit = max(self.seit - self.settings.karte_ueberlappung_s, 0)
        gesehen = 0
        neuester = self.seit

        for seite in range(self.settings.karte_max_seiten):
            antwort = self.abruf(self._url(seit, seite * self.settings.karte_limit))
            self.abrufe += 1
            self.letzter_abruf = time.time()
            self.connected.set()
            self.last_error = None

            eintraege = antwort.get("packets") or []
            for eintrag in eintraege:
                paket = als_observer_paket(eintrag)
                if paket is None:
                    continue
                self.verarbeite(json.dumps(paket))
                gesehen += 1
                wann = _zeit(eintrag.get("timestamp"))
                if wann and wann > neuester:
                    neuester = wann

            gesamt = antwort.get("total") or 0
            geholt = (seite + 1) * self.settings.karte_limit
            if len(eintraege) < self.settings.karte_limit or geholt >= gesamt:
                break
            if seite + 1 == self.settings.karte_max_seiten:
                # Nach einer laengeren Stoerung darf der Spiegel Discord nicht
                # fluten. Was hier wegfaellt, wird gezaehlt und gesagt — still
                # verschlucken waere schlimmer als der Verlust.
                self.verworfen_deckel += max(gesamt - geholt, 0)
                logger.warning("Karte: %d Pakete ueber dem Seitendeckel liegen gelassen",
                               max(gesamt - geholt, 0))

        self.seit = neuester
        return gesehen

    def stats(self) -> dict:
        return {
            "abrufe": self.abrufe,
            "letzter_abruf": self.letzter_abruf,
            "verworfen_deckel": self.verworfen_deckel,
            "fenster_ab": self.seit,
        }


def _zeit(wert: Any) -> Optional[float]:
    """``2026-09-01T07:35:08Z`` in Sekunden. Unlesbares gibt ``None``."""
    if not isinstance(wert, str) or not wert:
        return None
    try:
        return datetime.fromisoformat(wert.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
