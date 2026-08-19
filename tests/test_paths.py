"""Wege durchs Netz: Empfaenge einzeln halten, Praefixe aufloesen.

Die Beispiele stammen aus echten Paketen des CarinthiaMesh-Observers. Wichtig
daran: die Pfadeintraege sind mal ein Byte lang, mal zwei. Genau daran soll sich
nichts festbeissen.
"""

from __future__ import annotations

import json

from app.main import attach_paths
from app.store import Store, StoredMessage


def make_store(tmp_path) -> Store:
    return Store(str(tmp_path / "test.db"))


def add_message(store: Store, *, packet_hash: str, path, observer="OBS", hops=None):
    return store.upsert(
        channel="kf", packet_hash=packet_hash, sent_at=1787000000, sender="A",
        text="hallo", hops=hops if hops is not None else (len(path) if path else None),
        snr=10.0, rssi=-50.0, observer=observer, received_at=1787000001, path=path,
    )


def test_mehrfachempfang_behaelt_jeden_weg(tmp_path):
    store = make_store(tmp_path)
    add_message(store, packet_hash="AA", path=["1b", "a1", "d7"])
    add_message(store, packet_hash="AA", path=["fc", "a9"], observer="OBS2")

    message, is_new = add_message(store, packet_hash="AA", path=["5a"], observer="OBS2")
    assert not is_new
    assert message.receptions == 3
    # Die verdichtete Zeile behaelt den kuerzesten Weg ...
    assert message.hops == 1

    # ... die Empfaenge dagegen jeden einzeln, in Reihenfolge.
    heard = store.receptions_for([message.id])[message.id]
    assert [entry["path"] for entry in heard] == [["1b", "a1", "d7"], ["fc", "a9"], ["5a"]]
    assert [entry["observer"] for entry in heard] == ["OBS", "OBS2", "OBS2"]


def test_praefix_aufloesung_bei_ein_und_zwei_byte(tmp_path):
    store = make_store(tmp_path)
    store.record_node("d79c1111222233334444555566667777888899990000aaaabbbbccccddddeeee",
                      "AT-OW-Markt Allhau", "Repeater")
    store.record_node("1baaaaaabbbbccccddddeeeeffff000011112222333344445555666677778888",
                      "T3S3-1262 Repeater", "Repeater")

    # Ein-Byte-Praefix trifft den einen, Zwei-Byte-Praefix den anderen.
    resolved = store.resolve_prefixes({"1b", "d79c"})
    assert [c["name"] for c in resolved["1b"]] == ["T3S3-1262 Repeater"]
    assert [c["name"] for c in resolved["d79c"]] == ["AT-OW-Markt Allhau"]


def test_mehrdeutiges_praefix_nennt_alle_kandidaten(tmp_path):
    store = make_store(tmp_path)
    store.record_node("ab" + "1" * 62, "Repeater Eins", "Repeater")
    store.record_node("ab" + "2" * 62, "Repeater Zwei", "Repeater")

    resolved = store.resolve_prefixes({"ab"})
    assert sorted(c["name"] for c in resolved["ab"]) == ["Repeater Eins", "Repeater Zwei"]


def test_einzelner_repeater_entscheidet_gegen_companions(tmp_path):
    """Weitergereicht wird nur von Repeatern. Ist unter den Treffern genau einer,
    ist die Sache eindeutig, auch wenn Companions dasselbe Praefix haben."""
    store = make_store(tmp_path)
    store.record_node("cd" + "1" * 62, "Handgeraet Anna", "Companion")
    store.record_node("cd" + "2" * 62, "Repeater Dobratsch", "Repeater")
    store.record_node("cd" + "3" * 62, "Handgeraet Bert", "Companion")

    resolved = store.resolve_prefixes({"cd"})
    assert [c["name"] for c in resolved["cd"]] == ["Repeater Dobratsch"]


def test_advert_ohne_namen_loescht_bekannten_namen_nicht(tmp_path):
    store = make_store(tmp_path)
    key = "ef" + "0" * 62
    store.record_node(key, "Repeater Gerlitzen", "Repeater", seen_at=100)
    store.record_node(key, None, None, seen_at=200)

    resolved = store.resolve_prefixes({"ef"})
    assert resolved["ef"][0]["name"] == "Repeater Gerlitzen"
    assert resolved["ef"][0]["mode"] == "Repeater"


def test_attach_paths_haengt_empfaenge_mit_namen_an(tmp_path):
    store = make_store(tmp_path)
    store.record_node("1b" + "0" * 62, "T3S3-1262 Repeater", "Repeater")
    message, _ = add_message(store, packet_hash="BB", path=["1b", "99"])

    payload = attach_paths(store, [message])
    assert len(payload) == 1
    heard = payload[0]["heard"]
    assert len(heard) == 1
    assert heard[0]["path"] == [
        {"prefix": "1b", "names": ["T3S3-1262 Repeater"]},
        {"prefix": "99", "names": []},   # unbekannt, bleibt Hex
    ]


def test_nachricht_ohne_pfad_bleibt_heil(tmp_path):
    """Direkt geroutete Pakete koennen ohne Pfad kommen. Das ist kein Fehler."""
    store = make_store(tmp_path)
    message, _ = add_message(store, packet_hash="CC", path=None, hops=None)

    payload = attach_paths(store, [message])
    assert payload[0]["heard"][0]["path"] == []


def test_purge_raeumt_die_empfaenge_mit_weg(tmp_path):
    store = make_store(tmp_path)
    store.upsert(channel="kf", packet_hash="DD", sent_at=1, sender="A", text="alt",
                 hops=1, snr=None, rssi=None, observer="OBS", received_at=1, path=["aa"])
    assert store.purge(retention_days=1) == 1
    rows = store._db.execute("SELECT COUNT(*) AS n FROM receptions").fetchone()
    assert rows["n"] == 0
