"""Durchstich: MQTT-Paket rein, Discord-Post raus.

Die Einzelteile sind anderswo geprueft. Hier geht es um die Verdrahtung —
und um die eine Eigenschaft, an der der ganze Spiegel haengt: **jede Nachricht
genau einmal**, obwohl der Observer dasselbe Paket zwei- bis dreimal hoert.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Settings, parse_channels  # noqa: E402
from app.main import create_app  # noqa: E402
from test_decode import kf_packet  # noqa: E402

WEBHOOK = "https://discord.com/api/webhooks/1/abcdef"


def paket(text: str, *, hash_: str, snr: str = "11.5") -> bytes:
    """Ein Observer-Paket, wie es auf meshcore/+/+/packets liegt."""
    return json.dumps({
        "type": "PACKET",
        "packet_type": "5",
        "raw": kf_packet(text).hex(),
        "hash": hash_,
        "SNR": snr,
        "origin": "AT-VL-Noetsch-ObsBot",
        "decoded": {},
    }).encode("utf-8")


@pytest.fixture
def kette(tmp_path):
    app = create_app(Settings(
        mqtt_host="127.0.0.1",
        channels=parse_channels("#kf=Kaernten funkt"),
        db_path=str(tmp_path / "test.db"),
        discord_webhooks={"kf": WEBHOOK},
        # Der Sendethread laeuft hier nicht; geprueft wird, was eingereiht wird.
        discord_start_still_s=0.0,
    ))
    return app.state.collector, app.state.discord


def eingereiht(sink) -> list:
    weg = sink.wege["kf"]
    return [weg.warteschlange.get_nowait() for _ in range(weg.warteschlange.qsize())]


def test_kanalnachricht_landet_bei_discord(kette):
    collector, sink = kette
    collector.ingest(paket("AT-VL-Noetsch: Wetter mies hier oben", hash_="AA11"))

    posts = eingereiht(sink)
    assert len(posts) == 1
    # Der Absender aus dem Funk wird zum Anzeigenamen — der Kanalname steht
    # nicht in der Zeile, er ist der Discord-Kanal.
    assert posts[0].username == "AT-VL-Noetsch"
    assert posts[0].inhalt.startswith("Wetter mies hier oben")
    assert "SNR 11.5 dB" in posts[0].inhalt


def test_dasselbe_paket_mehrfach_gehoert_wird_einmal_gepostet(kette):
    collector, sink = kette
    # Genau der Normalfall: 2,6 Empfaenge je Paket, weil die Repeater es
    # weiterreichen und mehrere Observer es hoeren.
    for _ in range(3):
        collector.ingest(paket("AT-VL-Noetsch: Hallo", hash_="BB22"))

    assert len(eingereiht(sink)) == 1


def test_fremder_kanal_kommt_nicht_durch(tmp_path):
    # Ein Kanal, dessen Schluessel wir nicht haben, ist nicht entschluesselbar
    # und darf nirgends auftauchen.
    app = create_app(Settings(
        mqtt_host="127.0.0.1",
        channels=parse_channels("#at-ktn=Kaernten"),   # nicht #kf
        db_path=str(tmp_path / "test.db"),
        discord_webhooks={"at-ktn": WEBHOOK},
        discord_start_still_s=0.0,
    ))
    app.state.collector.ingest(paket("Wer auch immer: geheim", hash_="CC33"))
    assert app.state.discord.wege["at-ktn"].warteschlange.qsize() == 0


def test_ohne_webhooks_bleibt_alles_wie_vorher(tmp_path):
    # Die bestehende kf-Instanz: dieselbe Verarbeitung, nur ohne Spiegel.
    app = create_app(Settings(
        mqtt_host="127.0.0.1",
        channels=parse_channels("#kf=Kaernten funkt"),
        db_path=str(tmp_path / "test.db"),
    ))
    nachricht = app.state.collector.ingest(paket("AT-VL-Noetsch: Hallo", hash_="DD44"))

    assert nachricht is not None          # gespeichert wird weiterhin
    assert app.state.discord.aktiv is False


def test_knotenuebergang_geht_an_das_knotenziel(tmp_path):
    app = create_app(Settings(
        mqtt_host="127.0.0.1",
        channels=parse_channels("#kf=Kaernten funkt"),
        db_path=str(tmp_path / "test.db"),
        discord_webhooks={"kf": WEBHOOK, "_knoten": WEBHOOK},
        discord_start_still_s=0.0,
        discord_knoten=True,
        discord_warmup_min=0,
    ))
    advert = json.dumps({
        "type": "PACKET", "packet_type": "4", "raw": "00",
        "decoded": {"advert_parse_ok": True, "public_key": "a1b2c3d4e5f6",
                    "name": "AT-SP-Neuling"},
    }).encode("utf-8")

    app.state.collector.ingest(advert)
    app.state.collector.ingest(advert)          # zweites Advert, kein Uebergang

    weg = app.state.discord.wege["_knoten"]
    assert weg.warteschlange.qsize() == 1
    assert "AT-SP-Neuling" in weg.warteschlange.get_nowait().inhalt


def test_aufwaermfrist_haelt_die_erste_welle_zurueck(tmp_path):
    # Beim ersten Start ist jeder Knoten neu. Ohne Frist waeren das drei
    # Dutzend Meldungen am Stueck.
    app = create_app(Settings(
        mqtt_host="127.0.0.1",
        channels=parse_channels("#kf=Kaernten funkt"),
        db_path=str(tmp_path / "test.db"),
        discord_webhooks={"_knoten": WEBHOOK},
        discord_start_still_s=0.0,
        discord_knoten=True,
        discord_warmup_min=30,
    ))
    advert = json.dumps({
        "type": "PACKET", "packet_type": "4", "raw": "00",
        "decoded": {"advert_parse_ok": True, "public_key": "a1b2c3d4e5f6",
                    "name": "AT-SP-Neuling"},
    }).encode("utf-8")

    app.state.collector.ingest(advert)

    assert app.state.discord.wege["_knoten"].warteschlange.qsize() == 0
