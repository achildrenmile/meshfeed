"""Tests fuer die Karten-Quelle.

Zwei Dinge tragen hier alles andere: dass die Uebersetzung ins Observer-Format
stimmt, und dass ein Wechsel der Quelle **keine** Dubletten erzeugt. Der zweite
Punkt ist der, der im Betrieb weh taete — dieselbe Funkzeile zweimal in Discord,
weil zwei Quellen denselben Hash unterschiedlich schreiben.

Kein Test spricht mit der echten Karte: die Abrufe werden ersetzt.
"""

from __future__ import annotations

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Settings, parse_channels  # noqa: E402
from app.karte import KartenQuelle, als_observer_paket  # noqa: E402
from app.store import Store  # noqa: E402
from test_decode import kf_packet  # noqa: E402

# Echter Ausschnitt aus /api/packets vom 01.09.2026, gekuerzt.
KANALPAKET = {
    "id": 198175,
    "hash": "347B6C454FA31567",
    "raw_hex": "1506CF288BBFF5A9D9C5BCF4A13081DEB4334065D9B4617C77A48B14B203E4E8F0B9B945F55EC50FDA81B5",
    "payload_type": 5,
    "snr": 11.5,
    "rssi": -97,
    "path_json": '["CF","28","8B"]',
    "observer_name": "AT-SV-Observer",
    "timestamp": "2026-09-01T07:34:51Z",
    "decoded_json": '{"type":"CHAN","channel":"#test","text":"IU3LYA: Test","sender":"IU3LYA"}',
}

ADVERT = {
    "id": 198174,
    "hash": "aabbccddeeff0011",
    "raw_hex": "04aabb",
    "payload_type": 4,
    "snr": 9.0,
    "rssi": -80,
    "path_json": "[]",
    "observer_name": "AT-WO-Observer",
    "timestamp": "2026-09-01T07:30:00Z",
    "decoded_json": json.dumps({
        "type": "ADVERT",
        "pubKey": "10fc6e4c7dcd1021bfc25aba89ce112d80b393c51f9191f902a22653fd92fcf0",
        "timestamp": 1788241135,
        "signatureValid": True,
        "flags": {"repeater": True, "room": False, "sensor": False, "chat": False},
        "name": "AT-SP-Neuling",
    }),
}


def einstellungen(tmp_path, **kwargs) -> Settings:
    return Settings(
        quelle="http",
        channels=parse_channels("#kf=Kaernten funkt"),
        db_path=str(tmp_path / "test.db"),
        **kwargs,
    )


# --- Uebersetzung --------------------------------------------------------

def test_kanalpaket_wird_uebersetzt():
    p = als_observer_paket(KANALPAKET)
    assert p["type"] == "PACKET"
    # Die Karte zaehlt als Zahl, ingest() vergleicht mit "5".
    assert p["packet_type"] == "5"
    assert p["raw"] == KANALPAKET["raw_hex"]
    assert p["SNR"] == 11.5 and p["RSSI"] == -97
    assert p["origin"] == "AT-SV-Observer"
    assert p["decoded"]["path"] == ["CF", "28", "8B"]


def test_hash_wird_klein_geschrieben():
    # Der Observer liefert Grossbuchstaben, die Karte klein. Ohne Angleichung
    # steht dieselbe Nachricht zweimal in der Datenbank.
    assert als_observer_paket(KANALPAKET)["hash"] == "347b6c454fa31567"


def test_klartext_der_karte_wird_nicht_uebernommen():
    # Wir entschluesseln selbst aus raw_hex — sonst haetten wir zwei
    # Dekodierwege und muessten uns auf die Kanalliste der Karte verlassen,
    # die #oeradio nicht kennt.
    p = als_observer_paket(KANALPAKET)
    assert "text" not in p and "sender" not in p
    assert "text" not in p["decoded"]


def test_advert_wird_uebersetzt():
    d = als_observer_paket(ADVERT)["decoded"]
    assert d["advert_parse_ok"] is True
    assert d["public_key"].startswith("10fc6e4c")
    assert d["name"] == "AT-SP-Neuling"
    assert d["mode"] == "repeater"
    assert d["advert_time"] == 1788241135


