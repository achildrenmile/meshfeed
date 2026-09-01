"""Tests fuer die Kanalwacht.

Der wichtigste steht unten: Ein fremder Kanal wird **gezaehlt und benannt**,
sein Text taucht nirgends auf. Das ist die Grenze, an der die ganze Sache
haengt — Hashtag-Schluessel sind ableitbar, also koennte man mehr. Man soll
aber nicht.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Settings, parse_channels  # noqa: E402
from app.decode import derive_hashtag_key  # noqa: E402
from app.kanalwacht import Kanalwacht  # noqa: E402
from app.store import Store  # noqa: E402
from test_decode import build_group_packet, kf_packet  # noqa: E402


def wacht(kanaele: str = "#kf") -> Kanalwacht:
    return Kanalwacht(parse_channels(kanaele))


def test_eigener_kanal_wird_unter_seinem_namen_gezaehlt():
    w = wacht()
    w.zaehle(kf_packet("Wer: Hallo"), "#kf", "E9")
    assert w.benannt["#kf"] == 1
    assert not w.unbekannt


def test_fremder_hashtag_kanal_wird_erraten_und_benannt():
    # #wardriving steht auf der Kandidatenliste, unser Feed kennt ihn nicht.
    roh = build_group_packet(derive_hashtag_key("#wardriving"), "Wer: unterwegs")
    w = wacht()
    w.zaehle(roh, None, "AA")
    assert w.benannt["#wardriving"] == 1
    assert not w.unbekannt


def test_privater_kanal_bleibt_ein_hash():
    # Zufaelliger Schluessel: kein Name der Welt loest den auf.
    roh = build_group_packet(bytes(range(16)), "Wer: geheim")
    w = wacht()
    w.zaehle(roh, None, "7c")
    assert not w.benannt
    assert w.unbekannt["7C"] == 1        # gross geschrieben vereinheitlicht
    assert "7C" in w.neue_hashes


def test_neue_hashes_nur_beim_ersten_mal():
    w = wacht()
    roh = build_group_packet(bytes(range(16)), "Wer: geheim")
    w.zaehle(roh, None, "7c")
    w.tag_abschliessen()
    w.zaehle(roh, None, "7c")
    assert w.unbekannt["7C"] == 1
    assert not w.neue_hashes             # am zweiten Tag nicht mehr neu


def test_bericht_nennt_zahlen_und_die_unschaerfe():
    w = wacht()
    w.zaehle(kf_packet("Wer: Hallo"), "#kf", "E9")
    w.zaehle(build_group_packet(bytes(range(16)), "Wer: geheim"), None, "A5")
    b = w.bericht()
    assert "#kf 1" in b
    assert "`A5` 1" in b
    # Die Unschaerfe gehoert in die Meldung, sonst liest sie sich genauer,
    # als sie ist.
    assert "ein Byte" in b
    assert "privat" in b


def test_ohne_verkehr_kein_bericht():
    assert wacht().bericht() is None


# --- Die Grenze ----------------------------------------------------------

def test_fremder_inhalt_taucht_nirgends_auf():
    """Benannt und gezaehlt ja, Inhalt nein."""
    geheim = "Wer: Treffpunkt um acht am Parkplatz"
    w = wacht()
    w.zaehle(build_group_packet(derive_hashtag_key("#wardriving"), geheim), None, "AA")

    alles = json.dumps(w.stats(), ensure_ascii=False) + (w.bericht() or "")
    assert "#wardriving" in alles          # der Name schon
    assert "Treffpunkt" not in alles       # der Text nicht
    assert "Parkplatz" not in alles


def test_fremder_kanal_kommt_nicht_in_die_datenbank(tmp_path):
    """Und er wird auch nicht gespiegelt — er faellt aus ingest heraus."""
    from app.karte import KartenQuelle

    gepostet = []
    s = Settings(quelle="http", channels=parse_channels("#kf=Kaernten funkt"),
                 db_path=str(tmp_path / "t.db"))
    q = KartenQuelle(s, Store(s.db_path), on_message=lambda m, neu: gepostet.append(m),
                     abruf=lambda url: {"packets": [], "total": 0})

    roh = build_group_packet(derive_hashtag_key("#wardriving"), "Wer: unterwegs")
    q.ingest(json.dumps({"type": "PACKET", "packet_type": "5", "raw": roh.hex(),
                         "hash": "aa" * 8, "decoded": {"channel_hash": "AA"}}))

    assert gepostet == []                            # nichts weitergereicht
    assert q.kanalwacht.benannt["#wardriving"] == 1  # aber gezaehlt
