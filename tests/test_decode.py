"""Tests fuer Dekodierung, Konfiguration und Ablage.

Die Kanalpakete hier sind **selbst gebaut** (``build_group_packet``), nicht
mitgeschnitten: Ein echtes GRP_TXT-Paket eines Hashtag-Kanals laesst sich von
jedem entschluesseln, es hier abzulegen hiesse also, die Nachricht einer realen
Person dauerhaft zu veroeffentlichen. Der Aufbau der Pakete — Headerbyte,
Pfadbyte mit 2-Byte-Hashes, Nutzlast — ist gegen echten Verkehr geprueft und in
``build_group_packet`` nachgebaut.

Das eine echte Paket unten ist ein Routing-Paket (RESPONSE) ohne Textinhalt.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import tempfile

import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Settings, parse_channels  # noqa: E402
from app.decode import (  # noqa: E402
    PUBLIC_CHANNEL_KEY,
    Channel,
    ChannelSet,
    channel_hash_for_key,
    decode_group_text,
    derive_hashtag_key,
)
from app.store import Store  # noqa: E402

SENDER = "AT-XX-Testnode"
TEXT = "Antenne steht, Standort bleibt"
PATHS = [["5f46", "0490", "a92b"], ["5f46", "0490", "a92b", "49be"], ["5f46", "0490", "a92b", "cd52"]]


@pytest.fixture
def kf_channels() -> ChannelSet:
    return parse_channels("#kf=Kärnten funkt,#at-ktn")


# --- Schluesselableitung -------------------------------------------------

def test_hashtag_key_matches_documented_vector():
    # Gegenprobe aus der MeshCore-Doku: #test -> 9cd8fc...
    assert derive_hashtag_key("#test").hex() == "9cd8fcf22a47333b591d96a2b848b73f"


def test_hashtag_key_is_case_insensitive_and_hash_optional():
    assert derive_hashtag_key("KF") == derive_hashtag_key("#kf")


def test_at_ktn_key_matches_wiki():
    assert derive_hashtag_key("#at-ktn").hex() == "a198f68a114f515766f3abbfa96f5b11"


def test_public_key_is_not_the_hashtag_derivation():
    assert PUBLIC_CHANNEL_KEY != derive_hashtag_key("#public")


# --- Kanalnachrichten ----------------------------------------------------

@pytest.mark.parametrize("hops", [3, 4])
def test_channel_message_decrypts(kf_channels, hops):
    """Dieselbe Nachricht, unterschiedlich langer Pfad — wie bei mehreren
    Empfaengen ueber verschiedene Repeater."""
    raw = kf_packet(f"{SENDER}: {TEXT}", hops=hops)
    message = decode_group_text(raw, kf_channels, PATHS[hops - 3])
    assert message is not None
    assert message.channel.name == "#kf"
    assert message.sender == SENDER
    assert message.text == TEXT


def test_decrypts_without_path_hint(kf_channels):
    """Ohne Pfadangabe des Observers muss der Offset selbst gefunden werden."""
    message = decode_group_text(kf_packet(TEXT, hops=3), kf_channels, None)
    assert message is not None and message.text == TEXT


def test_unknown_channel_stays_closed():
    """Ein Kanal, dessen Schluessel wir nicht haben, wird nicht entschluesselt."""
    only_public = ChannelSet([Channel("public", "Public", PUBLIC_CHANNEL_KEY, "public")])
    assert decode_group_text(kf_packet(TEXT, hops=3), only_public, PATHS[0]) is None


def test_non_group_packet_is_ignored(kf_channels):
    # packet_type 1 (RESPONSE), echtes Paket aus dem Netz — reines Routing,
    # kein Textinhalt.
    raw = bytes.fromhex("0543D79CFCBFA92B1FA1BDA0D961CDD873A9429E6526D21849050FF7")
    assert decode_group_text(raw, kf_channels, ["d79c", "fcbf", "a92b"]) is None


def test_truncated_packet_returns_none(kf_channels):
    assert decode_group_text(bytes.fromhex("1543"), kf_channels, None) is None


# --- Paketbau: Randfaelle, die live selten vorkommen ---------------------

def build_group_packet(key: bytes, text: str, *, timestamp: int = 1786618209,
                       hops: int = 0, transport: bool = False) -> bytes:
    """Ein GRP_TXT-Paket bauen — dieselbe Reihenfolge wie die Firmware."""
    plaintext = timestamp.to_bytes(4, "little") + b"\x00" + text.encode("utf-8") + b"\x00"
    plaintext += b"\x00" * (-len(plaintext) % 16)
    encryptor = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend()).encryptor()
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()
    mac = hmac.new(key + b"\x00" * 16, ciphertext, hashlib.sha256).digest()[:2]

    route = 0x03 if transport else 0x01
    header = bytes([(0x05 << 2) | route])
    transport_bytes = b"\xaa\xbb\xcc\xdd" if transport else b""
    path = bytes(range(1, hops * 2 + 1))
    return header + transport_bytes + bytes([0x40 | hops]) + path + bytes([channel_hash_for_key(key)]) + mac + ciphertext


def kf_packet(text: str, *, hops: int = 0) -> bytes:
    """Kurzform: Paket auf #kf."""
    return build_group_packet(derive_hashtag_key("#kf"), text, hops=hops)