def test_advert_ohne_gueltige_signatur_gilt_als_ungeprueft():
    roh = {**ADVERT, "decoded_json": json.dumps({
        "type": "ADVERT", "pubKey": "aa" * 32, "signatureValid": False, "name": "Falsch"})}
    # advert_parse_ok False heisst: _record_advert uebernimmt nichts.
    assert als_observer_paket(roh)["decoded"]["advert_parse_ok"] is False


def test_unbrauchbare_eintraege_geben_nichts():
    assert als_observer_paket({"hash": "ab"}) is None                    # kein Typ
    assert als_observer_paket({"payload_type": 5, "hash": "ab"}) is None  # kein raw_hex
    assert als_observer_paket({**ADVERT, "decoded_json": "kein json"}) is None


# --- Abrufschleife -------------------------------------------------------

def quelle(tmp_path, antworten, **kwargs):
    """KartenQuelle mit vorgegebenen Antworten statt Netz."""
    gerufen = []

    def abruf(url):
        gerufen.append(url)
        return antworten.pop(0) if antworten else {"packets": [], "total": 0}

    q = KartenQuelle(einstellungen(tmp_path, **kwargs), Store(":memory:"), abruf=abruf)
    return q, gerufen


def test_erster_lauf_holt_keine_vergangenheit(tmp_path):
    # Nach einem Neustart darf nichts Altes nachkommen — dieselbe Regel wie
    # beim MQTT-Weg, wo clean_session das erledigt.
    q, gerufen = quelle(tmp_path, [{"packets": [], "total": 0}])
    assert abs(q.seit - time.time()) < 5
    q.runde()
    assert "since=" in gerufen[0]


def test_fenster_wandert_mit_dem_juengsten_paket(tmp_path):
    q, _ = quelle(tmp_path, [{"packets": [KANALPAKET, ADVERT], "total": 2}])
    q.seit = 0.0
    q.runde()
    # 2026-09-01T07:34:51Z ist das juengere der beiden
    assert q.seit == pytest.approx(1788248091, abs=2)


def test_ueberlappung_geht_zurueck(tmp_path):
    q, gerufen = quelle(tmp_path, [{"packets": [], "total": 0}],
                        karte_ueberlappung_s=60.0)
    q.seit = 1788248091.0
    q.runde()
    # 60 s vor dem Stand, damit verspaetet einsortierte Pakete nicht durchfallen
    assert "07%3A33%3A51Z" in gerufen[0] or "07:33:51Z" in gerufen[0]


def test_pakete_landen_in_der_verarbeitung(tmp_path):
    q, _ = quelle(tmp_path, [{"packets": [KANALPAKET, ADVERT], "total": 2}])
    q.seit = 0.0
    assert q.runde() == 2
    assert q.messages_received == 2
    assert q.adverts_seen == 1          # das Advert ist durchgelaufen
    assert q.connected.is_set()


def test_seitendeckel_zaehlt_was_liegen_bleibt(tmp_path):
    # Nach einer laengeren Stoerung darf der Spiegel nicht alles auf einmal
    # nachschieben. Was wegfaellt, wird gezaehlt statt still verschluckt.
    volle_seite = {"packets": [KANALPAKET] * 2, "total": 100}
    q, gerufen = quelle(tmp_path, [dict(volle_seite) for _ in range(6)],
                        karte_limit=2, karte_max_seiten=3)
    q.seit = 0.0
    q.runde()
    assert len(gerufen) == 3
    assert q.verworfen_deckel == 94     # 100 gesamt, 6 geholt


def test_fehler_beim_abruf_wird_vermerkt(tmp_path):
    def abruf(url):
        raise OSError("502 Bad Gateway")

    q = KartenQuelle(einstellungen(tmp_path), Store(":memory:"), abruf=abruf)
    q.connected.set()
    with pytest.raises(OSError):
        q.runde()
    # Die Schleife faengt das und vermerkt es; hier nur der Beleg, dass der
    # Fehler nicht verschluckt wird.
    assert q.connected.is_set()         # erst die Schleife loescht es


