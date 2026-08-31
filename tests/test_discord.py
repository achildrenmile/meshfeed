"""Tests fuer den Discord-Spiegel.

Der Schwerpunkt liegt auf dem, was im Betrieb weh tut und was man von aussen
nicht sieht: dass ein kaputter Webhook **nicht** wiederholt wird (sonst laufen
wir in Discords 10.000-ungueltige-Anfragen-Sperre und der ganze Host fliegt
raus), dass ein 429 respektiert wird, und dass ein aus dem Funk getipptes
@everyone niemanden anpingt.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import discord as discord_modul  # noqa: E402
from app.config import parse_webhooks  # noqa: E402
from app.discord import DiscordSink, kuerze, saeubere_namen  # noqa: E402

URL = "https://discord.com/api/webhooks/1/abcdef"


class FakeAntwort:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class Aufzeichnung:
    """Ersatz fuer ``urlopen``: merkt sich die Anfragen und antwortet nach Plan."""

    def __init__(self, antworten=None):
        self.anfragen = []
        self.antworten = list(antworten or [])

    def __call__(self, anfrage, timeout=None):
        self.anfragen.append(json.loads(anfrage.data.decode("utf-8")))
        if self.antworten:
            antwort = self.antworten.pop(0)
            if isinstance(antwort, Exception):
                raise antwort
        return FakeAntwort()


def http_fehler(code: int, koerper: dict | None = None) -> urllib.error.HTTPError:
    import io
    return urllib.error.HTTPError(
        URL, code, "fehler", {},  # type: ignore[arg-type]
        io.BytesIO(json.dumps(koerper or {}).encode("utf-8")),
    )


def warte_auf(bedingung, grenze: float = 3.0) -> bool:
    ende = time.monotonic() + grenze
    while time.monotonic() < ende:
        if bedingung():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def sink(monkeypatch):
    erzeugt = []

    def bauen(antworten=None, **kwargs):
        aufzeichnung = Aufzeichnung(antworten)
        monkeypatch.setattr(discord_modul.urllib.request, "urlopen", aufzeichnung)
        kwargs.setdefault("min_abstand_s", 0.0)
        kwargs.setdefault("start_still_s", 0.0)
        s = DiscordSink({"at-ktn": URL}, **kwargs)
        s.start()
        erzeugt.append(s)
        return s, aufzeichnung

    yield bauen
    for s in erzeugt:
        s.stop()


# --- Namen und Laengen ---------------------------------------------------

def test_leerer_absender_wird_nicht_leer_gesendet():
    # Discord antwortet auf einen leeren username mit 400, und ein 400 in
    # Schleife zaehlt gegen dieselbe Sperre wie ein 401.
    assert saeubere_namen(None) == "unbekannt"
    assert saeubere_namen("   ") == "unbekannt"


def test_verbotene_namen_werden_ersetzt():
    assert "discord" not in saeubere_namen("Discord-Bot").lower()
    assert "clyde" not in saeubere_namen("clyde").lower()


def test_name_wird_auf_80_zeichen_gekuerzt_und_einzeilig():
    assert len(saeubere_namen("A" * 200)) == 80
    assert "\n" not in saeubere_namen("AT-VL\nNoetsch")


def test_inhalt_wird_auf_2000_zeichen_gekuerzt():
    assert len(kuerze("x" * 5000)) == 2000


# --- Einliefern ----------------------------------------------------------

def test_kanal_ohne_webhook_wird_verworfen(sink):
    s, aufzeichnung = sink()
    # Kein Sammelkanal als Auffangbecken: was kein Ziel hat, faellt weg.
    assert s.post("kein-ziel", "wer", "was") is False
    assert aufzeichnung.anfragen == []


def test_startfenster_verwirft(sink):
    s, aufzeichnung = sink(start_still_s=30.0)
    assert s.post("at-ktn", "wer", "was") is False
    assert aufzeichnung.anfragen == []


def test_volle_warteschlange_verwirft_statt_zu_puffern():
    # Ohne laufenden Arbeiter, damit die Schlange wirklich volllaeuft.
    s = DiscordSink({"at-ktn": URL}, warteschlange_max=2, start_still_s=0.0)
    assert s.post("at-ktn", "a", "1") is True
    assert s.post("at-ktn", "b", "2") is True
    assert s.post("at-ktn", "c", "3") is False
    assert s.wege["at-ktn"].verworfen_voll == 1
    assert s.wege["at-ktn"].warteschlange.qsize() == 2


# --- Senden --------------------------------------------------------------

def test_erwaehnungen_werden_unterbunden(sink):
    s, aufzeichnung = sink()
    s.post("at-ktn", "AT-VL-Noetsch", "@everyone kommt wer zum Basteln?")
    assert warte_auf(lambda: aufzeichnung.anfragen)
    koerper = aufzeichnung.anfragen[0]
    assert koerper["allowed_mentions"] == {"parse": []}
    assert koerper["username"] == "AT-VL-Noetsch"
    assert "@everyone" in koerper["content"]  # Text bleibt lesbar, pingt nur nicht


def test_401_legt_den_weg_still_und_wiederholt_nicht(sink):
    s, aufzeichnung = sink(antworten=[http_fehler(401)])
    s.post("at-ktn", "wer", "was")
    assert warte_auf(lambda: s.wege["at-ktn"].stillgelegt)
    # Genau ein Versuch. Jeder weitere ginge auf das Konto der 10.000er-Sperre.
    assert len(aufzeichnung.anfragen) == 1
    assert s.alle_stillgelegt is True
    # Und danach nimmt der Weg nichts mehr an.
    assert s.post("at-ktn", "wer", "nochmal") is False


def test_404_legt_ebenfalls_still(sink):
    s, aufzeichnung = sink(antworten=[http_fehler(404)])
    s.post("at-ktn", "wer", "was")
    assert warte_auf(lambda: s.wege["at-ktn"].stillgelegt)
    assert len(aufzeichnung.anfragen) == 1


def test_429_wird_abgewartet_und_dann_gesendet(sink):
    s, aufzeichnung = sink(antworten=[http_fehler(429, {"retry_after": 0.01})])
    s.post("at-ktn", "wer", "was")
    assert warte_auf(lambda: s.wege["at-ktn"].gepostet == 1)
    assert len(aufzeichnung.anfragen) == 2  # einmal abgewiesen, einmal durch
    assert s.wege["at-ktn"].stillgelegt is False


def test_serverfehler_wird_wiederholt_und_dann_verworfen(sink):
    s, aufzeichnung = sink(antworten=[http_fehler(500)] * 3)
    s.post("at-ktn", "wer", "was")
    assert warte_auf(lambda: s.wege["at-ktn"].verworfen_fehler == 1, grenze=20.0)
    assert len(aufzeichnung.anfragen) == 3
    assert s.wege["at-ktn"].stillgelegt is False  # 500 ist nicht endgueltig


def test_trockenlauf_sendet_nichts(sink):
    s, aufzeichnung = sink(trockenlauf=True)
    s.post("at-ktn", "wer", "was")
    assert warte_auf(lambda: s.wege["at-ktn"].gepostet == 1)
    assert aufzeichnung.anfragen == []


# --- Konfiguration -------------------------------------------------------

def test_webhooks_werden_als_slug_gelesen():
    ziele = parse_webhooks(f"#at-ktn-bot={URL},_knoten={URL}")
    assert set(ziele) == {"at-ktn-bot", "_knoten"}


def test_webhook_ohne_https_wird_abgelehnt():
    with pytest.raises(ValueError):
        parse_webhooks("at-ktn=http://discord.com/api/webhooks/1/x")


def test_ohne_webhooks_ist_der_spiegel_aus():
    s = DiscordSink({})
    assert s.aktiv is False
    assert s.alle_stillgelegt is False  # nicht konfiguriert ist kein Fehler
    assert s.post("at-ktn", "wer", "was") is False


# --- Endpunkt ------------------------------------------------------------

def _app(tmp_path, **kwargs):
    from fastapi.testclient import TestClient

    from app.config import Settings, parse_channels
    from app.main import create_app

    return TestClient(create_app(Settings(
        mqtt_host="127.0.0.1",
        channels=parse_channels("#kf=Kaernten funkt"),
        db_path=str(tmp_path / "test.db"),
        **kwargs,
    )))


def test_ohne_spiegel_ist_der_endpunkt_gruen(tmp_path):
    # Nicht eingeschaltet ist kein Fehler.
    antwort = _app(tmp_path).get("/healthz/discord")
    assert antwort.status_code == 200
    assert antwort.json()["aktiv"] is False


def test_alle_wege_still_meldet_503_aber_healthz_bleibt_gruen(tmp_path):
    client = _app(tmp_path, discord_webhooks={"kf": URL})
    app = client.app
    # Zustand herstellen, wie ihn ein falsches Token erzeugt.
    for weg in app.state.discord.wege.values():
        weg.stillgelegt = True

    assert client.get("/healthz/discord").status_code == 503
    # Entscheidend: der Docker-Healthcheck haengt an /healthz und darf hier
    # nicht ausloesen — ein Neustart wuerde ein falsches Token nicht heilen.
    assert client.get("/healthz").status_code == 200