@pytest.mark.parametrize("hops", [0, 1, 5])
def test_synthetic_packet_roundtrip(kf_channels, hops):
    raw = build_group_packet(derive_hashtag_key("#kf"), "Fritz: Test", hops=hops)
    message = decode_group_text(raw, kf_channels, None)
    assert message is not None
    assert message.sender == "Fritz" and message.text == "Test"


def test_transport_route_has_four_extra_bytes(kf_channels):
    raw = build_group_packet(derive_hashtag_key("#kf"), "Test ohne Absender", hops=2, transport=True)
    message = decode_group_text(raw, kf_channels, None)
    assert message is not None
    assert message.sender is None and message.text == "Test ohne Absender"


def test_wrong_key_fails_mac(kf_channels):
    raw = build_group_packet(derive_hashtag_key("#nichtunser"), "geheim", hops=1)
    assert decode_group_text(raw, kf_channels, None) is None


def test_umlauts_survive(kf_channels):
    raw = build_group_packet(derive_hashtag_key("#kf"), "Grüße vom Dobratsch, 60 °C", hops=1)
    message = decode_group_text(raw, kf_channels, None)
    assert message is not None and message.text == "Grüße vom Dobratsch, 60 °C"


# --- Konfiguration -------------------------------------------------------

def test_parse_channels_labels_and_slugs():
    channels = parse_channels("#kf=Kärnten funkt,public=Public")
    assert [c.slug for c in channels.all()] == ["kf", "public"]
    assert channels.by_slug("kf").label == "Kärnten funkt"
    assert channels.by_slug("public").key == PUBLIC_CHANNEL_KEY


def test_parse_channels_without_label_uses_name():
    assert parse_channels("#kf").by_slug("kf").label == "#kf"


def test_explicit_channel_key():
    channels = parse_channels("", "intern=00112233445566778899aabbccddeeff=Intern")
    assert channels.by_slug("intern").key == bytes.fromhex("00112233445566778899aabbccddeeff")


def test_bad_channel_key_is_rejected():
    with pytest.raises(ValueError):
        parse_channels("", "intern=keinhex=Intern")


def test_theme_falls_back_when_unknown():
    from app.config import DEFAULT_THEME, available_themes, resolve_theme

    assert "kf" in available_themes() and "plain" in available_themes()
    assert resolve_theme("kf") == "kf"
    assert resolve_theme("gibtsnicht") == DEFAULT_THEME
    assert resolve_theme("") == DEFAULT_THEME


def test_theme_rejects_path_traversal():
    """Der Wert landet in einem <link href> — nur harmlose Namen zulassen."""
    from app.config import DEFAULT_THEME, resolve_theme

    for bad in ("../../etc/passwd", "kf.css", "kf/../plain", "a b", "KF"):
        assert resolve_theme(bad) == DEFAULT_THEME


def test_settings_require_channels():
    # Ohne Kanal gibt es nichts anzuzeigen — das gilt fuer jede Quelle.
    with pytest.raises(ValueError):
        Settings.from_env({"MQTT_HOST": "broker"})


def test_jede_quelle_verlangt_nur_ihr_eigenes():
    # Der Broker ist nur noch Pflicht, wenn auch ueber ihn gelesen wird.
    with pytest.raises(ValueError):
        Settings.from_env({"CHANNELS": "#kf", "QUELLE": "mqtt"})

    # Vorgabe ist die Karte, und die braucht keinen Broker.
    s = Settings.from_env({"CHANNELS": "#kf"})
    assert s.quelle == "http"
    assert s.karte_url.startswith("https://")

    # Ein Tippfehler in QUELLE soll auffallen, nicht still auf etwas
    # zurueckfallen — sonst laeuft der Dienst an der falschen Quelle.
    with pytest.raises(ValueError):
        Settings.from_env({"CHANNELS": "#kf", "QUELLE": "brieftaube"})


