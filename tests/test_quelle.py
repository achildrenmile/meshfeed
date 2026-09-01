"""Tests fuer die Stille-Erkennung an der Quelle.

Geprueft wird die Unterscheidung, um die es hier eigentlich geht: ein ruhiger
Kanal ist kein Fehler, eine tote Leitung schon. Deshalb zaehlt jede MQTT-
Nachricht als Lebenszeichen — auch eine, die gleich danach verworfen wird.
"""

from __future__ import annotations

import json
import os
import sys
import time

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Settings, parse_channels  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture
def app(tmp_path):
    """App ohne Lifespan — der Collector wird gebaut, verbindet aber nicht."""
    return create_app(Settings(
        # Ausdruecklich der MQTT-Weg: diese Datei prueft ihn samt
        # _handle_message. Die Vorgabe ist inzwischen die Karte.
        quelle="mqtt",
        mqtt_host="127.0.0.1",
        channels=parse_channels("#kf=Kaernten funkt"),
        db_path=str(tmp_path / "test.db"),
        quelle_still_minuten=30,
    ))


class Paket:
    """Minimaler Ersatz fuer paho's MQTTMessage."""

    topic = "meshcore/KLU/AB/packets"

    def __init__(self, payload: bytes) -> None:
        self.payload = payload


def test_frische_quelle_ist_ok(app):
    antwort = TestClient(app).get("/healthz/quelle")

    assert antwort.status_code == 200
    daten = antwort.json()
    assert daten["ok"] is True
    # Noch kein Paket: die Startzeit dient als Grundlinie, damit ein frisch
    # gestarteter Container nicht sofort Alarm schlaegt.
    assert daten["letztes_paket"] is None
    assert daten["grenze_s"] == 1800


def test_stille_ueber_der_grenze_meldet_503(app):
    # Ein Paket vor einer Stunde, Grenze sind 30 Minuten.
    app.state.collector.last_packet_at = time.time() - 3600

    antwort = TestClient(app).get("/healthz/quelle")

    assert antwort.status_code == 503
    daten = antwort.json()
    assert daten["ok"] is False
    assert daten["still_seit_s"] >= 1800


def test_stille_unter_der_grenze_bleibt_ok(app):
    app.state.collector.last_packet_at = time.time() - 60

    antwort = TestClient(app).get("/healthz/quelle")

    assert antwort.status_code == 200
    assert antwort.json()["ok"] is True


def test_langer_start_ohne_paket_meldet_503(app):
    """Ohne Grundlinie wuerde ein Feed, der nie ein Paket sah, ewig gruen sein."""
    app.state.collector.started_at = time.time() - 3600

    antwort = TestClient(app).get("/healthz/quelle")

    assert antwort.status_code == 503
    assert antwort.json()["letztes_paket"] is None


def test_verworfenes_paket_ist_ein_lebenszeichen(app):
    """packet_type 0 ist keine Kanalnachricht — verworfen, aber gezaehlt."""
    collector = app.state.collector
    assert collector.last_packet_at is None

    collector._handle_message(
        None, None,
        Paket(json.dumps({"type": "PACKET", "packet_type": "0"}).encode()),
    )

    assert collector.last_packet_at is not None
    assert collector.messages_received == 1
    assert collector.messages_decoded == 0


def test_kaputtes_paket_zaehlt_trotzdem(app):
    """Auch Unsinn auf der Leitung beweist, dass die Leitung steht."""
    collector = app.state.collector

    collector._handle_message(None, None, Paket(b"kein json"))

    assert collector.last_packet_at is not None
    assert collector.messages_received == 1
    assert collector.last_error is not None
