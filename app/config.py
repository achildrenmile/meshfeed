"""Konfiguration. Alles kommt aus der Umgebung, nichts ist einkompiliert.

Eine Instanz = ein Satz Umgebungsvariablen. Ein zweiter Feed (etwa fuer einen
oeradio-Kanal) ist ein zweites Compose-Verzeichnis mit eigener ``.env``, eigenem
Datenvolume und eigenem Tunnel — dasselbe Image.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .decode import PUBLIC_CHANNEL_KEY, Channel, ChannelSet, derive_hashtag_key

THEME_DIR = Path(__file__).parent / "static" / "themes"
DEFAULT_THEME = "plain"


def resolve_theme(name: str) -> str:
    """Themename pruefen.

    Der Wert landet in einem ``<link href>``, deshalb nur harmlose Zeichen und
    nur Dateien, die es wirklich gibt — sonst waere das ein Weg, beliebige Pfade
    einzuschleusen. Ein unbekanntes Theme faellt still auf ``plain`` zurueck,
    damit eine falsche .env die Seite nicht unbenutzbar macht.
    """
    if not name or not re.fullmatch(r"[a-z0-9_-]+", name):
        return DEFAULT_THEME
    return name if (THEME_DIR / f"{name}.css").is_file() else DEFAULT_THEME


def available_themes() -> list[str]:
    return sorted(path.stem for path in THEME_DIR.glob("*.css"))


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower().lstrip("#")).strip("-")
    return slug or "kanal"


def _split_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_webhooks(value: str) -> dict[str, str]:
    """``DISCORD_WEBHOOKS`` lesen: ``at-ktn=https://…,at-ktn-bot=https://…``

    Schluessel ist der **Slug** des Kanals, nicht sein Name — ``#at-ktn-bot``
    wird zu ``at-ktn-bot``, genau wie in ``_slugify``. Damit steht in der .env
    dasselbe, was auch in den URLs des Feeds auftaucht.

    Der Sondername ``_knoten`` nimmt die Knotenmeldungen auf und ist deshalb
    kein Kanal-Slug.
    """
    ziele: dict[str, str] = {}
    for entry in _split_list(value):
        slug, _, url = entry.partition("=")
        slug, url = slug.strip(), url.strip()
        if not url:
            raise ValueError(f"DISCORD_WEBHOOKS-Eintrag ohne URL: {entry!r}")
        if not url.startswith("https://"):
            raise ValueError(f"DISCORD_WEBHOOKS: {slug} ist keine https-URL")
        ziele[slug if slug.startswith("_") else _slugify(slug)] = url
    return ziele


def parse_channels(channels: str, channel_keys: str = "") -> ChannelSet:
    """Kanaele aus zwei Umgebungsvariablen bauen.

    ``CHANNELS``     ``#kf=Kaernten funkt,#at-ktn`` — Schluessel wird aus dem
                     Namen abgeleitet. ``public`` meint den festen Public-Key.
    ``CHANNEL_KEYS`` ``intern=<32 Hex>=Intern`` — fuer private Kanaele, deren
                     Schluessel sich nicht aus dem Namen ergibt.
    """
    result = ChannelSet()

    for entry in _split_list(channels):
        name, _, label = entry.partition("=")
        name = name.strip()
        key = PUBLIC_CHANNEL_KEY if name.lower() == "public" else derive_hashtag_key(name)
        result.add(Channel(name=name, label=label.strip() or name, key=key, slug=_slugify(name)))

    for entry in _split_list(channel_keys):
        parts = [p.strip() for p in entry.split("=")]
        if len(parts) < 2:
            raise ValueError(f"CHANNEL_KEYS-Eintrag ohne Schluessel: {entry!r}")
        name, key_hex = parts[0], parts[1]
        label = parts[2] if len(parts) > 2 else name
        try:
            key = bytes.fromhex(key_hex)
        except ValueError as exc:
            raise ValueError(f"CHANNEL_KEYS: {name} hat keinen Hex-Schluessel") from exc
        result.add(Channel(name=name, label=label, key=key, slug=_slugify(name)))

    return result


@dataclass
class Settings:
    # --- Woher die Pakete kommen ---
    # "http" holt sie von der Karte: mehrere Observer, dafuer bis zu ein
    # Abrufintervall Verzoegerung und ein fremder Dienst dazwischen.
    # "mqtt" nimmt den eigenen Broker: ein Observer, dafuer sofort.
    # Es gibt keinen Rueckfall — was hier steht, laeuft.
    quelle: str = "http"

    # --- Quelle: MQTT-Broker des Observers ---
    # Leer erlaubt: bei quelle="http" wird kein Broker gebraucht. Ob der Wert
    # da sein muss, entscheidet from_env.
    mqtt_host: str = ""
    mqtt_port: int = 1883
    mqtt_user: str = ""
    mqtt_pass: str = ""
    mqtt_tls: bool = False
    mqtt_topics: list[str] = field(default_factory=lambda: ["meshcore/+/+/packets"])
    mqtt_client_id: str = "meshfeed"

    # --- Quelle: Karten-API ---
    karte_url: str = "https://map.carinthiamesh.com"
    karte_intervall_s: float = 20.0
    karte_limit: int = 200
    # Das Abrufenster beginnt so viele Sekunden vor dem juengsten bisher
    # gesehenen Paket. Faengt verspaetet einsortierte Pakete; die dabei
    # doppelt geholten faengt der Dedup.
    karte_ueberlappung_s: float = 60.0
    # Deckel gegen die Flut nach einer laengeren Stoerung.
    karte_max_seiten: int = 5
    karte_zeitlimit_s: float = 15.0

    # --- Anzeige ---
    site_title: str = "Mesh-Feed"
    site_tagline: str = ""
    site_link_url: str = ""
    site_link_label: str = ""
    site_logo_url: str = ""
    site_favicon_url: str = ""
    site_also_url: str = ""
    site_also_label: str = ""
    site_note: str = ""
    # Pflichtangabe, sobald der Feed oeffentlich erreichbar ist. Verweist
    # in der Regel auf das Impressum des Hauptauftritts.
    site_imprint_url: str = ""
    site_imprint_label: str = ""
    theme: str = "plain"

    # --- Betrieb ---
    channels: ChannelSet = field(default_factory=ChannelSet)
    db_path: str = "/data/meshfeed.db"
    retention_days: int = 30
    page_size: int = 100
    port: int = 8080
    # Ab wie vielen Minuten ohne ein einziges Paket ``/healthz/quelle`` auf
    # 503 geht. Nicht der Docker-Healthcheck — siehe main.py.
    quelle_still_minuten: int = 30

    # --- Discord-Spiegel ---
    # Leer = aus. Eine Instanz ohne diese Werte verhaelt sich wie vorher, das
    # ist der Normalfall: den Spiegel betreibt eine eigene Instanz.
    discord_webhooks: dict[str, str] = field(default_factory=dict)
    discord_min_abstand_s: float = 2.5
    discord_warteschlange_max: int = 200
    discord_trockenlauf: bool = False
    discord_start_still_s: float = 5.0
    discord_funkdaten: bool = True
    # Knotenmeldungen (neuer Knoten, Umbenennung) an das Ziel ``_knoten``.
    discord_knoten: bool = False
    # Welche Knoten gemeldet werden. Der eigene Observer hoert die Nachbarschaft,
    # die Karte dagegen **ganz Oesterreich** — ohne Filter stuenden hier
    # Neuzugaenge aus Graz und dem Burgenland. Gefiltert wird nach dem
    # Namensschema des Netzes; leer heisst: alles melden.
    discord_knoten_muster: str = r"^AT-(K|KL|VI|VL|FE|HE|SV|SP|VK|WO)-"
    # Beim ersten Start ist jeder Knoten neu. Ohne Aufwaermfrist setzt es
    # sofort drei Dutzend Meldungen — deshalb wird zu Beginn nur gefuellt.
    discord_warmup_min: int = 30

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = dict(os.environ if env is None else env)

        def get(key: str, default: str = "") -> str:
            return env.get(key, default).strip()

        def get_int(key: str, default: int) -> int:
            raw = get(key)
            return int(raw) if raw else default

        def get_bool(key: str, default: bool = False) -> bool:
            raw = get(key).lower()
            return raw in ("1", "true", "yes", "on") if raw else default

        def get_float(key: str, default: float) -> float:
            raw = get(key)
            return float(raw) if raw else default

        quelle = (get("QUELLE") or "http").lower()
        if quelle not in ("http", "mqtt"):
            raise ValueError(f"QUELLE kennt nur http oder mqtt, nicht {quelle!r}")

        host = get("MQTT_HOST")
        karte_url = get("KARTE_URL") or "https://map.carinthiamesh.com"
        # Verlangt wird nur, was die gewaehlte Quelle wirklich braucht.
        if quelle == "mqtt" and not host:
            raise ValueError("MQTT_HOST fehlt — bei QUELLE=mqtt ist er Pflicht")
        if quelle == "http" and not karte_url:
            raise ValueError("KARTE_URL fehlt — bei QUELLE=http ist sie Pflicht")

        channels = parse_channels(get("CHANNELS"), get("CHANNEL_KEYS"))
        if not len(channels):
            raise ValueError("CHANNELS fehlt — ohne Kanal gibt es nichts anzuzeigen")

        topics = _split_list(get("MQTT_TOPICS")) or ["meshcore/+/+/packets"]

        return cls(
            quelle=quelle,
            karte_url=karte_url,
            karte_intervall_s=get_float("KARTE_INTERVALL_S", 20.0),
            karte_limit=get_int("KARTE_LIMIT", 200),
            karte_ueberlappung_s=get_float("KARTE_UEBERLAPPUNG_S", 60.0),
            karte_max_seiten=get_int("KARTE_MAX_SEITEN", 5),
            karte_zeitlimit_s=get_float("KARTE_ZEITLIMIT_S", 15.0),
            mqtt_host=host,
            mqtt_port=get_int("MQTT_PORT", 1883),
            mqtt_user=get("MQTT_USER"),
            mqtt_pass=env.get("MQTT_PASS", ""),
            mqtt_tls=get_bool("MQTT_TLS"),
            mqtt_topics=topics,
            mqtt_client_id=get("MQTT_CLIENT_ID") or "meshfeed",
            site_title=get("SITE_TITLE") or "Mesh-Feed",
            site_tagline=get("SITE_TAGLINE"),
            site_link_url=get("SITE_LINK_URL"),
            site_link_label=get("SITE_LINK_LABEL"),
            site_logo_url=get("SITE_LOGO_URL"),
            # Ohne eigene Angabe dient das Logo als Favicon — ein Bild weniger
            # zu pflegen, und in der Tableiste steht dasselbe Zeichen wie oben
            # auf der Seite.
            site_favicon_url=get("SITE_FAVICON_URL") or get("SITE_LOGO_URL"),
            site_also_url=get("SITE_ALSO_URL"),
            site_also_label=get("SITE_ALSO_LABEL"),
            site_note=get("SITE_NOTE"),
            site_imprint_url=get("SITE_IMPRINT_URL"),
            site_imprint_label=get("SITE_IMPRINT_LABEL"),
            theme=resolve_theme(get("THEME")),
            channels=channels,
            db_path=get("DB_PATH") or "/data/meshfeed.db",
            retention_days=get_int("RETENTION_DAYS", 30),
            page_size=get_int("PAGE_SIZE", 100),
            port=get_int("PORT", 8080),
            quelle_still_minuten=get_int("QUELLE_STILL_MINUTEN", 30),
            discord_webhooks=parse_webhooks(get("DISCORD_WEBHOOKS")),
            discord_min_abstand_s=get_float("DISCORD_MIN_ABSTAND_S", 2.5),
            discord_warteschlange_max=get_int("DISCORD_WARTESCHLANGE_MAX", 200),
            discord_trockenlauf=get_bool("DISCORD_TROCKENLAUF"),
            discord_start_still_s=get_float("DISCORD_START_STILL_S", 5.0),
            discord_funkdaten=get_bool("DISCORD_FUNKDATEN", True),
            discord_knoten=get_bool("DISCORD_KNOTEN"),
            discord_knoten_muster=env.get(
                "DISCORD_KNOTEN_MUSTER", r"^AT-(K|KL|VI|VL|FE|HE|SV|SP|VK|WO)-"),
            discord_warmup_min=get_int("DISCORD_WARMUP_MIN", 30),
        )
