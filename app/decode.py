"""MeshCore-Paketdekodierung fuer den Feed.

Eigenstaendig: Standardbibliothek plus ``cryptography``. Bewusst kein Import aus
meshcore-packet-capture — dieser Dienst haengt nur am MQTT-Broker des Observers
und soll unabhaengig von dessen Version bleiben.

Wire-Format GRP_TXT (identisch zur Referenz meshcore-decoder):

    payload      = channel_hash(1) + cipher_mac(2) + ciphertext(n*16)
    channel_hash = erstes Byte von SHA256(key16)
    MAC          = HMAC_SHA256(key16 + 16 Nullbytes, ciphertext)[:2]
    cipher       = AES-128-ECB ohne Padding
    plaintext    = timestamp(4, LE) + flags(1) + text(UTF-8, NUL-terminiert)

Der Kanalschluessel eines Hashtag-Kanals ist SHA256("#name")[:16].
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Iterable, Optional

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PT_GRP_TXT = 0x05

# Routentypen im Headerbyte (Bits 0-1). Bei den beiden Transport-Varianten
# stehen hinter dem Header 4 zusaetzliche Transport-Bytes.
ROUTE_TRANSPORT_FLOOD = 0x00
ROUTE_FLOOD = 0x01
ROUTE_DIRECT = 0x02
ROUTE_TRANSPORT_DIRECT = 0x03
_ROUTES_WITH_TRANSPORT = (ROUTE_TRANSPORT_FLOOD, ROUTE_TRANSPORT_DIRECT)

# Fester, allgemein bekannter Schluessel des Public-Kanals. Das ist *nicht* die
# Hashtag-Ableitung von "#public".
PUBLIC_CHANNEL_KEY = bytes.fromhex("8b3387e9c5cdea6ac9e5edbaa115cd72")


def derive_hashtag_key(name: str) -> bytes:
    """Schluessel eines Hashtag-Kanals: SHA256 des kleingeschriebenen "#name"."""
    if not name.startswith("#"):
        name = "#" + name
    return hashlib.sha256(name.lower().encode("utf-8")).digest()[:16]


def channel_hash_for_key(key16: bytes) -> int:
    """Kanalhash (erstes Byte von SHA256(key)) als Ganzzahl."""
    return hashlib.sha256(key16).digest()[0]


@dataclass(frozen=True)
class Channel:
    """Ein Kanal, den dieser Feed anzeigt."""

    name: str          # "#kf" oder ein freier Name bei explizitem Schluessel
    label: str         # Anzeigename
    key: bytes         # 16 Byte
    slug: str          # URL-Segment

    @property
    def hash_byte(self) -> int:
        return channel_hash_for_key(self.key)


class ChannelSet:
    """Kanalhash -> Kandidatenkanaele. Mehrere Kanaele koennen denselben Hash
    haben (1 Byte), deshalb eine Liste und Entscheidung per MAC."""

    def __init__(self, channels: Iterable[Channel] = ()) -> None:
        self._by_hash: dict[int, list[Channel]] = {}
        self._by_slug: dict[str, Channel] = {}
        for ch in channels:
            self.add(ch)

    def add(self, channel: Channel) -> None:
        if len(channel.key) != 16:
            raise ValueError(f"Kanalschluessel muss 16 Byte haben: {channel.name}")
        self._by_hash.setdefault(channel.hash_byte, []).append(channel)
        self._by_slug[channel.slug] = channel

    def candidates(self, hash_byte: int) -> list[Channel]:
        return self._by_hash.get(hash_byte, [])

    def by_slug(self, slug: str) -> Optional[Channel]:
        return self._by_slug.get(slug)

    def all(self) -> list[Channel]:
        return list(self._by_slug.values())

    def __len__(self) -> int:
        return len(self._by_slug)


@dataclass(frozen=True)
class GroupMessage:
    """Eine entschluesselte Kanalnachricht."""

    channel: Channel
    sender: Optional[str]
    text: str
    sent_at: int        # Unix-Sekunden, vom sendenden Node gesetzt
    flags: int


def _decrypt(ciphertext: bytes, mac: bytes, key16: bytes) -> Optional[tuple[int, int, str]]:
    """MAC pruefen und entschluesseln. None, wenn der Schluessel nicht passt."""
    if len(ciphertext) < 16 or len(ciphertext) % 16 != 0:
        return None

    key32 = key16 + b"\x00" * 16
    if not hmac.compare_digest(hmac.new(key32, ciphertext, hashlib.sha256).digest()[:2], mac[:2]):
        return None

    decryptor = Cipher(algorithms.AES(key16), modes.ECB(), backend=default_backend()).decryptor()
    plaintext = decryptor.update(ciphertext) + decryptor.finalize()
    if len(plaintext) < 5:
        return None

    timestamp = int.from_bytes(plaintext[0:4], "little")
    flags = plaintext[4]
    text = plaintext[5:].decode("utf-8", errors="ignore")
    nul = text.find("\x00")
    if nul >= 0:
        text = text[:nul]
    return timestamp, flags, text


def _split_sender(text: str) -> tuple[Optional[str], str]:
    """"Name: Nachricht" trennen, sofern der Praefix wie ein Name aussieht."""
    colon = text.find(": ")
    if 0 < colon < 50:
        candidate = text[:colon]
        if not any(c in candidate for c in ":[]"):
            return candidate, text[colon + 2:]
    return None, text


def payload_offsets(raw: bytes, path_hops: Optional[list[str]] = None) -> list[int]:
    """Moegliche Startpositionen der Nutzlast im Rohpaket, beste zuerst.

    Aufbau: header(1) + [transport(4)] + path_len(1) + path(...) + payload.
    Die Pfadlaenge steckt in einem Byte, dessen Bedeutung sich zwischen
    Firmwarestaenden unterscheiden kann (Anzahl Hops vs. Anzahl Bytes, dazu die
    Hashgroesse von 1 bis 3 Byte). Statt uns auf eine Lesart festzulegen,
    sammeln wir die plausiblen Offsets ein; entschieden wird per MAC-Pruefung.
    """
    if len(raw) < 4:
        return []

    transport = 4 if (raw[0] & 0x03) in _ROUTES_WITH_TRANSPORT else 0
    path_len_idx = 1 + transport
    if path_len_idx >= len(raw):
        return []

    offsets: list[int] = []

    def add(offset: int) -> None:
        if path_len_idx < offset <= len(raw) - 3 and offset not in offsets:
            offsets.append(offset)

    # 1. Aus dem bereits dekodierten Pfad des Observers: exakte Byteanzahl.
    if path_hops:
        add(path_len_idx + 1 + sum(len(hop) // 2 for hop in path_hops))

    # 2. Pfadbyte als Byteanzahl, und als Hopzahl mal Hashgroesse 1-3.
    path_len = raw[path_len_idx]
    hops = path_len & 0x3F
    add(path_len_idx + 1 + path_len)
    for hash_size in (2, 1, 3):
        add(path_len_idx + 1 + hops * hash_size)

    return offsets


def decode_group_text(raw: bytes, channels: ChannelSet,
                      path_hops: Optional[list[str]] = None) -> Optional[GroupMessage]:
    """GRP_TXT aus einem Rohpaket lesen. None, wenn kein Kanal passt.

    Die MAC-Pruefung entscheidet, ob ein Offset und ein Schluessel stimmen. Sie
    ist 2 Byte lang, das reicht hier: Ein falscher Treffer verlangt gleichzeitig
    passenden Kanalhash, passende Blocklaenge und passende 16 MAC-Bits.
    """
    if len(raw) < 4 or ((raw[0] >> 2) & 0x0F) != PT_GRP_TXT:
        return None

    for offset in payload_offsets(raw, path_hops):
        payload = raw[offset:]
        if len(payload) < 19:
            continue
        candidates = channels.candidates(payload[0])
        if not candidates:
            continue
        mac, ciphertext = payload[1:3], payload[3:]
        for channel in candidates:
            decrypted = _decrypt(ciphertext, mac, channel.key)
            if decrypted is None:
                continue
            timestamp, flags, text = decrypted
            sender, content = _split_sender(text)
            return GroupMessage(channel=channel, sender=sender, text=content,
                                sent_at=timestamp, flags=flags)
    return None