def test_favicon_falls_back_to_logo():
    base = {"MQTT_HOST": "broker", "CHANNELS": "#kf", "SITE_LOGO_URL": "/static/logo.png"}
    assert Settings.from_env(base).site_favicon_url == "/static/logo.png"
    assert Settings.from_env({**base, "SITE_FAVICON_URL": "/static/favicon.png"}).site_favicon_url \
        == "/static/favicon.png"
    assert Settings.from_env({"MQTT_HOST": "broker", "CHANNELS": "#kf"}).site_favicon_url == ""


def test_settings_from_env():
    settings = Settings.from_env({
        "MQTT_HOST": "broker", "MQTT_PORT": "1884", "CHANNELS": "#kf=KF",
        "RETENTION_DAYS": "7", "SITE_TITLE": "Test",
    })
    assert settings.mqtt_port == 1884
    assert settings.retention_days == 7
    assert settings.channels.by_slug("kf").label == "KF"


# --- Ablage und Ingest ---------------------------------------------------

@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(os.path.join(tmp, "test.db"))
        yield store
        store.close()


def test_repeated_reception_counts_once(store):
    for snr, observer in ((3.0, "obs-a"), (12.0, "obs-b"), (7.0, "obs-a")):
        message, is_new = store.upsert(
            channel="kf", packet_hash="ABC", sent_at=1786618209, sender="Fritz",
            text="Hallo", hops=3, snr=snr, rssi=-100.0, observer=observer,
        )
    assert is_new is False
    assert message.receptions == 3
    assert message.snr == 12.0            # bester Empfang gewinnt
    assert message.observers == ["obs-a", "obs-b"]
    assert len(store.recent(channel="kf")) == 1


def test_observers_come_from_the_data(store):
    store.upsert(channel="kf", packet_hash="A", sent_at=1, sender=None, text="a",
                 hops=None, snr=None, rssi=None, observer="AT-XX-Observer")
    store.upsert(channel="kf", packet_hash="A", sent_at=1, sender=None, text="a",
                 hops=None, snr=None, rssi=None, observer="AT-YY-Zweiter")
    store.upsert(channel="kf", packet_hash="B", sent_at=2, sender=None, text="b",
                 hops=None, snr=None, rssi=None, observer=None)
    assert store.observers() == ["AT-XX-Observer", "AT-YY-Zweiter"]


def test_observers_empty_without_data(store):
    assert store.observers() == []


def test_retention_purge(store):
    store.upsert(channel="kf", packet_hash="ALT", sent_at=1, sender=None, text="alt",
                 hops=None, snr=None, rssi=None, observer=None, received_at=1)
    store.upsert(channel="kf", packet_hash="NEU", sent_at=2, sender=None, text="neu",
                 hops=None, snr=None, rssi=None, observer=None)
    assert store.purge(30) == 1
    assert [m.text for m in store.recent()] == ["neu"]


def test_purge_disabled_keeps_everything(store):
    store.upsert(channel="kf", packet_hash="ALT", sent_at=1, sender=None, text="alt",
                 hops=None, snr=None, rssi=None, observer=None, received_at=1)
    assert store.purge(0) == 0
    assert len(store.recent()) == 1


def test_collector_ingest_end_to_end(store):
    from app.collector import Collector

    settings = Settings.from_env({"MQTT_HOST": "broker", "CHANNELS": "#kf=KF", "DB_PATH": ":memory:"})
    seen: list[tuple] = []
    collector = Collector(settings, store, on_message=lambda m, new: seen.append((m, new)))

    packet = {
        "type": "PACKET", "packet_type": "5", "origin": "AT-XX-Observer",
        "raw": kf_packet(TEXT, hops=3).hex(), "hash": "8488FB95C455BD40",
        "SNR": "12.0", "RSSI": "-50",
        "decoded": {"kind": "GRP_TXT", "path": PATHS[0]},
    }
    first = collector.ingest(json.dumps(packet))
    assert first is not None and first.text == TEXT and seen[-1][1] is True

    # zweiter Empfang derselben Nachricht -> nur Zaehler. Anderer Pfad, gleicher
    # Hash: genau so kommt es vom Observer.
    packet["raw"], packet["decoded"]["path"] = kf_packet(TEXT, hops=4).hex(), PATHS[1]
    second = collector.ingest(json.dumps(packet))
    assert second.receptions == 2 and seen[-1][1] is False
    assert collector.messages_decoded == 2 and len(store.recent()) == 1


def test_collector_ignores_other_packet_types(store):
    from app.collector import Collector

    settings = Settings.from_env({"MQTT_HOST": "broker", "CHANNELS": "#kf=KF"})
    collector = Collector(settings, store)
    assert collector.ingest(json.dumps({"type": "PACKET", "packet_type": "4", "raw": "00"})) is None
    assert collector.ingest(json.dumps({"type": "STATUS"})) is None
    assert collector.packets_seen == 0