# --- Der eigentliche Punkt ----------------------------------------------

def test_quellenwechsel_erzeugt_keine_dublette(tmp_path):
    """Dieselbe Nachricht ueber beide Wege — genau eine Zeile."""
    roh = kf_packet("AT-VL-Noetsch: Hallo")
    store = Store(str(tmp_path / "beide.db"))
    s = einstellungen(tmp_path)

    # So kaeme sie vom Observer: Hash in Grossbuchstaben.
    vom_broker = json.dumps({
        "type": "PACKET", "packet_type": "5", "raw": roh.hex(),
        "hash": "AB12CD34EF56AB78", "SNR": "11.5", "RSSI": "-46",
        "origin": "AT-VL-Noetsch-ObsBot", "decoded": {},
    })
    # Und so von der Karte: derselbe Hash, klein geschrieben.
    von_der_karte = als_observer_paket({
        "payload_type": 5, "raw_hex": roh.hex(), "hash": "ab12cd34ef56ab78",
        "snr": 11.5, "rssi": -46, "observer_name": "AT-SV-Observer",
        "path_json": "[]", "timestamp": "2026-09-01T07:00:00Z",
    })

    q = KartenQuelle(s, store, abruf=lambda url: {"packets": [], "total": 0})
    q.ingest(vom_broker)
    q.ingest(json.dumps(von_der_karte))

    assert len(store.recent("kf", limit=10)) == 1


# --- Der Filter, den die Karte noetig macht -----------------------------

def _knotenmeldungen(tmp_path, adverts, **kwargs):
    """App bauen, Adverts einspeisen, zurueckgeben was nach Discord ginge."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    client = TestClient(create_app(Settings(
        quelle="http",
        channels=parse_channels("#kf=Kaernten funkt"),
        db_path=str(tmp_path / "knoten.db"),
        discord_webhooks={"_knoten": "https://discord.com/api/webhooks/1/x"},
        discord_start_still_s=0.0,
        discord_knoten=True,
        discord_warmup_min=0,
        **kwargs,
    )))
    quelle_ = client.app.state.collector
    for name, pubkey in adverts:
        quelle_.ingest(json.dumps(als_observer_paket({
            **ADVERT, "hash": pubkey[:16],
            "decoded_json": json.dumps({
                "type": "ADVERT", "pubKey": pubkey, "signatureValid": True,
                "timestamp": 1788241135, "flags": {"repeater": True}, "name": name}),
        })))
    weg = client.app.state.discord.wege["_knoten"]
    return [weg.warteschlange.get_nowait().inhalt for _ in range(weg.warteschlange.qsize())]


def test_fremde_bundeslaender_werden_nicht_gemeldet(tmp_path):
    # Die Karte hoert ganz Oesterreich. Ohne Filter stuenden Neuzugaenge aus
    # Graz und dem Burgenland im netzwache-Kanal — gemessen am 01.09.2026.
    gemeldet = _knotenmeldungen(tmp_path, [
        ("AT-SP-Neuling", "aa" * 32),
        ("AT-ST-GRAZ-STPETER", "bb" * 32),
        ("AT-EU-Neufeld-4MXB", "cc" * 32),
        ("AT-VL-Altenberg", "dd" * 32),
        ("Bergfee", "ee" * 32),
    ])
    assert len(gemeldet) == 2
    assert any("AT-SP-Neuling" in z for z in gemeldet)
    assert any("AT-VL-Altenberg" in z for z in gemeldet)


def test_ohne_muster_kommt_alles_durch(tmp_path):
    # Leeres Muster heisst ausdruecklich: alles melden.
    gemeldet = _knotenmeldungen(tmp_path, [
        ("AT-ST-GRAZ-STPETER", "bb" * 32), ("Bergfee", "ee" * 32),
    ], discord_knoten_muster="")
    assert len(gemeldet) == 2
